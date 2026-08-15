"""The builder's half of a hardcoded opener: walk here, put a barrier there.

An ordinary state, so it costs one `score()` call on maps with no opener and
composes with everything else for free. It declares the highest MAX_SCORE in the
bot, which means two things: while a program is live nothing can interrupt it,
and because `select_best_state` walks the states in descending MAX_SCORE order
and breaks as soon as the best score reaches the next state's ceiling, no other
state's `score()` even runs. On every other map it returns 0 immediately and the
bot behaves exactly as it did before.

Preempting even a siege at home is deliberate. These programs are a handful of
turns that seal a chokepoint the whole rest of the game is played around, and a
builder that abandons the walk halfway leaves a barrier line with a hole in it --
worse than never having started. There is no escape hatch either: an op that is
not legal yet is retried forever rather than timed out, because "not yet" is
almost always a shortage and not an impossibility. Only the map check in
`units/opener.py` can call the whole thing off.

# Guarding

Finishing the program does not release the builder. A barrier is 30 HP and an
enemy builder erases it at 2 HP a turn for 2 Ti, so an unwatched seal is a seal
with a countdown on it; the point of these openers is that the line holds for the
rest of the game. So a builder that has run out of ops goes on standing next to
what it built, permanently:

    * pick the barrier with the lowest HP, ties broken toward whichever one an
      enemy builder is nearest to -- a destroyed one counts as 0 HP and so sorts
      first of all;
    * if it is gone, rebuild it;
    * otherwise just be adjacent to it and spend no action.

That last line is why the healing is not written here. `builder.run()` already
offers an unspent turn to `heal._do_best_heal()`, which heals the most damaged
adjacent friendly building and refuses to spend the 1 Ti unless a full 4 HP would
be restored -- the same "4 or more missing" rule, already gated correctly against
overheal, already prioritising barriers. Standing still next to the weakest
barrier is exactly the input that function wants, so the guard's job is only to
be in the right place.

Which tile that is, is a real choice and not just "any neighbour". A guard is a
40 HP builder standing motionless for hundreds of turns, which is the easiest
target on the board: a sentinel hits for 18 and reaches r^2=32, so two shots
from something the guard cannot even see kill it, and the barrier it was there to
heal then dies unattended. So the standing tile is picked out of the neighbours
NOT in `map_info._bm_enemy_turret_threat` -- the mask of every tile an enemy
gunner or sentinel can shoot, which the bot already maintains and which
`Pathing.move_to` already routes around. If every neighbour of the target is
covered, the guard does not take the trade: it stays out of fire and waits,
because a dead guard heals nothing.
"""

from main import has_op
from fcode import Controller, EntityType

import map_info
import openers
import units.builder
import units.opener as opener
from log import log

rc: Controller = None
nav = None

# Above defend's SIEGE_SCORE (20), the previous ceiling: while a scripted program
# is live it is the only thing this builder does.
MAX_SCORE = 100

_role = None            # scripted role index, or None
_prog: list | None = None
_step = 0
_claimed = False
_barriers: tuple = ()   # the barriers this role owns, once claimed
_guard_tile = None      # the one currently being stood next to
_ferried = False        # has reached the end of the chain; ordinary again
_ferrying = False       # committed to going, part-way or not
_eco_done = False       # our ore has all been worked at least once


def init(c: Controller):
    global rc, nav, _role, _prog, _step, _claimed, _barriers, _guard_tile
    global _ferried, _ferrying, _eco_done
    rc = c
    nav = units.builder.nav
    _role = None
    _prog = None
    _step = 0
    _claimed = False
    _barriers = ()
    _guard_tile = None
    _ferried = False
    _ferrying = False
    _eco_done = False


def _entry_matches(spec, role: int, prog: list, me) -> bool:
    """Whether a builder standing on `me`, on its first turn, is this role.

    Two ways in, and the distinction is what keeps roles unambiguous when two of
    them are thrown to the same tile (yulerune sends both A and C to (7,3)):

      * on the role's spawn tile -- the normal case, claimed beside the core;
      * on the tile the role's opening WAIT names, but only when WAIT is the
        FIRST op of the program. A builder spawned and thrown on the same turn
        never runs beside the core, so it has to be able to join at its landing
        tile -- but a role that opens by BUILDING something has already done work
        beside the core by then, so it must not be claimable out there.
    """
    script = spec["core_script"]
    if role < len(script) and opener.pos(script[role][1]) == me:
        return True
    return bool(prog) and prog[0][0] == openers.WAIT and opener.pos(prog[0][1]) == me


