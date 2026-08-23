from fcode import Controller, Position, EntityType, GameConstants
import map_info
from pathing import Pathing
import units.turret_priority as turret_priority
import comms
from _config import SKIP_TURRET_DUEL

rc: Controller = None
nav: Pathing = None

# bird2 targeting order: enemy BUILDER BOTS first, then the enemy CORE, then
# everything else. The reasoning is the rush model in rush.py -- it prices the
# siege against a defender heal rate R, and every builder we kill removes 4 HP/round
# from R permanently. Killing one healer is worth more than the 18 HP the same
# shot would have taken off the core, because it speeds up every subsequent shot.
# A builder is 40 HP, so three sentinel shots remove a healer for good.
#
# The core is raised from 2 to 12 so that, once no bots are in the line, the siege
# actually chews the core instead of preferring a 4-point conveyor. It stays below
# BUILDER_BOT (15) so a defender walking into the ray is always taken first.
_WEIGHTS = {
    EntityType.CORE: 12,
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
        if SKIP_TURRET_DUEL and comms.heal_race_won():
            # We are out-healing whatever is shooting the core, so their turret is
            # not the thing killing us -- and a sentinel shot costs 10 ammo. Demote
            # enemy turrets below the targets that actually advance the game
            # (builders, then the core). Killing a duelling turret restores nothing;
            # killing a builder permanently removes 4 HP/round of THEIR healing.
            raw = [(t, n, (1 if et in (EntityType.SENTINEL, EntityType.GUNNER)
                           else w), hp, et)
                   for (t, n, w, hp, et) in raw]
        priority_sets = turret_priority.compute_priority_sets(rc)
        chosen = turret_priority.select_best(raw, priority_sets, nav, 0)

    if chosen is not None:
        # Conserve ammo: hold fire on a wasted shot (full-HP target or the enemy core
        # while income is <=1 and an enemy builder is near). We still had a target, so
        # don't fall through to scrap.
        #
        # EXCEPT during our own siege. should_hold_fire refuses to shoot the enemy
        # core whenever our income is <= 1 -- which is exactly the state an
        # econ-light rush is in by design. Left alone it silently cancels the plan:
        # sentinels arrive, cost 30 Ti each, and then never fire. The siege word
        # (comms slot 14) is live only while the rusher is actually besieging, so
        # this exemption cannot leak into the ordinary defensive turrets at home.
        if comms.siege_active() or not turret_priority.should_hold_fire(rc, chosen[0]):
            rc.fire(chosen[0])
        return

    # Nothing to shoot: recycle this sentinel if the enemy has left our view
    # (no enemy bots, <= 2 enemy buildings). Guards on enemy presence, so a
    # momentary ammo dip with foes still around won't scrap it.
    turret_priority.scrap_if_idle(rc)
