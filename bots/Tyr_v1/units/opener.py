"""Runs the hardcoded opener in `openers.py`, if this map really is that map.

Three units have a part in an opener and none of them can see the others' state
-- every unit runs in its own sub-interpreter, so a module global here is private
to one unit and there is no shared "what turn of the script are we on". So there
is no central conductor: each unit carries its own queue of ops, pops the next
one the moment that op is legal, and the schedule falls out of the ops gating
each other. The core cannot spawn onto a tile until the launcher has thrown the
last builder off it; the launcher has nothing to throw until the core spawns.
That is the whole synchronisation mechanism, and it means a turn that slips costs
a turn rather than desynchronising the script.

    core.py                 -> core_spawn()      owns the core's one spawn per turn
    turret_launcher.py      -> launcher_throw()  the scripted throws
    units/states/opening.py -> the builder half, as an ordinary state

# Being sure it is the right map

Size and core position are NOT an identity. Across the current pool they collide
outright -- yulerune and frostgate are both 20x20 with cores at (2,9) and (16,9)
-- so a script keyed on those alone would run yulerune's opener on frostgate and
throw builders into walls. Identification therefore runs against `mapdata.py`'s
baked terrain: candidates are every map of our size whose core matches ours, they
are narrowed by every tile this unit has actually looked at, and the opener only
starts once exactly one survives.

`verify()` then keeps doing it, every turn, for as long as the opener is live. A
single tile that disagrees with the table drops the whole thing and the unit
plays on as if there had never been an opener. Terrain the bot merely inferred
from symmetry is not evidence and is excluded -- only `_bm_seen_observed`, tiles
read off the board with `get_tile_env`, can convict.

Being wrong is bounded either way: at worst a few units idle for a few turns
before the board contradicts the table.

# The titanium buffer

An opener that spends itself out of finishing is worse than no opener -- a half
sealed chokepoint is a chokepoint. So the core's scripted spawns have to leave

    resources - cost > (barriers the opener still owes) * barrier cost

behind them. `barrier_reserve()` counts what is owed by looking at the scripted
barrier tiles themselves: one that already holds a barrier is paid for, anything
else is still owed. A unit that cannot see a tile counts it as unbuilt, so the
reserve errs high, which is the safe direction.

The builders themselves build with no reserve at all -- not this one, not
`map_info.ti_reserve()`'s flat 40 -- because they are what the reserve is being
held FOR. Only `can_spend`, and therefore only the core, is gated.
"""

from fcode import Controller, Direction, EntityType, Position

import comms
import map_info
import mapdata
import openers
from log import log

rc: Controller = None

# --- identification, per unit like every module global here -----------------
name: str | None = None
spec: dict | None = None
mirror = False
_cands: list | None = None     # [(name, wall, ore)] still consistent, decoded
_wall = 0                      # the identified map's masks, kept for verify()
_ore = 0
_dead = False

# --- the core's half of the script ---
_core_step = 0

# --- a launcher's half ---
_launch_q: tuple | None = None   # None until this launcher has looked itself up
_launch_step = 0
_retired = False                 # this launcher has spent itself

_BUILD_KIND = {
    "conveyor": EntityType.CONVEYOR,
    "splitter": EntityType.SPLITTER,
    "harvester": EntityType.HARVESTER,
    "barrier": EntityType.BARRIER,
    "gunner": EntityType.GUNNER,
    "sentinel": EntityType.SENTINEL,
    "launcher": EntityType.LAUNCHER,
}


def init(c: Controller) -> None:
    """Reset every latch. init() only ever runs once per unit, but the states in
    this codebase all re-zero defensively and there is no reason to differ."""
    global rc, name, spec, mirror, _cands, _wall, _ore, _dead
    global _core_step, _launch_q, _launch_step, _retired, _sentinel_q, _no_spawn
    rc = c
    _no_spawn = None
    name = None
    spec = None
    mirror = False
    _cands = None
    _wall = _ore = 0
    _dead = False
    _core_step = 0
    _launch_q = None
    _launch_step = 0
    _retired = False
    _sentinel_q = None


def _my_core() -> Position | None:
    """Our core's corner, from sight if we have it and from the core's own
    broadcast if we do not.

    The fallback is not hypothetical: a builder that the core spawns and a
    launcher throws on the same turn has its first `run()` somewhere across the
    map, having never had the core in vision, so `map_info._my_core` is None for
    it and stays None. The core publishes its position in slot 0 every round.
    """
    core = map_info._my_core
    if core is not None:
        return core
    return comms.core_position()