def _throw_possible(target) -> bool:
    """Could some friendly launcher actually throw us from here to `target`?

    A WAIT is a builder standing on a pickup square expecting to be picked up, so
    it is only a sane thing to be doing while a launcher exists that can do it:
    within pickup reach of us (d^2 <= 2) and throw reach of the destination
    (d^2 <= 26).

    Without this an ordinary economic builder that happens to spawn on a scripted
    spawn tile claims that role, starts its WAIT, and -- since the scripted
    launchers self-destruct once their throws are made -- stands there for the
    rest of the game waiting for a throw from a launcher that no longer exists.
    That is not a timeout being missed; the thing it is waiting for cannot
    happen, which is a different question and one that can be asked directly.
    """
    me = map_info._my_pos
    mask = (map_info._bm_et[map_info._IDX_LAUNCHER]
            & map_info._bm_team[map_info._my_team_idx])
    for p in map_info.iter_mask(mask):
        if p.distance_squared(me) <= 2 and p.distance_squared(target) <= 26:
            return True
    return False


def _already_standing(spec, prog: list) -> bool:
    """True if this role's first build is already up, so somebody else has it.

    Only meaningful for a tile the unit can actually see; an unobserved tile
    reads as empty and the role looks free. That is the right way round -- this
    is a guard against a late ordinary builder inheriting a finished role, and a
    late builder claiming on a spawn tile is standing right next to the build.
    """
    for op in prog:
        if op[0] == openers.BUILD:
            p = opener.pos(op[2])
            return map_info.type_at(p.x, p.y) is opener.build_kind(op[1])
    return False


def _claim() -> None:
    global _claimed, _role, _prog, _step, _barriers
    _claimed = True
    spec = opener.spec
    if spec is None:
        return
    now = rc.get_current_round()
    me = map_info._my_pos
    for role, prog in enumerate(spec["builders"]):
        if now > openers.claim_deadline(spec, role):
            continue
        if not _entry_matches(spec, role, prog, me):
            continue
        if _already_standing(spec, prog):
            continue
        if (prog and prog[0][0] == openers.WAIT
                and opener.pos(prog[0][1]) != me
                and not _throw_possible(opener.pos(prog[0][1]))):
            continue        # nothing here can throw us; this role is not ours
        _role, _prog, _step = role, list(prog), 0
        # Everything this role puts a barrier on, however it gets there: a plain
        # BUILD, or a STRIKE whose fallback is one. All of it wants guarding.
        owned = [op[2] for op in prog if op[0] == openers.BUILD and op[1] == "barrier"]
        owned += [op[1] for op in prog if op[0] == openers.STRIKE and op[2] == "barrier"]
        _barriers = tuple(opener.pos(t) for t in owned)
        log(f"OPENER role {role} claimed at {me}")
        return


def _unseen_ore():
    """Table ore tiles this builder has never actually looked at.

    "Never looked at" is `_bm_seen_observed`, not `_bm_seen`: a tile filled in by
    the symmetry solver or relayed by a teammate says nothing about whether a
    harvester is standing on it now.
    """
    tiles = opener.our_ore()
    if not tiles:
        return ()
    w = map_info._width
    observed = map_info._bm_seen_observed
    return tuple(t for t in tiles if not (observed >> (t.x + t.y * w)) & 1)


def _ore_worked() -> bool:
    """Is every ore tile the table calls ours being worked?

    False while any of them is unlooked-at -- the caller sends the builder to go
    and see rather than assuming either way.

    Latched once true, because "after we finish eco" is a phase and not an
    instant. Read live it is momentary: one harvester dies and it is false again,
    so only builders that happened to evaluate on a true turn ever went.
    """
    global _eco_done
    if _eco_done:
        return True
    tiles = opener.our_ore()
    if not tiles or _unseen_ore():
        return False
    for t in tiles:
        if map_info.type_at(t.x, t.y) is not EntityType.HARVESTER:
            return False
    _eco_done = True
    return True


