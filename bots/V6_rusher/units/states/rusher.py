"""Builder state: one designated builder that goes and strangles the enemy core.

One builder, chosen at birth, walks to the enemy core and executes a fixed
three-phase plan. Everything about it is deliberate:

  1. **Seal the delivery ring.** An enemy core is 2x2 and exactly eight cardinal
     tiles can feed it, so eight barriers at 3 Ti each cut every conveyor route
     into it permanently. We already know this works from the receiving end: the
     economy audit found that when an opponent closes OUR ring, the share of our
     conveyors still reaching our core drops to zero on the turn the eighth tile
     closes and never recovers -- on quarry we then held 155 conveyors that
     delivered nothing for 300 rounds, and 8 of 10 long games against our own
     champions ended with zero harvesters built after round 200.

  2. **Put a sentinel on the last tile instead of a barrier.** A sentinel does 18
     damage on a 2-round cooldown -- 9 a round into a 500 HP core, so about 56
     rounds -- and range 5 means a ring tile is comfortably in range. The last
     open ring tile is the ideal site: it is adjacent to the core, and spending
     it on a turret rather than a barrier costs nothing, because the other seven
     barriers already deny the seven routes it would have denied.

  3. **Wall the sentinel in.** Builders can only fire at an orthogonally
     adjacent tile, so a sentinel whose free cardinal neighbours are barriers
     cannot be attacked at all -- an enemy builder has to chew through 30 HP of
     barrier at 2 damage a turn just to reach a tile it can shoot from. A ring
     tile has at most two free neighbours (the other two are a core tile and
     another ring tile we already sealed), so 6 Ti makes a 30 Ti turret
     effectively untouchable.

The whole structure costs roughly 7 barriers + 1 sentinel + 2 barriers = 51 Ti
plus scaling, and it both stops their economy and kills their core.

Why exactly one builder: four separate attempts to move MORE builder-turns
toward the enemy core have all lost (round-gated rush 43.9%, near-side-only
55.0% unrated, harvester-gated 71.2%, and the economy audit's whole F1/F6/F7
family at 41-45%). The lesson is that the marginal builder is worth more at home
than abroad -- which is an argument about the marginal builder, not the first
one. This commits precisely one, from turn one, and leaves the rest alone.
"""
import map_info
import units.builder
from fcode import Controller, Direction, EntityType, Position
from log import log
from pathing import Pathing

rc: Controller = None
nav: Pathing = None

# Above cut (13) so the designated builder is never pulled off the plan by the
# ordinary map-wide rush, and below defend's SIEGE_SCORE (20) so a turret
# actually shooting our own core still outranks it. Every other builder gets
# score 0 here on the first line of score(), so the ordering costs them nothing.
MAX_SCORE = 14

# Set by units/builder.py on the first turn of the first builder spawned.
am_rusher = False

# A sentinel reaches 5 tiles along a cardinal and ignores line of sight
# entirely, so any buildable tile sharing a row or column with a core tile
# within five steps is a valid firing position -- the ring is merely the closest.
SENTINEL_REACH = 5

_cached: tuple | None = None      # (kind, target, extra)


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _ring() -> int:
    """The eight cardinal tiles that can deliver into the enemy core."""
    core = map_info._bm_their_core_area
    if not core:
        return 0
    return map_info.expand_manhattan(core) & ~core & map_info._board_mask


def _buildable(mask: int) -> int:
    """Tiles in `mask` we could actually put something on."""
    return (mask
            & map_info._bm_seen
            & ~map_info._bm_any_building
            & ~map_info._bm_env[map_info._IDX_ENV_WALL]
            & ~map_info._bm_enemy_bots)


def _toward_core(pos: Position):
    """Cardinal direction from `pos` into the enemy core, or None."""
    core = map_info._bm_their_core_area
    if not core:
        return None
    w = map_info._width
    for d, (dx, dy) in zip(map_info._DIRECTIONS, map_info._DIRECTION_DELTAS_I):
        if dx and dy:
            continue                      # cardinal only: a diagonal line of 4 is shorter
        nx, ny = pos.x + dx, pos.y + dy
        if not map_info.in_bounds_coords(nx, ny):
            continue
        if core & (1 << (nx + ny * w)):
            return d
    return None


def _my_planted_sentinel() -> Position | None:
    """Our sentinel in firing position on the enemy core, if we have planted one.

    Searches everything within the sentinel's cardinal reach of the core, not
    just the ring. The first version only looked at ring tiles, so when phase 2
    fell back to an off-ring firing position the check could not see its own
    sentinel and phase 2 fired again -- measured, 2 sentinels on quarry.
    """
    core = map_info._bm_their_core_area
    if not core:
        return None
    near = core
    for _ in range(SENTINEL_REACH):
        near = map_info.expand_manhattan(near)
    mine = (map_info._bm_et[map_info._IDX_SENTINEL]
            & map_info._bm_team[map_info._my_team_idx]
            & near)
    for pos in map_info.iter_mask(mine):
        return pos
    return None





