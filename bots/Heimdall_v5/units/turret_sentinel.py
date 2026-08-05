"""Sentinel targeting for Heimdall v5's direct-contact core siege."""

from fcode import Controller, EntityType, GameConstants, Position

import comms
import map_info
from log import log


rc: Controller = None

_PRIORITY = {
    EntityType.CORE: 100,
    EntityType.GUNNER: 90,
    EntityType.SENTINEL: 85,
    EntityType.LAUNCHER: 80,
    EntityType.BUILDER_BOT: 60,
    EntityType.HARVESTER: 40,
    EntityType.CONVEYOR: 30,
    EntityType.SPLITTER: 30,
    EntityType.BARRIER: 10,
}


def init(c: Controller) -> None:
    global rc
    rc = c


def run() -> None:
    map_info.update(recompute=False)
    comms.update()
    map_info.recompute_derived()
    if (
        rc.get_action_cooldown() > 0
        or rc.get_global_ammo() < GameConstants.SENTINEL_AMMO_COST
    ):
        return

    attackable = set(rc.get_attackable_tiles())
    if not attackable:
        return
    my_team = rc.get_team()
    my_pos = rc.get_position()
    choices: list[tuple[int, int, int, int, Position]] = []

    for entity_id in rc.get_nearby_units():
        if rc.get_team(entity_id) == my_team:
            continue
        pos = rc.get_position(entity_id)
        if pos in attackable:
            etype = rc.get_entity_type(entity_id)
            priority = _PRIORITY.get(etype, 0)
            if priority:
                choices.append((priority, -pos.distance_squared(my_pos), -pos.x, -pos.y, pos))

    enemy = map_info._bm_team[1 - map_info._my_team_idx]
    for pos in attackable:
        n = pos.x + pos.y * map_info._width
        if not (enemy & (1 << n)):
            continue
        et_idx = map_info._building_et_idx[n]
        if et_idx < 0:
            continue
        etype = map_info._INT_ET[et_idx]
        priority = _PRIORITY.get(etype, 0)
        if priority:
            choices.append((priority, -pos.distance_squared(my_pos), -pos.x, -pos.y, pos))

    choices.sort(key=lambda item: item[:4], reverse=True)
    for _priority, _distance, _x, _y, pos in choices:
        if rc.can_fire(pos):
            rc.fire(pos)
            log(f"sentinel fired at {pos}")
            return
