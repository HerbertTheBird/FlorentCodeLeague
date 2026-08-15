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
_chosen: Position | None = None  # the tile this launcher's last rule picked

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
    global _avoid_ore_mask, _chosen
    rc = c
    _no_spawn = None
    _avoid_ore_mask = None
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
    _chosen = None
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
        src = pos(_launch_q[_launch_step][1])
        dst = _throw_target(_launch_q[_launch_step][2])
        if dst is None:
            return False                   # a rule with no answer this turn
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
    """This launcher's (pickup, destination, who) triple, or None.

    Also the answer to "am I a ferry launcher" for the builder side, which asks
    the same table by tile rather than by standing on it.
    """
    if spec is None:
        return None
    for tile, route in spec.get("ferry", {}).items():
        if pos(tile) == map_info._my_pos:
            return pos(route[0]), _dest(route[1]), _who(route)
    return None


def _dest(d):
    """A ferry destination: a fixed tile, or the string naming a rule.

    ENEMY_SIDE is resolved per throw rather than baked into the table, because a
    map can have no single doorway worth aiming at -- the right answer is
    whichever reachable tile is nearest their core at the time.
    """
    return d if isinstance(d, str) else pos(d)


def _who(route):
    """The (modulus, remainder) an id must satisfy for this stop, or None.

    Per-stop, because a map can run more than one crossing and want a different
    slice of the workforce on each: valkyrie sends id%3==0 south and id%3==1
    north, keeping the last third at home.
    """
    if len(route) > 2:
        return tuple(route[2])
    return spec.get("ferry_who")


def ferry_stops():
    """Every (pickup, destination, who) in the table."""
    if spec is None:
        return ()
    return tuple((pos(r[0]), _dest(r[1]), _who(r))
                 for r in spec.get("ferry", {}).values())


def _key_matches(key: int, who) -> bool:
    """`who` is (modulus, remainder) or (modulus, (remainders...))."""
    if not who:
        return True
    rem = who[1]
    if isinstance(rem, int):
        return key % who[0] == rem
    return key % who[0] in rem


def split_matches(slot, bot_id: int, who) -> bool:
    """Does this builder fall in the half the map wants sent forward?

    Split on the COMMS SLOT wherever there is one, and only fall back to the
    entity id when there is not.

    The id is a bad key on its own. Ids come from a single counter shared by both
    teams, so any function of one -- parity, or a hash -- inherits whatever
    interleaving that particular game produced. Measured on royale: "odd id"
    selected 3 of our 12 builders on one side and 9 of 15 on the other, so the
    crossing fired once in a game we lost and seven times in the same game from
    the other seat. Hashing the id changes which builders, not how many.

    Slots are claimed lowest-free-first out of our own fourteen, so slot parity
    is exactly seven and seven no matter what the ids look like. That is the
    guaranteed core of the split.

    The fallback is not a compromise, it is the rest of the crew: there are only
    fourteen slots and the working crew runs well past that -- 35 units alive on
    drakkarfjord after the hiring cap lifts -- so keying on slots alone would cap
    the attack at seven however rich we got. Everything above the slot range is
    split on its id, which is biased but additive on top of a guaranteed seven,
    so the failure mode of three attackers in a whole game cannot come back.
    """
    return _key_matches(slot if slot is not None else bot_id, who)


def id_matches(bot_id: int, who) -> bool:
    """Id-only form, for callers with no way to learn a slot."""
    return _key_matches(bot_id, who)


def our_ore():
    """The ore tiles the table calls ours, or () if it does not say."""
    if spec is None:
        return ()
    return tuple(pos(t) for t in spec.get("ore", ()))


def ferry_wants(slot, bot_id: int) -> bool:
    """Is there any crossing on this map that would take this builder?"""
    return any(split_matches(slot, bot_id, who) for _, _, who in ferry_stops())


def ferry_terminal(dest) -> bool:
    """Is `dest` the end of the chain, or just the next pickup tile along it?

    A chain hop lands a builder exactly where the next launcher collects from, so
    "have I arrived" cannot be "have I been thrown" -- it has to be "is this a
    landing tile that nothing throws on from".
    """
    return all(dest != src for src, _, _ in ferry_stops())


_avoid_ore_mask: int | None = None


def avoid_ore_mask() -> int:
    """Ore the table says not to work, as a tile bitmask.

    Some ore is not worth what it costs to hold -- too far out, or on the wrong
    side of a seal, or simply drawing builders away from the bank. Cached: the
    answer cannot change once the map is known.
    """
    global _avoid_ore_mask
    if spec is None:
        return 0
    if _avoid_ore_mask is None:
        w = map_info._width
        m = 0
        for t in spec.get("avoid_ore", ()):
            p = pos(t)
            m |= 1 << (p.x + p.y * w)
        _avoid_ore_mask = m
    return _avoid_ore_mask