def _narrow() -> None:
    """Cut the candidate list down to the maps still consistent with the board.

    First call builds the list from `mapdata`; later calls only re-filter. Both
    are a couple of big-integer ANDs per candidate, and there are at most a
    handful of candidates because the size and core position have already been
    matched.
    """
    global _cands, name, spec, mirror, _wall, _ore
    core = _my_core()
    if core is None:
        return
    mine = (core.x, core.y)
    if _cands is None:
        _cands = []
        for entry in mapdata.MAPS.get((map_info._width, map_info._height), ()):
            nm, core_a, core_b, sym, wall_hex, ore_hex = entry
            if mine != core_a and mine != core_b:
                continue
            _cands.append((nm, sym, int(wall_hex, 16), int(ore_hex, 16)))
        if not _cands:
            abandon("board matches no known map")
            return

    observed = map_info._bm_seen_observed
    obs_wall = map_info._bm_env[map_info._IDX_ENV_WALL] & observed
    obs_ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI] & observed
    _cands = [c for c in _cands
              if c[2] & observed == obs_wall and c[3] & observed == obs_ore]

    if not _cands:
        abandon("terrain contradicts every candidate")
        return
    if len(_cands) > 1 or spec is not None:
        return                      # still ambiguous, or already committed

    nm, sym, wall, ore = _cands[0]
    # Which side we are on is decided against the TABLE's authored core, never
    # against mapdata's core_a. A table may be written from either side -- some
    # are -- and deriving the flip from mapdata silently inverts every coordinate
    # for the ones written from the second core.
    hit = openers.lookup(map_info._width, map_info._height, mine)
    if hit is None:
        abandon(f"{nm} has no opener")
        return
    found, entry, flipped = hit
    if found != nm:
        abandon(f"{nm}: terrain and core position disagree ({found})")
        return
    if entry["sym"] != sym or tuple(entry["size"]) != (map_info._width, map_info._height):
        abandon(f"{nm}: opener table disagrees with mapdata")
        return
    name, spec, mirror, _wall, _ore = nm, entry, flipped, wall, ore
    log(f"OPENER {name}{' mirrored' if mirror else ''}")


def verify() -> bool:
    """True while an opener is live and the board still agrees with it.

    Call once a turn, from every unit that has a part. Cheap while ambiguous
    (a filter over a handful of candidates), two big-int ANDs once committed.
    """
    global _cands
    if _dead:
        return False
    if spec is None:
        _narrow()
        return spec is not None
    observed = map_info._bm_seen_observed
    if (_wall & observed != map_info._bm_env[map_info._IDX_ENV_WALL] & observed
            or _ore & observed != map_info._bm_env[map_info._IDX_ENV_ORE_TI] & observed):
        abandon(f"{name}: a tile disagrees with the table")
        return False
    return True


def abandon(why: str) -> None:
    """Drop the opener for this unit, permanently. There is no way back: the
    board disagreeing with the table once means the table was never right."""
    global _dead, spec
    if not _dead:
        _dead = True
        spec = None
        log(f"OPENER abandoned ({why})")


def pos(tile) -> Position:
    """A table coordinate as a real Position on our side of the board."""
    if mirror:
        tile = openers.mirror_pos(tile, spec)
    return Position(tile[0], tile[1])


_DIRECTIONS = {d.name.lower(): d for d in Direction}


def facing(name: str) -> Direction:
    """A table facing as a real Direction, mirrored with the coordinates.

    Mirroring a turret's facing is separate from mirroring its tile and easy to
    forget: a sentinel whose southeast ray threads a doorway on one side has to
    face southwest to thread the mirrored one.
    """
    if mirror:
        name = openers.mirror_dir(name, spec)
    return _DIRECTIONS[name]


# --- the titanium buffer ----------------------------------------------------
def barrier_reserve() -> int:
    """Titanium that must survive the next scripted action.

    Every scripted barrier that is not standing yet is still owed. Tiles this
    unit has never seen read as empty, so they count as owed too and the reserve
    comes out high rather than low.
    """
    if spec is None:
        return 0
    owed = 0
    for role in spec["builders"]:
        for op in role:
            if op[0] == openers.BUILD and op[1] == "barrier":
                p = pos(op[2])
                if map_info.type_at(p.x, p.y) is not EntityType.BARRIER:
                    owed += 1
    return owed * rc.get_barrier_cost()


_no_spawn: frozenset | None = None