def _scouting():
    """An ore tile to go and look at, or None.

    Only odd ids bother: they are the ones whose decision depends on the answer.
    An even-id builder has no reason to walk across the map to confirm something
    it will not act on.
    """
    if not opener.ferries_only(rc.get_id()) or _eco_done:
        return None
    unseen = _unseen_ore()
    if not unseen:
        return None
    me = map_info._my_pos
    return min(unseen, key=lambda t: me.distance_squared(t))


def _wants_ferry() -> bool:
    """Should this builder give up on the economy and go to the front?

    Every odd-id builder, once the economy has nothing left to build. The even
    ids stay home: the seal still has to be maintained and the harvesters still
    have to be repaired.

    Latched once taken. A builder part-way along a launcher chain is standing in
    a sealed room it cannot walk out of, so "do I still want to go" is not a
    question worth re-asking -- and `_ore_worked` can go false again the moment
    an enemy kills a harvester.
    """
    global _ferrying
    if _ferried or opener.spec is None:
        return False
    if _ferrying:
        return True
    # Anyone standing on a mid-chain landing tile continues the chain, volunteer
    # or not. A ferry launcher throws whatever is on its pickup square and cannot
    # ask why, so an ordinary builder that merely walked across one ends up in a
    # room walled on every side: the only way out is onward, and until it takes
    # it, it is sitting on the landing tile blocking every throw behind it. That
    # is what stalled the whole chain -- one uninvolved builder parked in the
    # middle and nothing else ever got across.
    for _, dst in opener.ferry_stops():
        if map_info._my_pos == dst and not opener.ferry_terminal(dst):
            _ferrying = True
            log("OPENER stranded mid-chain; carrying on across")
            return True
    if opener.spec.get("ferry_after") != "ore_worked":
        return False
    if not opener.ferries_only(rc.get_id()):
        return False
    if not _ore_worked():
        return False
    _ferrying = True
    log("OPENER joining the ferry queue")
    return True


def _ferry_route():
    """The (pickup, destination) of the nearest REACHABLE ferry, or None.

    Reachable, not nearest-by-distance. A chain's middle pickup tiles sit inside
    a room that is walled on every side -- the only way in is to be thrown there
    -- so straight-line distance will happily nominate one to a builder standing
    at home, which then walks at a wall for the rest of the game. `nav.closest`
    answers with a real path or nothing.
    """
    stops = opener.ferry_stops()
    if not stops:
        return None
    me = map_info._my_pos
    for src, dst in stops:
        if src == me:
            return src, dst           # already queued
    w = map_info._width
    mask = 0
    for src, _ in stops:
        mask |= 1 << (src.x + src.y * w)
    tile, _ = nav.closest(mask)
    if tile is None:
        return None
    for src, dst in stops:
        if src == tile:
            return src, dst
    return None


def _update_ferry_avoid() -> None:
    """Keep builders that are not going to the front off the ferry pickup tiles.

    A ferry launcher throws whatever stands on its pickup square; it has no way
    to ask whether that builder meant to go. So the filtering has to happen on
    the other side -- anyone not volunteering treats those tiles as impassable
    and paths around them.

    This is the root cause of the chain stalling. An uninvolved builder crossing
    a pickup tile gets thrown into a room walled on every side, and then sits on
    the landing tile blocking every throw behind it. The mid-chain "carry on
    across" rule clears one once it happens; this stops it happening.
    """
    mask = 0
    if (opener.spec is not None and not _ferrying and not _ferried
            and not opener.ferries_only(rc.get_id())):
        w = map_info._width
        for src, _ in opener.ferry_stops():
            mask |= 1 << (src.x + src.y * w)
    map_info._bm_avoid_extra = mask


