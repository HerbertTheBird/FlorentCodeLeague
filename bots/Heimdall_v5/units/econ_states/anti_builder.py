"""Place exactly one useful gunner against a builder inside the core perimeter."""

from fcode import Controller, Direction, Position

import comms
import map_info
import units.builder
from log import log
from pathing import Pathing
from units.econ_states.follow import _owned_targets


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 12
target: Position | None = None
target_facing: Direction | None = None


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _near_core(enemies: int) -> int:
    if not enemies or not map_info._bm_my_core_area:
        return 0
    core_tiles = list(map_info.iter_mask(map_info._bm_my_core_area))
    result = 0
    for pos in map_info.iter_mask(enemies):
        if min(pos.distance_squared(core) for core in core_tiles) <= 16:
            result |= 1 << (pos.x + pos.y * map_info._width)
    return result


def _site_open(pos: Position) -> bool:
    if not map_info.in_bounds(pos):
        return False
    # The current builder is not included in every cached bot mask, but Titan
    # never permits building on our own tile.
    if pos == map_info._my_pos:
        return False
    bit = 1 << (pos.x + pos.y * map_info._width)
    return not bool(
        bit
        & (
            map_info._bm_env[map_info._IDX_ENV_WALL]
            | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
            | map_info._bm_any_building
            | map_info._bm_friendly_bots
            | map_info._bm_enemy_bots
            | map_info._bm_my_gunner_claims
        )
    )


def _sites_for_enemy(enemy: Position) -> tuple[int, dict[int, Direction]]:
    sites = 0
    facings: dict[int, Direction] = {}
    w = map_info._width
    for di, direction in enumerate(map_info._DIRECTIONS):
        rays = map_info._GUNNER_RAYS[di]
        for step, (dx, dy) in enumerate(rays):
            site = Position(enemy.x - dx, enemy.y - dy)
            if not _site_open(site):
                continue
            blocked = False
            for px, py in rays[:step]:
                x, y = site.x + px, site.y + py
                bit = 1 << (x + y * w)
                if (
                    map_info._bm_env[map_info._IDX_ENV_WALL] & bit
                    or map_info._bm_any_building & bit
                ):
                    blocked = True
                    break
            if blocked:
                continue
            n = site.x + site.y * w
            sites |= 1 << n
            facings[n] = direction
    return sites, facings


def _sites_within_core_radius(sites: int, radius_sq: int = 4) -> int:
    """Keep gunner build sites within radius_sq of any allied 2x2 core tile."""
    if not sites or not map_info._bm_my_core_area:
        return 0
    core_tiles = tuple(map_info.iter_mask(map_info._bm_my_core_area))
    kept = 0
    for site in map_info.iter_mask(sites):
        if min(site.distance_squared(core) for core in core_tiles) <= radius_sq:
            kept |= 1 << (site.x + site.y * map_info._width)
    return kept


def _covered_by_any_gunner_rotation(enemies: int) -> int:
    """Enemy builders an allied gunner can hit in at least one facing.

    This uses the gunner's exact cardinal/diagonal ranges and stops at the
    first unit, structure, or wall in each ray. A gunner therefore reserves a
    builder even when it is currently facing elsewhere, preventing a second
    anti-builder gunner from being built for the same target.
    """
    if not enemies:
        return 0
    allied_gunners = (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    if not allied_gunners:
        return 0

    occupied = (
        map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
    )
    covered = 0
    w = map_info._width
    for gunner in map_info.iter_mask(allied_gunners):
        for rays in map_info._GUNNER_RAYS:
            for dx, dy in rays:
                tile = Position(gunner.x + dx, gunner.y + dy)
                if not map_info.in_bounds(tile):
                    break
                bit = 1 << (tile.x + tile.y * w)
                if map_info._bm_env[map_info._IDX_ENV_WALL] & bit:
                    break
                if enemies & bit:
                    covered |= bit
                    break
                if occupied & bit:
                    break
    return covered


def score() -> int:
    global target, target_facing
    target = None
    target_facing = None
    if not units.builder._economy_builder:
        return 0
    enemies = map_info._bm_enemy_bots & map_info._bm_visible
    enemies = _near_core(enemies)
    enemies &= ~_covered_by_any_gunner_rotation(enemies)
    enemies = _owned_targets(enemies)
    if not enemies:
        return 0
    enemy, distance = nav.closest(enemies)
    if enemy is None or distance < 0:
        return 0
    sites, facings = _sites_for_enemy(enemy)
    sites = _sites_within_core_radius(sites)
    from units.econ_states import route
    sites &= ~route.planned_route_mask()
    if not sites:
        return 0
    target, distance = nav.closest(sites)
    if target is None or distance < 0:
        return 0
    target_facing = facings[target.x + target.y * map_info._width]
    return MAX_SCORE


def run() -> None:
    log("ANTI BUILDER")
    if target is None or target_facing is None:
        return
    if map_info._my_pos.distance_squared(target) != 1:
        nav.move_adjacent(target, avoid_turret=False)
        return
    if (
        rc.get_global_resources()
        >= rc.get_gunner_cost() + map_info.builder_ti_reserve()
        and rc.can_build_gunner(target, target_facing)
    ):
        rc.build_gunner(target, target_facing)
        comms.note_gunner_built()
        map_info.update_at(target)