def _sentinel_site(open_ring: int):
    """(tile, facing) for a sentinel that can shoot the enemy core, or (None, None).

    Prefers a free ring tile, since that denies a delivery route as well as
    shooting, and falls back to any tile on a cardinal from the core.
    """
    for pos in map_info.iter_mask(open_ring):
        facing = _toward_core(pos)
        if facing is not None:
            return pos, facing

    core = map_info._bm_their_core_area
    if not core:
        return None, None
    w = map_info._width
    my_pos = map_info._my_pos
    best = None
    best_key = None
    for cpos in map_info.iter_mask(core):
        for d, (dx, dy) in zip(map_info._DIRECTIONS, map_info._DIRECTION_DELTAS_I):
            if dx and dy:
                continue
            for step in range(1, SENTINEL_REACH + 1):
                nx, ny = cpos.x + dx * step, cpos.y + dy * step
                if not map_info.in_bounds_coords(nx, ny):
                    break
                bit = 1 << (nx + ny * w)
                if not _buildable(bit):
                    continue
                cand = Position(nx, ny)
                # Face back down the same cardinal, toward the core.
                facing = _opposite(d)
                key = (my_pos.distance_squared(cand), nx + ny * w)
                if best_key is None or key < best_key:
                    best, best_key = (cand, facing), key
    if best is None:
        return None, None
    return best


def _opposite(d):
    dx, dy = d.delta()
    for od, (odx, ody) in zip(map_info._DIRECTIONS, map_info._DIRECTION_DELTAS_I):
        if odx == -dx and ody == -dy:
            return od
    return d


def _plan():
    """(kind, target, extra) for this turn, or None when there is nothing to do."""
    ring = _ring()
    if not ring:
        return None

    # Phase 3 first: a planted sentinel that can still be walked up to and shot
    # is the thing most likely to be lost, and it is the piece doing the damage.
    sent = _my_planted_sentinel()
    if sent is not None:
        w = map_info._width
        sbit = 1 << (sent.x + sent.y * w)
        exposed = _buildable(map_info.expand_manhattan(sbit) & ~sbit)
        best = None
        for pos in map_info.iter_mask(exposed):
            if best is None or map_info._my_pos.distance_squared(pos) < map_info._my_pos.distance_squared(best):
                best = pos
        if best is not None:
            return ("wall", best, None)
        return None                       # sentinel is sealed in; job done

    open_ring = _buildable(ring)
    count = open_ring.bit_count()
    my_pos = map_info._my_pos

    # Phase 2. Triggered on "one or none left" rather than exactly one, and with
    # a fallback site off the ring, because the first version keyed on count == 1
    # and the sentinel then landed on only 2 of 6 maps: the OTHER builders seal
    # ring tiles too (cut, score 13), so the count routinely steps 2 -> 0 and the
    # trigger is simply never observed. Never gate a plan on a counter another
    # actor also decrements.
    if count <= 1:
        site, facing = _sentinel_site(open_ring)
        if site is not None:
            return ("sentinel", site, facing)

    if not open_ring:
        return None
    best = None
    for pos in map_info.iter_mask(open_ring):
        if best is None or my_pos.distance_squared(pos) < my_pos.distance_squared(best):
            best = pos
    if best is None:
        return None
    # Phase 1.
    return ("barrier", best, None)


def score():
    global _cached
    _cached = None
    if not am_rusher:
        return 0
    _cached = _plan()
    return MAX_SCORE if _cached is not None else 0


def run():
    kind, target, extra = _cached
    log(f"RUSHER {kind} -> {target}")
    my_pos = map_info._my_pos
    adjacent = abs(target.x - my_pos.x) + abs(target.y - my_pos.y) == 1

    if not adjacent:
        nav.move_adjacent(target)
        return

    ti = rc.get_global_resources()
    if kind == "sentinel":
        if rc.can_build_sentinel(target, extra) and ti >= rc.get_sentinel_cost():
            log(f"RUSHER: sentinel on their ring at {target} facing {extra}")
            rc.build_sentinel(target, extra)
            map_info.update_at(target)
        return

    # Deliberately ignores map_info.ti_reserve() and defense.may_wall(): the
    # reserve funds a defender spawn and the wall budget is about not flooding
    # the map with barriers, and neither applies to the nine specific tiles that
    # decide whether the enemy core keeps working.
    if rc.can_build_barrier(target) and ti >= rc.get_barrier_cost():
        rc.build_barrier(target)
        map_info.update_at(target)
