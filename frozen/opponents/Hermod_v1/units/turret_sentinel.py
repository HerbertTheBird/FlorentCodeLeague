"""Hermod sentinel: hold its facing and fire over all intervening tiles.

Sentinels have a broad directional footprint with radius squared 32. Unlike a
gunner, their shots are not line-of-sight rays, so walls, barriers, builders,
and other structures between the sentinel and its target are irrelevant.
"""

from fcode import Controller, EntityType, GameConstants, Position

import comms
import map_info
from log import log


rc: Controller = None

_PRIORITY = {
    EntityType.CORE: 100,
    EntityType.GUNNER: 80,
    EntityType.SENTINEL: 70,
    EntityType.LAUNCHER: 60,
    EntityType.BUILDER_BOT: 50,
    EntityType.HARVESTER: 30,
    EntityType.CONVEYOR: 20,
    EntityType.SPLITTER: 20,
    EntityType.BARRIER: 10,
}


def init(c: Controller) -> None:
    global rc
    rc = c


def _enemy_builder_targets(attackable: set[Position]) -> list[tuple[int, int, Position]]:
    result = []
    my_team = rc.get_team()
    my_pos = rc.get_position()
    for entity_id in rc.get_nearby_units():
        if (
            rc.get_entity_type(entity_id) != EntityType.BUILDER_BOT
            or rc.get_team(entity_id) == my_team
        ):
            continue
        pos = rc.get_position(entity_id)
        if pos in attackable:
            result.append((
                _PRIORITY[EntityType.BUILDER_BOT],
                -pos.distance_squared(my_pos),
                pos,
            ))
    return result


def run() -> None:
    map_info.update(recompute=False)
    comms.update()
    map_info.recompute_derived()
    if rc.get_action_cooldown() > 0:
        return
    if rc.get_global_ammo() < GameConstants.SENTINEL_AMMO_COST:
        return

    attackable = set(rc.get_attackable_tiles())
    if not attackable:
        return

    my_pos = rc.get_position()
    enemy_idx = 1 - map_info._my_team_idx
    enemy = map_info._bm_team[enemy_idx]
    choices: list[tuple[int, int, Position]] = _enemy_builder_targets(attackable)
    for pos in attackable:
        n = pos.x + pos.y * map_info._width
        bit = 1 << n
        if not (enemy & bit):
            continue
        et_idx = map_info._building_et_idx[n]
        if et_idx < 0:
            continue
        etype = map_info._INT_ET[et_idx]
        priority = _PRIORITY.get(etype, 0)
        if priority:
            choices.append((priority, -pos.distance_squared(my_pos), pos))

    choices.sort(reverse=True, key=lambda item: (item[0], item[1], -item[2].x, -item[2].y))
    for _priority, _distance, target in choices:
        if rc.can_fire(target):
            rc.fire(target)
            log(f"sentinel fired at {target}")
            return
