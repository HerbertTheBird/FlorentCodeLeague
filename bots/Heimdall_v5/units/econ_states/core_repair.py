"""Heal the allied core first, then damaged allied builders."""

from fcode import Controller, GameConstants, Position

import map_info
import units.builder
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 15
target: Position | None = None
repair_kind: str | None = None
approach: Position | None = None
_blocked_until = -1
_progress_last_round = -1
_progress_last_improved = -1
_progress_best_distance = 1 << 30
REPAIR_STALL_TURNS = 16
REPAIR_RETRY_TURNS = 20


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def score() -> int:
    global target, repair_kind, approach
    target = None
    repair_kind = None
    approach = None
    if not units.builder._economy_builder:
        return 0
    damaged = map_info._bm_my_core_area & map_info._bm_damaged
    if not damaged:
        # Titan healing is cardinal, so a builder cannot heal its own tile.
        # Cooperatively heal a visible damaged teammate instead.
        damaged_builders = 0
        for pos in map_info.iter_mask(
            map_info._bm_friendly_bots & map_info._bm_visible
        ):
            builder_id = rc.get_tile_builder_bot_id(pos)
            if (
                builder_id is not None
                and builder_id != rc.get_id()
                and rc.get_hp(builder_id) < GameConstants.BUILDER_BOT_MAX_HP
            ):
                damaged_builders |= 1 << (pos.x + pos.y * map_info._width)
        if not damaged_builders:
            return 0
        target, distance = nav.closest(damaged_builders)
        if target is None or distance < 0:
            return 0
        repair_kind = "builder"
        return 14
    current = rc.get_current_round()
    core_distance = min(
        (abs(map_info._my_pos.x - p.x) + abs(map_info._my_pos.y - p.y)
         for p in map_info.iter_mask(damaged)),
        default=1 << 30,
    )
    if current < _blocked_until and core_distance > 1:
        return 0
    w = map_info._width
    my = map_info._my_pos
    my_bit = 1 << (my.x + my.y * w)
    occupied = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
    )
    standable = (map_info._bm_passable_FFF | my_bit) & ~occupied
    best = None
    best_key = None
    best_approach = None
    m = damaged
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        hp = map_info._building_hp[n]
        pos = Position(n % w, n // w)
        adjacent = map_info.expand_manhattan(lsb) & ~lsb & standable
        # The current builder is absent from _bm_friendly_bots, but include it
        # explicitly when already in the correct cardinal healing position.
        if my.distance_squared(pos) == 1:
            adjacent |= my_bit
        stand, distance = nav.closest(adjacent)
        if stand is None or distance < 0:
            continue
        # If we can already heal a damaged core tile, do it without moving.
        # Titan shares movement/action cooldown; HP-first selection made edge
        # cores alternate between two approach tiles forever without healing.
        already_adjacent = my.distance_squared(pos) == 1
        key = (not already_adjacent, hp, distance, n)
        if best_key is None or key < best_key:
            best_key = key
            best = pos
            best_approach = stand
    target = best
    approach = best_approach
    repair_kind = "core"
    return MAX_SCORE if target is not None else 0


def run() -> None:
    global _blocked_until, _progress_last_round
    global _progress_last_improved, _progress_best_distance
    log("CORE REPAIR")
    if target is None:
        return
    if repair_kind == "core" and approach is not None and map_info._my_pos != approach:
        current = rc.get_current_round()
        core_distance = min(
            (abs(map_info._my_pos.x - p.x) + abs(map_info._my_pos.y - p.y)
             for p in map_info.iter_mask(map_info._bm_my_core_area)),
            default=1 << 30,
        )
        if _progress_last_round != current - 1:
            _progress_best_distance = core_distance
            _progress_last_improved = current
        elif core_distance < _progress_best_distance:
            _progress_best_distance = core_distance
            _progress_last_improved = current
        _progress_last_round = current
        if current - _progress_last_improved >= REPAIR_STALL_TURNS:
            _blocked_until = current + REPAIR_RETRY_TURNS
            _progress_last_round = -1
            return
        nav.move_to(approach)
        return
    if map_info._my_pos.distance_squared(target) != 1:
        nav.move_adjacent(target)
        return
    if rc.can_heal(target):
        _progress_last_round = -1
        rc.heal(target)
