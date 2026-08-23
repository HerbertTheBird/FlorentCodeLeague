import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import *
from log import log
import random

rc: Controller = None
nav: Pathing = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


# ----------------------------------------------------------------------------
# Block: drop a barrier on a tile that chokes the enemy economy. A choke tile is
# a barrierable (empty) tile that is any of
#   (a) the OUTPUT target of an enemy conveyor,
#   (b) a harvester JUNCTION -- adjacent to an enemy conveyor AND adjacent to an
#       ORPHAN enemy harvester (one with no enemy conveyor adjacent to it): the
#       empty tile that would wire that harvester onto the belt. A harvester
#       already served by a belt is not worth blocking, so it is excluded.
#   (c) an ORE tile adjacent to an enemy conveyor (barrier it so they can't drop a
#       harvester there to feed the belt), or
#   (d) cardinally adjacent to the ENEMY core.
# Validity is the same shape as chip's: I must be able to REACH the tile (get
# cardinally adjacent, so I can place the barrier) no later than the nearest enemy
# builder could -- "be adjacent before they can be adjacent."
#
# Two tiers, like chip:
#   * a choke already cardinally adjacent to me -> place it now, MAX_SCORE, no move;
#   * a choke I can win the reach-race to -> walk toward it at WALK_SCORE.
# ----------------------------------------------------------------------------
MAX_SCORE = 10          # tier A: a choke is already adjacent -> place, never move
WALK_SCORE = 5.8        # tier B: a choke I can beat the enemy to -> walk to it
_REACH_CAP = 12         # how far I'll consider walking to a choke

_cached_target = None   # tier A: adjacent choke tile to barrier this turn (no move)
_cached_walk = None     # tier B: choke tile to walk toward and barrier


def _barrierable() -> int:
    """Empty tiles a barrier can be placed on (no building/conveyor, no wall)."""
    return (map_info._board_mask
            & ~map_info._bm_any_building
            & ~map_info._bm_env[map_info._IDX_ENV_WALL])


def _block_tiles() -> int:
    """Barrierable tiles that choke the enemy economy (see module note)."""
    enemy = map_info._bm_team[1 - map_info._my_team_idx]
    enemy_conv = map_info._bm_et[map_info._IDX_CONVEYOR] & enemy
    enemy_harv = map_info._bm_et[map_info._IDX_HARVESTER] & enemy
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]

    adj_conv = map_info.expand_manhattan(enemy_conv)        # enemy conveyors + neighbours
    conv_targets = map_info._conveyor_target_tiles(enemy_conv)
    adj_core = map_info.expand_manhattan(map_info._bm_their_core_area)

    # Harvester junction: a tile touching an enemy conveyor AND an ORPHAN harvester
    # (a harvester with no enemy conveyor adjacent to it). It's the empty tile that
    # would connect that harvester to the belt; a harvester already on a belt is not
    # blocked. Buildings drop out via _barrierable(), leaving the empty junction.
    orphan_harv = enemy_harv & ~adj_conv
    harv_junction = map_info.expand_manhattan(orphan_harv) & adj_conv

    # Ore one step from an enemy conveyor -- deny it as a harvester spot.
    ore_targets = ore & adj_conv

    return (conv_targets | harv_junction | ore_targets | adj_core) & _barrierable()


def _dist_field(sources: int, passable: int, cap: int) -> dict:
    """tile index -> BFS moves from the nearest source over `passable`, up to `cap`.
    Sources are dist 0; tiles beyond `cap` are absent."""
    out = {}
    m = sources
    while m:
        b = m & -m
        m ^= b
        out[b.bit_length() - 1] = 0
    frontier = sources
    visited = sources
    for d in range(1, cap + 1):
        frontier = map_info.expand_manhattan(frontier) & passable & ~visited
        if not frontier:
            break
        visited |= frontier
        m = frontier
        while m:
            b = m & -m
            m ^= b
            out[b.bit_length() - 1] = d
    return out


def _reach_to_adjacent(field: dict, n: int, w: int, h: int, passable: int):
    """Fewest moves in `field` to a passable cardinal neighbour of tile n (the tiles
    from which one can act on n), or None if none is within the field."""
    x, y = n % w, n // w
    best = None
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < w and 0 <= ny < h:
            nn = nx + ny * w
            if (passable >> nn) & 1 and nn in field:
                d = field[nn]
                if best is None or d < best:
                    best = d
    return best


def _adjacent_choke_now(choke: int):
    """A choke tile cardinally adjacent to me that I can barrier right now, or None."""
    w, h = map_info._width, map_info._height
    my = map_info._my_pos
    hits = []
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        x, y = my.x + dx, my.y + dy
        if 0 <= x < w and 0 <= y < h and (choke >> (x + y * w)) & 1:
            p = Position(x, y)
            if rc.can_build_barrier(p):
                hits.append(p)
    return random.choice(hits) if hits else None


def score(can_move=True):
    global _cached_target, _cached_walk
    _cached_target = None
    _cached_walk = None
    # Must be able to actually afford the barrier (cost + defender reserve) -- the
    # same gate run() places under -- or a positive score just parks a builder that
    # can never build.
    if rc.get_global_resources() < rc.get_barrier_cost() + map_info.ti_reserve():
        return 0
    choke = _block_tiles()
    if not choke:
        return 0

    # Tier A: a choke is already one step away -> place it now, no move, top priority.
    adj = _adjacent_choke_now(choke)
    if adj is not None:
        _cached_target = adj
        return MAX_SCORE

    if not can_move:
        return 0

    # Tier B: walk to a choke I can be adjacent to no later than the nearest enemy.
    w, h = map_info._width, map_info._height
    passable = map_info.passable()
    my = map_info._my_pos
    my_field = _dist_field(1 << (my.x + my.y * w), passable, _REACH_CAP)
    enemy_bots = map_info._bm_enemy_bots
    enemy_field = _dist_field(enemy_bots, passable, _REACH_CAP) if enemy_bots else {}

    best_tile = None
    best_reach = None
    m = choke
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        my_reach = _reach_to_adjacent(my_field, n, w, h, passable)
        if my_reach is None:
            continue                        # I can't get adjacent within the cap
        enemy_reach = _reach_to_adjacent(enemy_field, n, w, h, passable)
        # Win the race: I must be adjacent no later than the nearest enemy. If no
        # enemy can reach it within the cap, I win by default.
        if enemy_reach is not None and my_reach > enemy_reach:
            continue
        if best_reach is None or my_reach < best_reach:
            best_reach = my_reach
            best_tile = Position(n % w, n // w)
    if best_tile is not None:
        _cached_walk = best_tile
        return WALK_SCORE
    return 0


def run(can_move=True):
    # Tier A: place on the adjacent choke, never move.
    if _cached_target is not None:
        p = _cached_target
        log("BLOCK")
        if (rc.can_build_barrier(p)
                and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.ti_reserve()):
            rc.build_barrier(p)
            map_info.update_at(p)
        return

    # Tier B: walk toward the choke and barrier it once we're adjacent.
    t = _cached_walk
    if t is None:
        return
    log("BLOCK-WALK", t)
    if nav.move_adjacent(t, can_move=can_move):
        return                              # still moving into position
    if (rc.can_build_barrier(t)
            and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.ti_reserve()):
        rc.build_barrier(t)
        map_info.update_at(t)