def no_spawn_tiles() -> frozenset:
    """Core-ring tiles the ordinary spawn chooser must not put a builder on.

    Empty on any map without an opener, and empty for the opener's own scripted
    spawns, which name their tile directly and never come through here. Cached
    after the first identification: this is asked once per candidate tile per
    spawn, and the answer cannot change.
    """
    global _no_spawn
    if not verify():
        return frozenset()
    if _no_spawn is None:
        _no_spawn = frozenset(pos(t) for t in spec.get("no_spawn", ()))
    return _no_spawn


def can_spend(cost: int) -> bool:
    """The opener's spending rule: `resources - cost > reserve`.

    Applies to the CORE's scripted spawns and nothing else. The builders do not
    consult it -- see `units/states/opening.py` -- because the reserve exists to
    stop other spending from eating the barrier budget, and a builder placing a
    scripted barrier IS the barrier budget being spent. Gating it on its own
    reserve would be the budget refusing to buy the thing it is a budget for.

    A builder is 30 Ti before scaling and ten barriers are about 30 Ti total, so
    on the core side this is a real constraint and not a formality: without it
    the fifth scripted spawn can leave the seal unaffordable.

    This also replaces `map_info.ti_reserve()` for scripted spawns rather than
    stacking with it. The opener is the priority while it runs.
    """
    return rc.get_global_resources() - cost > barrier_reserve()


# --- the core ---------------------------------------------------------------
def core_spawn() -> bool:
    """Take this turn's single spawn if the script still wants one.

    Returns True whenever the opener owns the turn -- including the turns it
    deliberately does nothing, waiting for a launcher to clear the spawn tile.
    The caller must not fall through to its own spawn logic on a True: a
    repairer, a defender or an economic builder dropped on a different ring tile
    is a builder the script's launchers cannot reach, and the rest of the script
    is then throwing at empty tiles.
    """
    global _core_step
    if not verify():
        return False
    script = spec["core_script"]
    if _core_step >= len(script):
        return False                     # script spent; the core is its own again
    tile = pos(script[_core_step][1])
    if (rc.get_action_cooldown() == 0
            and rc.can_spawn(tile)
            and can_spend(rc.get_builder_bot_cost())):
        rc.spawn_builder(tile)
        map_info.update_at(tile)
        _core_step += 1
        log(f"OPENER core spawn {_core_step}/{len(script)} at {tile}")
    return True


# --- a launcher -------------------------------------------------------------
def launcher_throw() -> bool:
    """Make this launcher's next scripted throw, or retire once they are done.

    True means the turn is spent (or the launcher no longer exists). A launcher
    finds its own script by where it stands, not by who built it, which is what
    lets the script survive the launcher going up a turn late.

    These launchers are built for one job: throw two builders out of the core's
    spawn ring. After that they are worth less than nothing -- a launcher holds
    one of the 50 unit slots, and every one standing adds 10% to the cost of the
    next launcher the bot wants anywhere on the map. So the last throw is
    followed by a self-destruct.
    """
    global _launch_q, _launch_step, _retired
    if not verify():
        return False
    if _launch_q is None:
        me = map_info._my_pos
        _launch_q = ()
        for tile, queue in spec["launchers"].items():
            if pos(tile) == me:
                _launch_q = tuple(queue)
                break
    if _launch_step < len(_launch_q):
        src, dst = pos(_launch_q[_launch_step][1]), pos(_launch_q[_launch_step][2])
        if rc.get_action_cooldown() == 0 and rc.can_launch(src, dst):
            rc.launch(src, dst)
            _launch_step += 1
            log(f"OPENER throw {src} -> {dst}")
            return True
        return False

    # Scripted throws done -- or there never were any. A launcher the chain built
    # partway along is a ferry and nothing else, so the ferry check has to sit
    # outside the scripted-queue branch entirely; gating it on `_launch_q` was
    # why chain launchers went up and then stood there doing nothing.
    if my_ferry() is not None:
        return _ferry()
    if not _launch_q:
        return False                       # nothing scripted here at all
    if not spec.get("keep_launchers") and not _retired:
        _retired = True                    # self_destruct() cannot fail, but the
        log("OPENER launcher retiring")    # latch keeps this a one-way door
        rc.self_destruct()
        return True
    return False


def my_ferry():
    """This launcher's (pickup, destination) pair, or None.

    Also the answer to "am I a ferry launcher" for the builder side, which asks
    the same table by tile rather than by standing on it.
    """
    if spec is None:
        return None
    for tile, route in spec.get("ferry", {}).items():
        if pos(tile) == map_info._my_pos:
            return pos(route[0]), pos(route[1])
    return None


