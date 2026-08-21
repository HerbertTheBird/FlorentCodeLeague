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

    if chosen is not None and not _heal_contested(rc, chosen[0], GameConstants.SENTINEL_DAMAGE):
        rc.fire(chosen[0])
        return

    # Nothing to shoot: recycle this sentinel if the enemy has left our view
    # (no enemy bots, <= 2 enemy buildings). Guards on enemy presence, so a
    # momentary ammo dip with foes still around won't scrap it.
    turret_priority.scrap_if_idle(rc)


# ---- #34 heal-aware fire gate -------------------------------------------------
# Heal is 4 HP/Ti; a gunner is 1.75 dmg/Ti and a sentinel 1.80. So they cancel our
# damage at a quarter of the cost. Per-turn: gunner 7, sentinel 9, each healer 4.
#
# MEASURED over 3452 shots in 8 games (h13_tdiag):
#     0 adjacent enemy bots  31.3%
#     1 adjacent enemy bot   68.5%    <- the real case
#    >=2 adjacent enemy bots  0.2%    <- 6 shots; not worth any machinery
# With one healer a gunner still nets 3 dmg/turn, but costs 4 Ti to their 1 --
# a 4:1 titanium deficit on two thirds of every shot we take. Ammo was already
# 62.8% of all spend before the herbert11 bound; this is the same disease.
#
# NOTE the probe counts ADJACENT ENEMY BOTS, not bots that actually healed, so
# HEAL_BLOCK is an upper bound on genuinely contested shots.
HEAL_BLOCK = 2
FINISH_MULT = 1.0   # ...unless target HP <= FINISH_MULT * our damage (we finish it)


def _heal_contested(rc, tgt, my_damage):
    """True if we should HOLD this shot."""
    import map_info as _mi
    w = _mi._width
    n = tgt.x + tgt.y * w
    if not (0 <= tgt.x < w) or n < 0:
        return False
    bit = 1 << n
    healers = (_mi.manhattan(bit) & ~bit & _mi._bm_enemy_bots).bit_count()
    if healers < HEAL_BLOCK:
        return False
    hp = _mi._building_hp[n]
    if hp <= 0:
        return False                      # unknown HP: do not second-guess
    # Fire anyway when the shot finishes the target -- healing cannot undo a kill.
    if hp <= FINISH_MULT * my_damage:
        return False
    return True
