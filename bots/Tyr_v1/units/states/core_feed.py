"""Near-core caretaker maintenance and passive core-feed construction."""

from main import has_op
from fcode import Controller, Position

import map_info
import units.builder
from log import log


rc: Controller = None
nav = None

# Broken inward feeds are real route damage and sit alongside urgent healing.
# Empty feed tiles are only filled after every other state declines the turn.
MAX_SCORE = 12
PASSIVE_SCORE = 1.5

_cached: tuple[str, Position, object] | None = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _feed_specs() -> tuple[tuple[Position, object], ...]:
    """Eight outside tiles that can deliver cardinally into our 2x2 core."""
    core = map_info._bm_my_core_area
    if not core:
        return ()
    w = map_info._width
    specs = {}
    for core_pos in map_info.iter_mask(core):
        for outward in map_info._CARDINAL:
            tile = map_info.pos_add(core_pos, outward)
            if not map_info.in_bounds(tile):
                continue
            n = tile.x + tile.y * w
            bit = 1 << n
            if core & bit:
                continue
            specs[n] = (tile, outward.opposite())
    return tuple(specs[n] for n in sorted(specs))


def score():
    global _cached
    _cached = None
    if not units.builder._stay_near_core or not has_op():
        return 0
    if rc.get_global_resources() < rc.get_conveyor_cost() + map_info.ti_reserve():
        return 0

    my_idx = map_info._my_team_idx
    my_conveyors = map_info._bm_conveyors & map_info._bm_team[my_idx]
    core = map_info._bm_my_core_area
    repair = []
    empty = []
    for tile, inward in _feed_specs():
        n = tile.x + tile.y * map_info._width
        bit = 1 << n
        if my_conveyors & bit:
            if (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & bit:
                continue
            target_n = map_info._building_conv_target[n]
            if target_n < 0 or not (core & (1 << target_n)):
                repair.append((tile, inward))
        elif (map_info._bm_seen & bit
              and not (map_info._bm_any_building & bit)
              and not (map_info._bm_env[map_info._IDX_ENV_WALL] & bit)
              and not ((map_info._bm_friendly_bots | map_info._bm_enemy_bots) & bit)):
            empty.append((tile, inward))

    my_pos = map_info._my_pos
    if repair:
        tile, inward = min(repair, key=lambda item: (
            my_pos.distance_squared(item[0]),
            item[0].x + item[0].y * map_info._width,
        ))
        _cached = ("repair", tile, inward)
        return MAX_SCORE
    if empty:
        tile, inward = min(empty, key=lambda item: (
            my_pos.distance_squared(item[0]),
            item[0].x + item[0].y * map_info._width,
        ))
        _cached = ("fill", tile, inward)
        return PASSIVE_SCORE
    return 0


def run():
    if _cached is None:
        return
    kind, tile, inward = _cached
    my_pos = map_info._my_pos
    adjacent = abs(tile.x - my_pos.x) + abs(tile.y - my_pos.y) == 1
    if not adjacent:
        nav.move_adjacent(tile, avoid_turret=False)
        return

    if kind == "repair" and rc.can_destroy(tile):
        rc.destroy(tile)
        map_info.update_at(tile)
    if (has_op()
            and rc.get_global_resources() >= rc.get_conveyor_cost() + map_info.ti_reserve()
            and rc.can_build_conveyor(tile, inward)):
        log(f"CORE_FEED {kind} {tile} -> {inward}")
        rc.build_conveyor(tile, inward)
        map_info.update_at(tile)
