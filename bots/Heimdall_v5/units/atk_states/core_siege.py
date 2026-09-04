"""Passive sentinel siege when no enemy builder is currently visible.

The map catalogue tells us where the core is, but this state intentionally
activates only after higher-priority economy work is exhausted. If an enemy
builder is present, ordinary gunner placement/following handles the contest.
"""

from fcode import Controller, Direction, Position

import comms
import map_info
import units.builder
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 11
target: Position | None = None
target_facing: Direction | None = None
_sentinel_site: Position | None = None
_mode = ""


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _bit(pos: Position) -> int:
    return 1 << (pos.x + pos.y * map_info._width)


def _our_sentinel_at(pos: Position | None) -> bool:
    if pos is None or not map_info.in_bounds(pos):
        return False
    return bool(
        _bit(pos)
        & map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[map_info._my_team_idx]
    )


def active_sentinel() -> bool:
    return _our_sentinel_at(_sentinel_site)


def _facing_for_site(site: Position, core_area: int) -> Direction | None:
    for di, direction in enumerate(map_info._DIRECTIONS):
        for dx, dy in map_info._SENTINEL_OFFSETS[di]:
            x, y = site.x + dx, site.y + dy
            if not (0 <= x < map_info._width and 0 <= y < map_info._height):
                continue
            if core_area & (1 << (x + y * map_info._width)):
                return direction
    return None


def _choose_site() -> tuple[Position | None, Direction | None]:
    core_area = map_info._bm_their_core_area
    if not core_area:
        return None, None
    occupied = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
    )
    candidates = map_info._bm_seen & map_info._board_mask & ~occupied
    facings: dict[int, Direction] = {}
    valid = 0
    m = candidates
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        site = Position(n % map_info._width, n // map_info._width)
        facing = _facing_for_site(site, core_area)
        if facing is not None:
            valid |= lsb
            facings[n] = facing
    if not valid:
        return None, None

    # Prefer sites outside all known turret fire. Within that safety tier, use
    # the closest reachable site so direct core contact converts immediately.
    safe = valid & ~(
        map_info._bm_enemy_soft_threat | map_info._bm_enemy_hard_threat
    )
    site, _distance = nav.closest(safe or valid)
    if site is None:
        return None, None
    return site, facings[site.x + site.y * map_info._width]


def score() -> int:
    global target, target_facing, _mode, _sentinel_site
    target = None
    target_facing = None
    _mode = ""
    if not units.builder._economy_builder or not map_info._bm_their_core_area:
        return 0
    if units.builder.visible_enemy_builders():
        return 0

    if active_sentinel():
        target = _sentinel_site
        _mode = "guard"
        return 3
    _sentinel_site = None
    target, target_facing = _choose_site()
    if target is None:
        return 0
    _mode = "build"
    return 3


def run() -> None:
    global _sentinel_site
    log("CORE SIEGE")
    if target is None:
        return
    if _mode == "guard":
        if map_info._my_pos.distance_squared(target) != 1:
            nav.move_adjacent(target, avoid_turret=False)
            return
        bit = _bit(target)
        if bit & map_info._bm_damaged and rc.can_heal(target):
            rc.heal(target)
        return
    if target_facing is None:
        return
    if map_info._my_pos.distance_squared(target) != 1:
        nav.move_adjacent(target, avoid_turret=False)
        return
    if (
        rc.get_global_resources() >= rc.get_sentinel_cost()
        and rc.can_build_sentinel(target, target_facing)
    ):
        rc.build_sentinel(target, target_facing)
        _sentinel_site = target
        comms.note_sentinel_built()
        map_info.update_at(target)