def score():
    """MAX_SCORE while this builder owes the script ops, then while it has
    barriers to guard, then while it is on its way to the front."""
    global _ferried
    if not opener.verify():
        map_info._bm_avoid_extra = 0
        return 0
    if not _claimed:
        _claim()
    if _prog is not None and (_step < len(_prog) or _barriers):
        return MAX_SCORE
    # Landing on the LAST destination in the chain is how a builder learns it has
    # arrived; from then on it is an ordinary builder again, on the other side.
    # Mid-chain landings do not count -- those tiles are the next launcher's
    # pickup square, and stopping there would strand the builder in a sealed room.
    if not _ferried:
        for _, dst in opener.ferry_stops():
            if map_info._my_pos == dst and opener.ferry_terminal(dst):
                _ferried = True
                log(f"OPENER ferried across to {map_info._my_pos}")
                break
    wants = _wants_ferry()
    _update_ferry_avoid()          # after _wants_ferry: it is what sets _ferrying
    if wants and _ferry_route() is not None:
        return MAX_SCORE
    # Nothing to decide on yet. Go and look at the ore we have never seen, so the
    # decision gets made on an observation rather than on an assumption.
    if _scouting() is not None:
        return MAX_SCORE
    return 0


def _enemy_distance_sq(tile) -> int:
    """Distance to the nearest enemy builder we know about, or a large number.

    Enemy builders are what actually eat a barrier -- 2 HP a turn from an
    orthogonally adjacent tile -- so they are what "an opponent is closest to"
    means here. Only bots this unit can see or has been told about count, which
    is the right conservatism: a threat nobody has spotted cannot pull the guard
    off the barrier that is visibly being chewed on.
    """
    w = map_info._width
    best = 1 << 30
    mask = map_info._bm_enemy_bots
    while mask:
        lsb = mask & -mask
        mask ^= lsb
        n = lsb.bit_length() - 1
        dx = n % w - tile.x
        dy = n // w - tile.y
        d = dx * dx + dy * dy
        if d < best:
            best = d
    return best


def _guard_target():
    """The barrier to be standing next to: weakest first, then most threatened.

    A missing barrier scores 0 HP, so rebuilding always outranks bodyguarding.
    Ties keep the tile we are already on -- without that, two barriers at equal
    HP with no enemy in sight would swap the guard back and forth forever and it
    would never actually be adjacent to either.
    """
    best = None
    best_key = None
    for tile in _barriers:
        if map_info.type_at(tile.x, tile.y) is EntityType.BARRIER:
            hp = map_info._building_hp[tile.x + tile.y * map_info._width]
        else:
            hp = 0
        key = (hp, _enemy_distance_sq(tile))
        if best_key is None or key < best_key or (key == best_key and tile == _guard_tile):
            best_key = key
            best = tile
    return best


def _under_fire(p) -> bool:
    return bool(map_info._bm_enemy_turret_threat >> (p.x + p.y * map_info._width) & 1)


def _safe_posts(tile) -> set:
    """Cardinal neighbours of `tile` that can be stood on and are not shootable.

    Cardinal only, because heal and build reach is cardinal -- a diagonal
    neighbour would be a safe place to watch a barrier die from. Our own tile
    counts as standable (we are on it), exactly as `Pathing.move_adjacent` does.
    """
    posts = set()
    for d in map_info._CARDINAL:
        p = map_info.pos_add(tile, d)
        if not map_info.in_bounds(p):
            continue
        if p != map_info._my_pos and not map_info.is_passable(p):
            continue
        if _under_fire(p):
            continue
        posts.add(p)
    return posts


def _retreat() -> None:
    """Step off a tile an enemy turret covers, if anywhere adjacent is better."""
    my_pos = map_info._my_pos
    if not _under_fire(my_pos):
        return
    safe = {p for p in (map_info.pos_add(my_pos, d) for d in map_info._CARDINAL)
            if map_info.in_bounds(p) and map_info.is_passable(p) and not _under_fire(p)}
    if safe:
        nav.move_to(safe)


