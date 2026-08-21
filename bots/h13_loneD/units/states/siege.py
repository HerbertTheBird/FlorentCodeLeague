"""Builder state: seal the enemy core's delivery ring with barriers.

The enemy core is 2x2. Delivery into it is a DIRECTED, CARDINAL push: a conveyor
must sit on one of the EIGHT cardinally-adjacent tiles and face into a core tile
(a harvester on such a tile delivers with no conveyor at all). The four diagonal
corners cannot deliver. So barriering those eight tiles cuts their economy at the
sink, completely -- unlike walling one ore tile, which costs them one harvester
site.

Why this is scored on COMPLETION rather than per-tile value: two earlier attempts
at travelling-barrier play both failed by scattering.
  * core-ring barriering, herbert4 era: +0.0012 -- a wash, 9 maps won / 7 lost,
    winning on big maps and losing on small ones because preferring the ring
    OVERRODE distance ordering and sent builders across the map.
  * disrupt raised from 2 to 6 (wall distant ore): won its head-to-head 40-26 and
    lost 4.6 points across the real suite -- "builders pulled off routing to go
    wall distant ore are not paying for themselves".
A half-sealed ring is worth nothing: they deliver through any remaining gap. So
the score RISES as the ring nears completion, which makes a builder finish a ring
it has started and makes an untouched ring unattractive unless it is close.
"""
import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import *
from log import log

rc: Controller = None
nav: Pathing = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


# Ceiling. Sits ABOVE chip (3.9) and chase (3.5) but BELOW harvest (7) and route
# (8): sealing their sink must never outrank feeding our own core, which is the
# mistake both previous travelling-barrier attempts made.
MAX_SCORE = 6.0
# Below this fraction sealed, an untouched ring is not worth crossing the map for.
MIN_PROGRESS = 0.25

_cached_target = None


# How many turns of head start the enemy may have and we still commit.
# A barrier is BARRIER_MAX_HP=30 against BUILDER_BOT_ATTACK_DAMAGE=2, so removing
# one costs them 15 builder-turns. We can therefore afford to lose the footrace
# by up to that much and the barrier still costs them more than it costs us --
# 3 Ti against 15 builder-turns.
RACE_MARGIN = 0

# Measured, 24-game screens vs herbert11 on a fixed 12-map set, with an UNGATED
# control run on the SAME maps:
#     margin  15 (loose)   +0.2302  == the ungated control EXACTLY (never binds)
#     margin   0 (this)    +0.4026  <- peak
#     margin  -3           +0.0282
#     margin  -6           +0.0729
#     ungated control      +0.2302
# Both flanks measured, so this is a peak rather than one lucky cell. The
# loose-margin arm matching the control exactly is a clean negative control: the
# gain comes from the gate BINDING, not from incidental code changes.
#
# Full panel, 86 games each:
#     vs herbert11      47-39  +0.1130 se 0.1028
#     vs Tyr_Jython     71-15  +0.6718  (herbert11 +0.6928)
#     vs V6_earlysiege  65-21  +0.4993  (herbert11 +0.4540)
#
# Also tested and WORSE: gate it, then relax everything else (MAX_SCORE 7.5,
# MIN_PROGRESS 0, leash 14) = +0.0891. The gate should NARROW the play, not
# license widening it elsewhere.


def _win_the_race(tile_bit: int, my_dist: int) -> bool:
    """True if we can place a barrier on `tile_bit` before the enemy can undo it.

    Compares our travel distance against the nearest ENEMY builder's travel
    distance to the same tile, allowing RACE_MARGIN turns of slack for the
    builder-turns it costs them to break the barrier once placed.

    This is the gate the static sweep lacked: MAX_SCORE / MIN_PROGRESS / travel
    leash all decide WHETHER to want a tile, none of them decide whether the
    attempt will actually stick. A barrier placed into a tile the enemy reaches
    first is 3 Ti and a walk, wasted.
    """
    enemies = map_info._bm_enemy_bots
    if not enemies:
        return True                    # nobody to contest it
    _, enemy_dist = nav.closest(enemies, pos=tile_bit, to_adjacent=True)
    if enemy_dist < 0:
        return True                    # they cannot reach it at all
    return my_dist <= enemy_dist + RACE_MARGIN


def _ring_tiles() -> int:
    """The eight cardinally-adjacent tiles of the enemy 2x2 core.

    manhattan() minus the core itself gives exactly the cardinal ring; the four
    diagonal corners are excluded automatically because they are not within
    Manhattan distance 1 of any core tile.
    """
    core = map_info._bm_their_core_area
    if not core:
        return 0
    return map_info.manhattan(core) & ~core & map_info._board_mask


def _open_ring() -> int:
    """Ring tiles that are still empty -- i.e. still able to carry a delivery."""
    return _ring_tiles() & ~map_info._bm_any_building


def _progress() -> float:
    """Fraction of the ring already denied (barriered or otherwise built on)."""
    ring = _ring_tiles()
    n = ring.bit_count()
    if not n:
        return 0.0
    return 1.0 - (_open_ring().bit_count() / n)


def score(can_move=True):
    global _cached_target
    _cached_target = None
    if rc.get_global_resources() < rc.get_barrier_cost() + map_info.ti_reserve():
        return 0
    open_ring = _open_ring()
    if not open_ring:
        return 0                      # already sealed, or no known enemy core
    # Never stand in turret fire to place a barrier: a builder loses that trade
    # badly (a sentinel deals 9/turn against a 40 HP builder healing 4).
    open_ring &= ~map_info._bm_enemy_turret_threat
    if not open_ring:
        return 0
    claims = pathing.claim_subset(
        1 << (map_info._my_pos.x + map_info._my_pos.y * map_info._width),
        map_info._bm_friendly_bots, open_ring, tie_self=True)
    if not claims:
        return 0
    if not can_move:
        my = map_info._my_pos
        for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            p = Position(my.x + d[0], my.y + d[1])
            if not map_info.in_bounds(p):
                continue
            if (claims >> (p.x + p.y * map_info._width)) & 1 and rc.can_build_barrier(p):
                _cached_target = p
                return MAX_SCORE
        return 0
    target, dist = nav.closest(claims, to_adjacent=True)
    if target is None:
        return 0
    # Only commit if the barrier will survive long enough to matter.
    if not _win_the_race(1 << (target.x + target.y * map_info._width), dist):
        return 0
    _cached_target = target
    prog = _progress()
    if prog < MIN_PROGRESS:
        # Untouched ring: only worth it if we are already close by, so this never
        # drags a builder off routing to cross the map.
        return MAX_SCORE * 0.5 if dist <= 10 else 0
    # Rises toward MAX_SCORE as the ring nears completion -- finish what is
    # started rather than scattering barriers over several rings.
    return MAX_SCORE * (0.5 + 0.5 * prog)


def run(can_move=True):
    target = _cached_target
    if target is None:
        return
    log("SIEGE", target)
    if nav.move_adjacent(target, can_move=can_move):
        return
    if rc.can_build_barrier(target):
        rc.build_barrier(target)
