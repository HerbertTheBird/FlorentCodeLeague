from fcode import Controller, Position, EntityType, GameConstants
import map_info
from pathing import Pathing
import units.turret_priority as turret_priority

rc: Controller = None
nav: Pathing = None

_WEIGHTS = {
    EntityType.CORE: 2,
    EntityType.SENTINEL: 50,
    EntityType.LAUNCHER: 10,
    EntityType.HARVESTER: 0,
    EntityType.BUILDER_BOT: 15,
    EntityType.GUNNER: 40,
    EntityType.BARRIER: 5,
    EntityType.SPLITTER: 3,
    EntityType.CONVEYOR: 4,
}


def init(c: Controller):
    global rc, nav
    rc = c
    nav = Pathing(c)


def _resolve_target_on_tile(tile: Position):
    """Return (etype, hp) of what a sentinel shot at `tile` would actually hit,
    or None if the tile is empty or friendly. Sentinels (like all
    turrets) hit a builder bot before any building on the same tile."""
    my_team = map_info._my_team
    bot_id = rc.get_tile_builder_bot_id(tile)
    if bot_id is not None:
        if rc.get_team(bot_id) == my_team:
            return None
        return EntityType.BUILDER_BOT, rc.get_hp(bot_id)
    bid = rc.get_tile_building_id(tile)
    if bid is None:
        return None
    if rc.get_team(bid) == my_team:
        return None
    return rc.get_entity_type(bid), rc.get_hp(bid)


def run():
    map_info.update()

    if rc.get_action_cooldown() > 0:
        return

    chosen = None
    if rc.get_global_ammo() >= GameConstants.SENTINEL_AMMO_COST:
        w = map_info._width
        raw = []
        for tile in rc.get_attackable_tiles():
            if not rc.can_fire(tile):
                continue
            resolved = _resolve_target_on_tile(tile)
            if resolved is None:
                continue
            etype, hp = resolved
            raw.append((tile, tile.x + tile.y * w, _WEIGHTS.get(etype, 0), hp, etype))
        priority_sets = turret_priority.compute_priority_sets(rc)
        chosen = turret_priority.select_best(raw, priority_sets, nav, 0)

    if chosen is not None:
        rc.fire(chosen[0])
        return

    # Nothing to shoot and no enemy bots tracked anywhere: recycle this sentinel
    # for its build scale. Enemy BUILDINGS are not considered -- the old comment
    # claimed a "<= 2 buildings" guard that never existed, and adding it measured
    # -0.0081. Guards on enemy BOTS, so a momentary ammo dip with foes around
    # won't scrap it.
    turret_priority.scrap_if_idle(rc)