def _enemy_can_heal(tile) -> bool:
    """Is an enemy builder orthogonally next to `tile`, able to out-heal us?

    A builder heals 4 HP for 1 Ti and attacks for 2 damage at 2 Ti, so one enemy
    beside the target beats one of ours in front of it, twice over on both
    counts. Only a sentinel's 18 breaks that.
    """
    for d in map_info._CARDINAL:
        p = map_info.pos_add(tile, d)
        if not map_info.in_bounds(p):
            continue
        bot = rc.get_tile_builder_bot_id(p)
        if bot is not None and rc.get_team(bot) != map_info._my_team:
            return True
    return False


def _release(why: str) -> None:
    """Give this builder back to the ordinary economy.

    Not the same as `opener.abandon`, which writes the whole opener off for this
    unit because the map was wrong. This one is narrower: this particular builder
    is not the one the script meant, so it stops pretending to be and goes and
    does something useful. Nothing else about the opener is affected.
    """
    global _role, _prog, _step, _barriers
    log(f"OPENER role {_role} released: {why}")
    _role = None
    _prog = None
    _step = 0
    _barriers = ()


def _go_to_ferry() -> None:
    """Walk to the ferry's pickup tile and stand on it until thrown.

    Standing still on arrival is the whole protocol -- the launcher throws
    whatever is on that tile, and it is the only tile it will throw from.

    One job on the way: if the launcher that serves this pickup is the chain's
    own and is not up yet, the builder standing here is the one that builds it.
    That is how the middle staging post gets established at all -- nothing can
    walk into that room, so the first builder thrown in has to construct its own
    way onward.
    """
    route = _ferry_route()
    if route is None:
        return
    src = route[0]
    if map_info._my_pos != src:
        if has_op():
            nav.move_to(src)
        return
    site = opener.ferry_launcher_for(src)
    if site is not None:
        if has_op() and rc.can_build_launcher(site):
            rc.build_launcher(site)
            map_info.update_at(site)
            log(f"OPENER chain launcher built at {site}")
        return
    # In the queue; leave the turn free so nothing moves us off the tile.


def _guard():
    """Hold station on the barriers this role built, and rebuild what dies."""
    global _guard_tile
    tile = _guard_target()
    _guard_tile = tile
    if tile is None:
        return
    my_pos = map_info._my_pos
    posts = _safe_posts(tile)
    if not posts:
        # Every way of reaching this barrier is covered by a turret. Guarding it
        # would cost the guard for nothing, so back out of the fire and wait --
        # turret facings change, and the threat mask with them.
        _retreat()
        return
    if my_pos not in posts:
        nav.move_to(posts)
        return

    # In position and safe. If the barrier still stands, leave the turn unspent:
    # builder.run()'s trailing heal._do_best_heal() is the healer, and moving
    # this turn would make its can_heal() gate fail.
    if map_info.type_at(tile.x, tile.y) is EntityType.BARRIER:
        return
    if has_op() and rc.can_build_barrier(tile):   # no reserve, as for the script
        rc.build_barrier(tile)
        map_info.update_at(tile)
        log(f"OPENER role {_role} rebuilt barrier at {tile}")