def builder_buffer() -> int:
    """Extra titanium the core should hold back before hiring another builder.

    Raises the economic spawn gate on maps where the plan needs a bank rather
    than a bigger crew -- drakkarfjord wants 1000 in hand to start its crossing,
    and hiring at the default rate never let it get there. Drops back to nothing
    once the bank is past the release mark and hiring is meant to open up.
    """
    if spec is None:
        return 0
    if rc.get_global_resources() >= openers.BUILDER_CAP_RELEASE:
        return 0
    return spec.get("builder_buffer", 0)


def spawn_gate_open() -> bool:
    """May the core make ORDINARY spawns yet?

    A map can name a tile that has to be built before the economy is allowed to
    hire at all -- glacierkeep names the far harvester its opening walks out to
    lay. Until that tile is ours the core spawns only what its own script asks
    for, so the bank goes into the route rather than into a crew with nothing to
    work on.

    An unobserved tile reads as not-built, which holds hiring rather than
    releasing it. That is the safe direction: the unit that most wants to hire is
    the core, and the core can see its own doorstep.
    """
    if spec is None:
        return True
    tile = spec.get("spawn_gate")
    if tile is None:
        return True
    p = pos(tile)
    return map_info.type_at(p.x, p.y) is not None \
        and bool(map_info._bm_team[map_info._my_team_idx] >> (p.x + p.y * map_info._width) & 1)


def may_hire(units_alive: int, titanium: int) -> bool:
    """May the core take on another economic builder?

    Counts every unit alive rather than the ones standing near the core, which
    is the measure the default gate uses and the reason it never stops. One is
    subtracted for the core itself; the odd launcher counts as a builder here,
    which is close enough at these numbers and errs toward hiring less.
    """
    if spec is None:
        return True
    if titanium >= openers.BUILDER_CAP_RELEASE:
        return True
    return units_alive - 1 < openers.BUILDER_SOFT_CAP


def ferry_sites():
    """Every launcher tile the table wants a ferry builder to put up."""
    if spec is None:
        return ()
    return tuple(pos(t) for t in spec.get("ferry_build", ()))


def needs_unit_slots() -> int:
    """How many of those launchers are still missing.

    A launcher counts against the 50-unit cap, so if the economy fills every
    slot with builders the ferry launcher can never be built at all -- which is
    exactly what happened: units=50, 4000 titanium banked, and can_build_launcher
    returning False forever. The core keeps this many slots free.

    An unobserved tile counts as missing, which errs toward reserving a slot we
    did not need. That is the harmless direction.
    """
    n = 0
    for site in ferry_sites():
        if map_info.type_at(site.x, site.y) is not EntityType.LAUNCHER:
            n += 1
    return n


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


def their_core():
    """The enemy core's corner, off the table rather than the symmetry solver.

    The solver only answers once `_solved_sym` latches; the table knows from the
    first turn, and on a map we have already identified there is nothing to wait
    for.
    """
    core = _my_core()
    if spec is None or core is None:
        return None
    w, h = spec["size"]
    t = openers._flip_core((core.x, core.y), spec["sym"], w, h)
    return Position(t[0], t[1])


def _forward_target():
    """The tile nearest the enemy core that this launcher can actually throw to.

    "Their side" is nearer their core than ours. That is one comparison per
    candidate rather than a flood fill, and on a map whose seal runs along the
    bisector it asks the same question as "across the wall" -- which is the
    property that matters, since the whole point is to put a builder somewhere it
    could not have walked.
    """
    theirs = their_core()
    if theirs is None:
        return None
    mine = _my_core()
    best = None
    best_d = None
    for t in rc.get_attackable_tiles():
        d = t.distance_squared(theirs)
        if best_d is not None and d >= best_d:
            continue
        if mine is not None and d >= t.distance_squared(mine):
            continue
        if not rc.is_tile_passable(t):
            continue
        best_d, best = d, t
    return best


def _throw_target(dst):
    """Where a scripted throw actually lands: a table tile, or a rule.

    Rules exist for the same reason the ferry's `enemy_side` does -- the tile
    worth aiming at is not knowable when the table is written -- and they are
    resolved HERE, by the launcher, in the round it throws, so a builder or a
    building standing in the way costs a tile rather than the plan.

    The answer is kept, and `SAME` returns it. That is the whole of "remember
    where you threw the first one and send the second one after it": a launcher's
    module state is private to that one launcher and lives as long as it does,
    which is exactly the lifetime the memory wants. It is deliberately the tile
    the rule PICKED and not the tile the throw reached, because they are the same
    thing -- `can_launch` refuses anything else -- and re-resolving on the second
    throw would answer a different board.
    """
    global _chosen
    if dst == openers.SAME:
        return _chosen
    if dst == openers.NEAR_CORE:
        _chosen = _near_core()
        return _chosen
    if isinstance(dst[0], str):                  # (FLANK, tile)
        want = pos(dst[1])
        _chosen = want if rc.is_tile_passable(want) else _near_core(want)
        return _chosen
    return pos(dst)


