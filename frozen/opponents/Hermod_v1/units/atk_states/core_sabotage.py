"""Fallback assault after the attacker's sentinel has been destroyed.

Enemy conveyors beside the 2x2 core are attacked directly from their tile.
Otherwise reuse the offensive gunner scorer to open fire on the core, while
closing to the core ring whenever no gunner placement is currently available.
"""

from fcode import Controller, Position

import map_info
import units.builder
import units.atk_states.attack as attack
import units.atk_states.sentinel_siege as siege
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 13
target: Position | None = None
_mode = ""


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _core_area() -> int:
    area = map_info._bm_their_core_area
    if area:
        return area
    core = map_info._their_core or map_info._predicted_enemy_core
    if core is None:
        return 0
    result = 0
    for x in (core.x, core.x + 1):
        for y in (core.y, core.y + 1):
            if 0 <= x < map_info._width and 0 <= y < map_info._height:
                result |= 1 << (x + y * map_info._width)
    return result


def _adjacent_enemy_conveyors(core_area: int) -> int:
    enemy_idx = 1 - map_info._my_team_idx
    conveyors = (
        map_info._bm_et[map_info._IDX_CONVEYOR]
        | map_info._bm_et[map_info._IDX_SPLITTER]
    ) & map_info._bm_team[enemy_idx]
    return conveyors & map_info.expand_chebyshev(core_area) & ~core_area


def _core_approach(core_area: int) -> Position | None:
    ring = map_info.expand_chebyshev(core_area) & ~core_area
    ring &= map_info._bm_passable_FFF
    my_bit = 1 << (
        map_info._my_pos.x + map_info._my_pos.y * map_info._width
    )
    ring &= ~(
        (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & ~my_bit
    )
    pos, _distance = nav.closest(ring)
    return pos


def score() -> int:
    global target, _mode
    target = None
    _mode = ""
    if not units.builder._atk_bot or not siege.sentinel_destroyed():
        return 0
    core_area = _core_area()
    if not core_area:
        return 0

    conveyors = _adjacent_enemy_conveyors(core_area)
    if conveyors:
        pos, _distance = nav.closest(conveyors)
        if pos is not None:
            target = pos
            _mode = "conveyor"
            return MAX_SCORE

    if attack.score() > 0:
        _mode = "gunner"
        return MAX_SCORE

    target = _core_approach(core_area)
    if target is not None:
        _mode = "approach"
        return MAX_SCORE
    return 0


def run() -> None:
    log("CORE SABOTAGE")
    if _mode == "gunner":
        attack.run()
        return
    if target is None:
        return
    if _mode == "conveyor":
        if map_info._my_pos != target:
            nav.move_to(target, avoid_turret=False, allow_enemy_gunner=True)
            return
        # Builder attacks only work against the building under the builder. This
        # repeatedly strips the core-adjacent supply tile until it is destroyed.
        if rc.can_fire(map_info._my_pos):
            rc.fire(map_info._my_pos)
        return
    if map_info._my_pos != target:
        nav.move_to(target, avoid_turret=False, allow_enemy_gunner=True)