def run():
    """Advance the program.

    Ops that need no turn (a WAIT already satisfied, a GOTO already arrived) fall
    through to the next one in the same turn, so a builder that lands on its
    waypoint starts walking the moment it can rather than a turn later.
    """
    global _step
    if _prog is None or _step >= len(_prog):
        if _prog is not None and _barriers:
            _guard()
        elif _wants_ferry():
            _go_to_ferry()
        else:
            tile = _scouting()
            if tile is not None and has_op():
                nav.move_adjacent(tile)
        return
    while _step < len(_prog):
        op = _prog[_step]
        kind = op[0]

        if kind == openers.WAIT:
            # Hold absolutely still: this tile is a launcher's pickup square and
            # wandering off it would strand the throw. Note this is also why the
            # opener has to suppress builder.run()'s trailing heal.
            dst = opener.pos(op[1])
            if map_info._my_pos == dst:
                _step += 1
                continue
            if not _throw_possible(dst):
                # The launcher that was going to do this is gone. Waiting is no
                # longer waiting for anything, so hand the builder back to the
                # ordinary economy rather than have it stand here for the game.
                _release("no launcher can make the throw")
            break

        if kind == openers.GOTO:
            dst = opener.pos(op[1])
            if map_info._my_pos == dst:
                _step += 1
                continue
            if not has_op():
                break
            nav.move_to(dst)
            break

        if kind == openers.BUILD:
            tile = opener.pos(op[2])
            my_pos = map_info._my_pos
            if abs(tile.x - my_pos.x) + abs(tile.y - my_pos.y) != 1:
                # Only reachable if a GOTO was preempted or the bot was pushed;
                # the scripts name the standing tile explicitly because
                # move_adjacent is free to pick the wrong side of a wall.
                if has_op():
                    nav.move_adjacent(tile)
                break
            # No reserve of any kind on a scripted build. `can_build` already
            # refuses when the titanium is not there, and every other gate --
            # map_info.ti_reserve()'s flat 40, and the opener's own barrier
            # buffer -- exists to stop OTHER spending from eating the barrier
            # budget. Applying either here would be the budget refusing to buy
            # the thing it is a budget for.
            extra = opener.facing(op[3]) if len(op) > 3 else None
            if not (has_op() and rc.can_build(opener.build_kind(op[1]), tile, extra)):
                break
            rc.build(opener.build_kind(op[1]), tile, extra)
            map_info.update_at(tile)
            log(f"OPENER role {_role} built {op[1]} at {tile}"
                + (f" facing {extra}" if extra else ""))
            _step += 1
            break

        if kind == openers.SKIP_UNLESS_ENEMY:
            tile = opener.pos(op[1])
            bid = rc.get_tile_building_id(tile)
            if bid is not None and rc.get_team(bid) != map_info._my_team:
                _step += 1
            else:
                _step += 1 + op[2]
                log(f"OPENER role {_role} skips {op[2]} op(s): {tile} is clear")
            continue

        if kind == openers.STRIKE:
            tile = opener.pos(op[1])
            my_pos = map_info._my_pos
            if abs(tile.x - my_pos.x) + abs(tile.y - my_pos.y) != 1:
                if has_op():
                    nav.move_adjacent(tile)
                break
            bid = rc.get_tile_building_id(tile)
            if bid is not None and rc.get_team(bid) != map_info._my_team:
                # Hitting it alone while an enemy builder stands beside it is
                # worse than doing nothing: they restore 4 HP for 1 Ti and we
                # remove 2 for 2. Hold instead and let the scripted sentinel's
                # 18 land -- it is holding fire for a builder to be adjacent, so
                # our own hit completes the 20 the same turn.
                if _enemy_can_heal(tile):
                    break
                if has_op() and rc.can_fire(tile):
                    rc.fire(tile)
                    map_info.update_at(tile)
                break
            if bid is not None:
                _step += 1              # already ours; the tile is held
                continue
            if not (has_op() and rc.can_build(opener.build_kind(op[2]), tile)):
                break
            rc.build(opener.build_kind(op[2]), tile)
            map_info.update_at(tile)
            log(f"OPENER role {_role} took {tile} with {op[2]}")
            _step += 1
            break

        if kind == openers.ASSAULT:
            stand = opener.pos(op[1])
            target = opener.pos(op[2])
            if map_info._my_pos != stand:
                if has_op():
                    nav.move_to(stand)
                break
            # Terminal: never advances, so this builder never reaches guarding.
            if has_op() and rc.can_fire(target):
                rc.fire(target)
                map_info.update_at(target)
            break

        log(f"OPENER unknown op {kind!r}")
        opener.abandon(f"unknown op {kind!r}")
        break

    # No stall counter and no timeout: an op that will not go this turn is
    # retried next turn, for as long as that takes. See the note in openers.py --
    # the usual reason is a shortage, not an impossibility, and giving up threw
    # away whole programs over a few turns of being poor.
    return


def holding() -> bool:
    """True when this builder is deliberately spending its turn on nothing.

    `builder.run()` offers an unspent turn to `heal._do_best_heal()`, which would
    quietly turn a WAIT into a heal. Harmless for the heal, fatal for the script:
    the tile it is standing on is a launcher's pickup square and the whole point
    of the WAIT is that the turn stays free and the builder stays put.
    """
    return (_prog is not None and _step < len(_prog)
            and _prog[_step][0] == openers.WAIT)
