"""Urgently place a cardinally adjacent gunner against an enemy sentinel."""

from fcode import Controller, Direction, Position

import comms
import map_info
import units.builder
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 15
target: Position | None = None
_target_facing: Direction | None = None
_mode = "anti_sentinel"

CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _bit(pos: Position) -> int:
    return 1 << (pos.x + pos.y * map_info._width)


def _candidate_sites(sentinel: Position) -> tuple[int, dict[int, Direction]]:
    my_bit = _bit(map_info._my_pos)
    occupied = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
        | ((map_info._bm_friendly_bots | map_info._bm_enemy_bots) & ~my_bit)
        | map_info._bm_my_gunner_claims
    )
    sites = 0
    facings = {}
    for direction in CARDINALS:
        site = map_info.pos_add(sentinel, direction)
        if not map_info.in_bounds(site):
            continue
        bit = _bit(site)
        if occupied & bit:
            continue
        sites |= bit
        facings[site.x + site.y * map_info._width] = map_info.direction_to(site, sentinel)
    return sites, facings


def score() -> int:
    global target, _target_facing
    target = None
    _target_facing = None
    if not units.builder._economy_builder:
        return 0
    enemy_idx = 1 - map_info._my_team_idx
    sentinels = (
        map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[enemy_idx]
        & map_info._bm_visible
        & ~map_info._bm_my_gunner_claims
    )
    best_key = None
    for sentinel in map_info.iter_mask(sentinels):
        sites, facings = _candidate_sites(sentinel)
        if not sites:
            continue
        site, distance = nav.closest(sites)
        if site is None:
            continue
        key = (distance, sentinel.distance_squared(map_info._my_pos), sentinel.x, sentinel.y)
        if best_key is None or key < best_key:
            best_key = key
            target = site
            _target_facing = facings[site.x + site.y * map_info._width]
    return MAX_SCORE if target is not None else 0


def _step_off_target() -> bool:
    choices = set()
    for direction in CARDINALS:
        pos = map_info.pos_add(target, direction)
        if not map_info.in_bounds(pos) or not map_info.is_passable(pos):
            continue
        bit = _bit(pos)
        if bit & ((map_info._bm_friendly_bots | map_info._bm_enemy_bots) & ~_bit(map_info._my_pos)):
            continue
        choices.add(pos)
    return bool(choices) and nav.move_to(choices, avoid_turret=False)


def run() -> None:
    log("ANTI SENTINEL")
    if target is None or _target_facing is None:
        return
    distance = map_info._my_pos.distance_squared(target)
    if distance == 0:
        _step_off_target()                 # cannot build on ourselves
        return
    if distance != 1:
        nav.move_adjacent(target, avoid_turret=False)
        return
    if (
        rc.get_global_resources() >= rc.get_gunner_cost()
        and rc.can_build_gunner(target, _target_facing)
    ):
        rc.build_gunner(target, _target_facing)
        comms.note_gunner_built()
        map_info.update_at(target)