def _near_core(beside=None):
    """The tile in throw range nearest the enemy core, or None.

    Distance is to their core's corner, the same measure `_forward_target` uses;
    ties go to whichever the engine lists first, which is good enough because a
    tie means two tiles equally close to what we are aiming at.

    `beside` restricts the answer to the side of the core that tile sits on, so
    two builders sent to opposite corners cannot both end up on the same one. The
    side is read off the tile rather than named in the table, which is what keeps
    it mirroring correctly: on an x-mirrored map the west flank becomes the east
    one without the table saying anything.
    """
    theirs = their_core()
    if theirs is None:
        return None
    axis = low = None
    if beside is not None:
        # The core is 2x2, so clear of it means < corner on the low side and
        # > corner+1 on the high one. Whichever axis `beside` is clear on is the
        # axis the side is measured along; x is tried first, since a flank named
        # beside a core is nearly always an east/west one.
        if beside.x < theirs.x or beside.x > theirs.x + 1:
            axis, low = 0, beside.x < theirs.x
        else:
            axis, low = 1, beside.y < theirs.y
    best = None
    best_d = None
    for t in rc.get_attackable_tiles():
        d = t.distance_squared(theirs)
        if best_d is not None and d >= best_d:
            continue
        if axis is not None:
            v = t.x if axis == 0 else t.y
            edge = theirs.x if axis == 0 else theirs.y
            if not (v < edge if low else v > edge + 1):
                continue
        if not rc.is_tile_passable(t):
            continue
        best_d, best = d, t
    return best


def _ferry() -> bool:
    """Throw whoever is waiting on our pickup tile across. True if we threw.

    Runs for the rest of the game once the scripted throws are done. Deliberately
    indiscriminate about WHICH builder it throws -- the pickup tile is the
    signal, and a builder only stands on it because it decided to go.
    """
    route = my_ferry()
    if route is None:
        return False
    src, dst, who = route
    if rc.get_action_cooldown() != 0:
        return False
    if isinstance(dst, str):
        dst = _forward_target()
        if dst is None:
            return False
    bot = rc.get_tile_builder_bot_id(src)
    if bot is None or not split_matches(comms.slot_at(src), bot, who):
        return False              # not one of ours to send
    if not rc.can_launch(src, dst):
        return False
    rc.launch(src, dst)
    log(f"OPENER ferry {src} -> {dst}")
    return True


# --- a sentinel -------------------------------------------------------------
_sentinel_q: tuple | None = None    # None until this sentinel has looked itself up


def _worth_shooting(p: Position) -> bool:
    """An enemy building stands on `p` and 18 damage is not wasted on it.

    The one exception is a core already down to 4 HP or less. The scripted
    builders that put this sentinel up are standing on its ring hitting the core
    for 2 each, so 4 is a core they finish between them this turn for 4 Ti --
    against 10 ammo, which the core bought with 10 titanium at 1:1. Firing would
    be paying more for something that was already happening.
    """
    bid = rc.get_tile_building_id(p)
    if bid is None or rc.get_team(bid) == map_info._my_team:
        return False
    return not (rc.get_entity_type(bid) is EntityType.CORE and rc.get_hp(bid) <= 4)


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
        if not _worth_shooting(target):
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


def turret_floor() -> int:
    """Titanium a ferried attacker must leave behind when it buys a turret.

    Zero unless the map asks for turrets, in which case it is what keeps the
    spending from eating the ammunition: the core converts titanium to ammo 1:1
    and a sentinel is 10 a shot, so a turret bought with the last of the bank is
    a turret that never fires.
    """
    return 0 if spec is None else spec.get("turret_floor", 0)


def wants_attack_turrets() -> bool:
    return bool(spec is not None and spec.get("turret_floor"))


def strike_stands_off() -> bool:
    """Does a STRIKE on this map give up a tile rather than fight for it?

    Off by default -- valkyrie's doorway is worth taking and the enemy is not
    usually beside it. On at glacierkeep, where every STRIKE is a tile of the
    ring around the enemy CORE: there is always a defender next to those, so the
    2-Ti-for-2-HP exchange runs four to one against us against 4-HP-for-1-Ti
    healing, the conveyor never dies, and the bank the rest of the plan needs
    goes with it.
    """
    return bool(spec is not None and spec.get("strike_stands_off"))


def build_kind(what: str):
    return _BUILD_KIND[what]