def ferry_stops():
    """Every (pickup, destination) in the table, for builders looking for one."""
    if spec is None:
        return ()
    return tuple((pos(r[0]), pos(r[1])) for r in spec.get("ferry", {}).values())


def our_ore():
    """The ore tiles the table calls ours, or () if it does not say."""
    if spec is None:
        return ()
    return tuple(pos(t) for t in spec.get("ore", ()))


def ferries_only(bot_id: int) -> bool:
    """Whether a launcher is allowed to throw this builder forward.

    Odd ids only. The launcher enforcing it is what actually makes it true: the
    pickup tile is an ordinary tile any builder can walk over, and asking them
    all nicely to keep off it does not stop one wandering across.
    """
    return bot_id % 2 == 1


def ferry_terminal(dest) -> bool:
    """Is `dest` the end of the chain, or just the next pickup tile along it?

    A chain hop lands a builder exactly where the next launcher collects from, so
    "have I arrived" cannot be "have I been thrown" -- it has to be "is this a
    landing tile that nothing throws on from".
    """
    return all(dest != src for src, _ in ferry_stops())


def ferry_launcher_for(pickup):
    """A launcher this table wants built to serve `pickup`, if it is missing.

    None when there is nothing to build -- either the table does not ask for one
    here, or it is already standing.
    """
    if spec is None:
        return None
    build = {pos(t) for t in spec.get("ferry_build", ())}
    for tile, route in spec.get("ferry", {}).items():
        site = pos(tile)
        if pos(route[0]) != pickup or site not in build:
            continue
        if map_info.type_at(site.x, site.y) is not EntityType.LAUNCHER:
            return site
    return None


def _ferry() -> bool:
    """Throw whoever is waiting on our pickup tile across. True if we threw.

    Runs for the rest of the game once the scripted throws are done. Deliberately
    indiscriminate about WHICH builder it throws -- the pickup tile is the
    signal, and a builder only stands on it because it decided to go.
    """
    route = my_ferry()
    if route is None:
        return False
    src, dst = route
    if rc.get_action_cooldown() != 0:
        return False
    bot = rc.get_tile_builder_bot_id(src)
    if bot is None or not ferries_only(bot):
        return False              # not one of ours to send
    if not rc.can_launch(src, dst):
        return False
    rc.launch(src, dst)
    log(f"OPENER ferry {src} -> {dst}")
    return True


# --- a sentinel -------------------------------------------------------------
_sentinel_q: tuple | None = None    # None until this sentinel has looked itself up


def _enemy_building_at(p: Position) -> bool:
    bid = rc.get_tile_building_id(p)
    return bid is not None and rc.get_team(bid) != map_info._my_team


def _friendly_builder_beside(p: Position) -> bool:
    for d in map_info._CARDINAL:
        n = map_info.pos_add(p, d)
        if not map_info.in_bounds(n):
            continue
        bot = rc.get_tile_builder_bot_id(n)
        if bot is not None and rc.get_team(bot) == map_info._my_team:
            return True
    return False


def sentinel_fire() -> bool:
    """Take this sentinel's scripted shot. True if it fired.

    False means "nothing scripted applies", and the caller should fall through
    to ordinary targeting -- a scripted sentinel with no scripted target in
    reach is still a sentinel, and an enemy walking into its line is still worth
    18 damage.
    """
    global _sentinel_q
    if not verify():
        return False
    if _sentinel_q is None:
        me = map_info._my_pos
        _sentinel_q = ()
        for tile, targets in spec.get("sentinels", {}).items():
            if pos(tile) == me:
                _sentinel_q = tuple(pos(t) for t in targets)
                break
    if not _sentinel_q:
        return False
    if rc.get_action_cooldown() != 0:
        return False
    last = len(_sentinel_q) - 1
    for i, target in enumerate(_sentinel_q):
        if not _enemy_building_at(target):
            continue
        # Every target but the fallback is a combination shot: hold fire until a
        # builder is beside it, so the two hits land together. Firing alone
        # spends 10 ammo and leaves a 20 HP conveyor standing on 2.
        if i != last and not _friendly_builder_beside(target):
            continue
        if not rc.can_fire(target):
            continue
        rc.fire(target)
        log(f"OPENER sentinel {map_info._my_pos} fires {target}")
        return True
    return False


def build_kind(what: str):
    return _BUILD_KIND[what]
