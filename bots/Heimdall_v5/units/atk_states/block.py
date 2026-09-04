"""BLOCK state — shield a friendly gunner from an enemy gunner's line of fire.

If this attack builder can see an enemy gunner that is currently aimed at one of
our gunners, and there is an empty tile on that firing line where a barrier would
break the shot, the builder goes and drops a barrier there. This is the highest-
priority attack-builder behaviour.

Skipped for a threatened gunner that is itself aimed back at an enemy gunner —
that's a mutual duel we deliberately leave open rather than walling off our own
gunner's return fire.
"""

import map_info
import units.builder
from pathing import Pathing
from fcode import Controller, Position
from log import log

rc: Controller = None
nav: Pathing = None

MAX_SCORE = 10          # above attack (9) — blocking a gunner duel comes first
target: Position | None = None

_barrier_tiles = 0      # candidate tiles cached by score() for run()


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _ray_scan(n: int, di: int, my_g: int, enemy_g: int, blockers: int, walls: int):
    """Walk the gunner firing line from tile `n` facing dir index `di`.

    Returns (kind, obstruction_tile, empties) where kind is 'mine' / 'enemy' /
    'other' for the first obstruction, and `empties` is the bitmask of empty
    tiles crossed before it. Returns None if the ray leaves the board, hits a
    wall, or reaches its end without obstruction."""
    w = map_info._width
    h = map_info._height
    gx, gy = n % w, n // w
    empties = 0
    for dx, dy in map_info._GUNNER_RAYS[di]:
        x, y = gx + dx, gy + dy
        if not (0 <= x < w and 0 <= y < h):
            return None
        t = x + y * w
        bit = 1 << t
        if walls & bit:
            return None
        if my_g & bit:
            return "mine", t, empties
        if enemy_g & bit:
            return "enemy", t, empties
        if blockers & bit:
            return "other", t, empties
        empties |= bit
    return None


def _compute() -> int:
    global _barrier_tiles
    _barrier_tiles = 0

    my_idx = map_info._my_team_idx
    enemy_idx = 1 - my_idx
    gun = map_info._IDX_GUNNER
    my_g = map_info._bm_et[gun] & map_info._bm_team[my_idx]
    enemy_g = map_info._bm_et[gun] & map_info._bm_team[enemy_idx]
    if not my_g or not enemy_g:
        return 0
    # Only react to an aggressor we can actually see (fresh facing).
    seen_enemy_g = enemy_g & map_info._bm_visible
    if not seen_enemy_g:
        return 0

    walls = map_info._bm_env[map_info._IDX_ENV_WALL]
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    bots = map_info._bm_friendly_bots | map_info._bm_enemy_bots
    blockers = map_info._bm_any_building | bots
    dirs = map_info._building_dir

    candidates = 0
    m = seen_enemy_g
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        di = dirs[n]
        if not (0 <= di < 8):
            continue
        res = _ray_scan(n, di, my_g, enemy_g, blockers, walls)
        if res is None or res[0] != "mine":
            continue                       # not aimed at one of our gunners
        _, mg_n, empties = res
        if not empties:
            continue                       # no room to interpose a barrier

        # Mutual-duel exception: if the threatened gunner is aimed back at an
        # enemy gunner, leave the lane open for its return fire.
        mdi = dirs[mg_n]
        if 0 <= mdi < 8:
            mres = _ray_scan(mg_n, mdi, my_g, enemy_g, blockers, walls)
            if mres is not None and mres[0] == "enemy":
                continue

        candidates |= empties

    # Barriers only go on plain empty ground (never ore); can_build_barrier is
    # the final authority when we get there.
    candidates &= ~ore
    if not candidates:
        return 0
    _barrier_tiles = candidates
    return MAX_SCORE


def score() -> int:
    return _compute()


def run():
    global target
    log("BLOCK")
    tiles = _barrier_tiles
    if not tiles:
        return
    best, _ = nav.closest(tiles)
    if best is None:
        return
    target = best
    nav.move_adjacent(best)
    if (
        rc.can_build_barrier(best)
        and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.builder_ti_reserve()
    ):
        rc.build_barrier(best)
        map_info.update_at(best)
