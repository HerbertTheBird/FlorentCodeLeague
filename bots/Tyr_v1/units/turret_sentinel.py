from fcode import Controller, Position, EntityType, GameConstants
import map_info
from pathing import Pathing
import units.opener as opener
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

# A conveyor with titanium on it right now is worth far more than the same
# conveyor empty: the shot destroys the delivery as well as the link, and the
# stack it was carrying never arrives. Ranked above a builder bot, below the
# turrets that can shoot back.
_CARRYING_WEIGHT = 20
_CARRIER_TYPES = (EntityType.CONVEYOR, EntityType.SPLITTER)


def init(c: Controller):
    global rc, nav
    rc = c
    nav = Pathing(c)
    opener.init(c)


def _resolve_target_on_tile(tile: Position):
    """Return (etype, hp) of what a sentinel shot at `tile` would actually hit,
    or None if the tile is empty / friendly / a marker. Sentinels (like all
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

    # A scripted sentinel was put down to cover specific tiles; take that shot
    # first. Falling through when it declines is deliberate -- a scripted
    # sentinel with nothing scripted in reach is still a sentinel.
    if opener.sentinel_fire():
        return

    if rc.get_action_cooldown() > 0:
        return
    if rc.get_global_ammo() < GameConstants.SENTINEL_AMMO_COST:
        return

    w = map_info._width
    raw = []
    for tile in rc.get_attackable_tiles():
        if not rc.can_fire(tile):
            continue
        resolved = _resolve_target_on_tile(tile)
        if resolved is None:
            continue
        etype, hp = resolved
        weight = _WEIGHTS.get(etype, 0)
        if etype in _CARRIER_TYPES and (map_info._bm_conv_ti >> (tile.x + tile.y * w)) & 1:
            weight = _CARRYING_WEIGHT
        raw.append((tile, tile.x + tile.y * w, weight, hp, etype))

    priority_sets = turret_priority.compute_priority_sets(rc)
    chosen = turret_priority.select_best(raw, priority_sets, nav, 0)
    if chosen is None:
        return
    rc.fire(chosen[0])
