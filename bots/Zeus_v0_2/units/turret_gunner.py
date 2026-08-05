from fcode import Controller, Direction, EntityType, Position, Team, Environment, GameConstants
import map_info
import comms
import units.killbox_plan as killbox_plan
from log import log

rc: Controller = None
my_pos: Position = None
my_team: Team = None
_attackable_by_dir: dict = {}

# Fire targets grouped by kind (the enemy team is required in every case).
_TURRET_TYPES = frozenset({EntityType.GUNNER, EntityType.LAUNCHER, EntityType.SENTINEL})
_CONVEYOR_TYPES = frozenset({EntityType.CONVEYOR, EntityType.SPLITTER})

# Rotation chases the core, enemy builders, or enemy turrets (never harvesters
# or conveyors). Core remains dominant; a builder is next so a counter-mirror
# gunner follows the bot it was built to remove.
_CORE_ROTATE_VALUE = 100
_BUILDER_ROTATE_VALUE = 4
_TURRET_ROTATE_VALUE = {
    EntityType.GUNNER: 3,
    EntityType.LAUNCHER: 2,
    EntityType.SENTINEL: 1,
}


def init(c: Controller):
    global rc, my_pos, my_team, _attackable_by_dir
    rc = c
    my_pos = rc.get_position()
    my_team = map_info._my_team
    # The engine's exact gunner firing geometry per facing (3 tiles cardinal,
    # 2 diagonal), ignoring occupancy — we walk it in order and apply occupancy
    # ourselves.
    _attackable_by_dir = {
        d: set(rc.get_attackable_tiles_from(my_pos, d, EntityType.GUNNER))
        for d in map_info._DIRECTIONS
    }


def _occupant(tile: Position):
    """(entity_type, team) of the entity a shot would hit on `tile`, else None.
    A builder bot standing on a tile wins over a building underneath it, matching
    the engine's fire resolution."""
    bot_id = rc.get_tile_builder_bot_id(tile)
    if bot_id is not None:
        return EntityType.BUILDER_BOT, rc.get_team(bot_id)
    bid = rc.get_tile_building_id(tile)
    if bid is not None:
        return rc.get_entity_type(bid), rc.get_team(bid)
    return None


def _line_eval(direction: Direction, ignore_leading_friendly_builder: bool = False):
    """Walk the gunner's firing line in `direction` (nearest first) and decide
    whether — and why — we would want to fire.

    Returns (reason, target, etype) or None, where `reason` is one of
    'core' / 'turret' / 'harvester' / 'conveyor' and `target` is the tile to
    fire at (always the first real obstruction). We fire when the first enemy
    obstruction is the enemy core, an enemy turret, harvester, or conveyor; or
    when it is any other enemy unit (barrier/builder) that stands between us and
    the enemy core further down the same line.

    Enemy builders are immediate valid targets. Walls block the line; a friendly obstruction blocks it too (no shooting
    through allies) — except an optional single leading friendly builder bot,
    which callers skip to detect a target it is temporarily blocking."""
    if direction == Direction.CENTRE:
        return None
    attackable = _attackable_by_dir.get(direction)
    if not attackable:
        return None

    cur = map_info.pos_add(my_pos, direction)
    first = None            # (tile, etype) of the first real obstruction
    skipped = False
    while map_info.in_bounds(cur) and cur in attackable:
        if map_info.ground_at(cur.x, cur.y) == Environment.WALL:
            break
        occ = _occupant(cur)
        if occ is None:
            cur = map_info.pos_add(cur, direction)
            continue
        etype, team = occ
        enemy = team != my_team

        if first is None:
            if (ignore_leading_friendly_builder and not skipped
                    and not enemy and etype == EntityType.BUILDER_BOT):
                skipped = True
                cur = map_info.pos_add(cur, direction)
                continue
            if not enemy:
                return None                      # ally blocks the lane
            first = (cur, etype)
            if etype == EntityType.CORE:
                return "core", cur, etype
            if etype == EntityType.BUILDER_BOT:
                return "builder", cur, etype
            if etype in _TURRET_TYPES:
                return "turret", cur, etype
            if etype == EntityType.HARVESTER:
                return "harvester", cur, etype
            if etype in _CONVEYOR_TYPES:
                return "conveyor", cur, etype
            # Enemy barrier / builder bot: only worth a shot if it's shielding
            # the enemy core further along this same line — keep scanning.
            cur = map_info.pos_add(cur, direction)
            continue

        # Past the first obstruction: look for the enemy core behind it.
        if not enemy:
            break                                # ally permanently breaks LOS
        if etype == EntityType.CORE:
            return "core", first[0], first[1]
        cur = map_info.pos_add(cur, direction)

    return None


