"""Launcher-dispatched defender that exactly mirrors one claimed intruder."""

from fcode import Controller, Direction, EntityType, Position

import comms
import map_info
import units.builder
import units.launch_plan as launch_plan
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 14
target: Position | None = None
_move_direction: Direction | None = None
_enemy_id = 0
_previous_enemy: Position | None = None
_returning = False
_mode = ""


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _visible_enemy(enemy_id: int) -> Position | None:
    my_team = rc.get_team()
    for entity_id in rc.get_nearby_units():
        if (
            entity_id == enemy_id
            and rc.get_entity_type(entity_id) == EntityType.BUILDER_BOT
            and rc.get_team(entity_id) != my_team
        ):
            return rc.get_position(entity_id)
    return None


def _home() -> Position | None:
    return launch_plan.launcher_position()


def _at_home() -> bool:
    home = _home()
    return home is not None and max(
        abs(map_info._my_pos.x - home.x),
        abs(map_info._my_pos.y - home.y),
    ) <= 1


def _release(lane: int) -> None:
    global _enemy_id, _previous_enemy, _returning
    comms.release_defense_claim(lane)
    _enemy_id = 0
    _previous_enemy = None
    _returning = True


def score() -> int:
    global target, _move_direction, _enemy_id, _previous_enemy, _returning, _mode
    target = None
    _move_direction = None
    _mode = ""
    if not units.builder._economy_builder or units.builder._economy_index not in (0, 1):
        return 0
    lane = units.builder._economy_index
    claim = comms.defense_claim(lane)

    if claim is None:
        if _enemy_id:
            _release(lane)
        if _returning and not _at_home():
            target = _home()
            _mode = "return"
            return MAX_SCORE
        if _at_home():
            _returning = False
        return 0

    enemy_id, reported, active = claim
    if not active:
        # The launcher has reserved this defender but has not thrown it yet.
        target = _home()
        _mode = "pickup_wait"
        return MAX_SCORE

    enemy = _visible_enemy(enemy_id)
    if enemy is None:
        _release(lane)
        target = _home()
        _mode = "return"
        return MAX_SCORE

    if _enemy_id != enemy_id:
        _enemy_id = enemy_id
        _previous_enemy = reported
    previous = _previous_enemy
    _previous_enemy = enemy
    comms.set_defense_claim(lane, enemy_id, enemy, active=True)
    target = enemy
    _mode = "mirror"

    # A successful intercept keeps the defender in the enemy's 3x3. If that
    # geometry has already broken, exact mirroring can no longer body-block it.
    if max(
        abs(map_info._my_pos.x - enemy.x),
        abs(map_info._my_pos.y - enemy.y),
    ) > 2:
        _release(lane)
        target = _home()
        _mode = "return"
        return MAX_SCORE

    if previous is None:
        return MAX_SCORE
    dx, dy = enemy.x - previous.x, enemy.y - previous.y
    if dx == 0 and dy == 0:
        return MAX_SCORE                 # opponent held, so we hold too
    if max(abs(dx), abs(dy)) != 1:
        _release(lane)                   # missed/teleported: mirror is broken
        target = _home()
        _mode = "return"
        return MAX_SCORE
    _move_direction = map_info.direction_to(previous, enemy)
    return MAX_SCORE


def _move_home() -> None:
    home = _home()
    if home is None or _at_home():
        return
    zone = map_info.expand_chebyshev(
        1 << (home.x + home.y * map_info._width)
    ) & map_info._bm_passable_FFF
    zone &= ~(1 << (home.x + home.y * map_info._width))
    if zone:
        nav.move_to(set(map_info.iter_mask(zone)), avoid_turret=False)


def run() -> None:
    log("MIRROR GUARD")
    if _mode == "return":
        _move_home()
        return
    if _mode != "mirror" or _move_direction is None:
        return
    lane = units.builder._economy_index
    if rc.can_move(_move_direction):
        nav.move(_move_direction)
        return
    # Never improvise a different move: that gives the intruder a coreward gap.
    _release(lane)
    _move_home()