def _facing_fire_target():
    """(reason, target, etype) if we want to fire in our current facing, else None."""
    return _line_eval(rc.get_direction())


def _friendly_builder_blocking_target() -> bool:
    """True when a friendly builder bot in our current facing is the only thing
    keeping us from a shot we'd otherwise take (so we hold and wait for it to
    move rather than rotating away)."""
    direction = rc.get_direction()
    if _line_eval(direction) is not None:
        return False                             # we'd already fire
    return _line_eval(direction, ignore_leading_friendly_builder=True) is not None


def _choose_rotate_dir():
    """Best facing yielding a core, builder, or enemy-turret shot.

    Ranks core first, then builders, then gunner > launcher > sentinel; nearer
    targets and stable direction order break ties.
    """
    current = rc.get_direction()
    best_key = None
    best_dir = None
    for i, d in enumerate(map_info._DIRECTIONS):
        if d == current:
            continue
        res = _line_eval(d)
        if res is None:
            continue
        reason, tile, etype = res
        if reason == "core":
            value = _CORE_ROTATE_VALUE
        elif reason == "builder":
            value = _BUILDER_ROTATE_VALUE
        elif reason == "turret":
            value = _TURRET_ROTATE_VALUE.get(etype, 0)
        else:
            continue                             # harvester/conveyor don't trigger a turn
        dist = max(abs(tile.x - my_pos.x), abs(tile.y - my_pos.y))
        key = (value, -dist, -i)
        if best_key is None or key > best_key:
            best_key = key
            best_dir = d
    return best_dir


def run():
    map_info.update()
    # Load the full board from the core's published map id (turrets get complete
    # map knowledge on spawn without seeing it themselves).
    comms.update()

    # (Titan: turrets fire from the global ammo pool.)
    if rc.get_action_cooldown() > 0:
        return

    # Killbox gunner: fire ONLY at an enemy builder in its fixed facing, and
    # never turn (it guards the one trap tile it was built to cover).
    if killbox_plan.is_killbox_gunner(my_pos):
        fire = _facing_fire_target()
        if fire is not None and fire[0] == "builder":
            target = fire[1]
            if rc.get_global_ammo() >= GameConstants.GUNNER_AMMO_COST and rc.can_fire(target):
                rc.fire(target)
                log(f"killbox gunner fired at {target}")
        return

    # 1. Fire if our current facing satisfies any fire condition.
    fire = _facing_fire_target()
    if fire is not None:
        target = fire[1]
        if rc.get_global_ammo() >= GameConstants.GUNNER_AMMO_COST and rc.can_fire(target):
            rc.fire(target)
            log(f"gunner fired at {target} ({fire[0]})")
        # Want to fire but can't afford ammo yet — hold this facing.
        return

    # 2. Hold (do nothing) if a friendly builder is the only thing blocking a
    #    shot we'd otherwise take; wait for it to move rather than turning away.
    if _friendly_builder_blocking_target():
        return

    # 3. Turn toward the core or an enemy turret if that would let us fire.
    rotate_dir = _choose_rotate_dir()
    if rotate_dir is not None and rc.can_rotate(rotate_dir):
        rc.rotate(rotate_dir)
        log(f"gunner rotated toward {rotate_dir}")
        return

    # 4. Otherwise do nothing.
