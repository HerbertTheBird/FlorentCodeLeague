from fcode import Controller, Direction, EntityType, Position, Team, Environment, GameConstants
import map_info
import comms
from log import log

rc: Controller = None
my_pos: Position = None
my_team: Team = None
_attackable_by_dir: dict = {}
_builder_tracks: dict[int, tuple[Position, int, int]] = {}
_stationary_builder_turns: dict[int, int] = {}

# Fire targets grouped by kind (the enemy team is required in every case).
_TURRET_TYPES = frozenset({EntityType.GUNNER, EntityType.LAUNCHER, EntityType.SENTINEL})
_CONVEYOR_TYPES = frozenset({EntityType.CONVEYOR, EntityType.SPLITTER})

# Rotation continues the chip attack into valuable economy structures after a
# target falls. Builders become rotation targets only after camping one tile
# for more than three consecutive visible turns.
_CORE_ROTATE_VALUE = 100
_TURRET_ROTATE_VALUE = {
    EntityType.GUNNER: 3,
    EntityType.LAUNCHER: 2,
    EntityType.SENTINEL: 1,
}
_ECON_ROTATE_VALUE = {
    EntityType.HARVESTER: 2,
    EntityType.SPLITTER: 2,
    EntityType.CONVEYOR: 1,
}
_BUILDER_ROTATE_VALUE = 2


def init(c: Controller):
    global rc, my_pos, my_team, _attackable_by_dir
    global _builder_tracks, _stationary_builder_turns
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
    _builder_tracks = {}
    _stationary_builder_turns = {}


def _update_builder_stationary() -> None:
    """Track consecutive visible turns each enemy builder stays on one tile."""
    global _builder_tracks, _stationary_builder_turns
    current_round = rc.get_current_round()
    new_tracks = {}
    stationary_by_tile = {}
    enemies = map_info._bm_enemy_bots & map_info._bm_visible
    for tile in map_info.iter_mask(enemies):
        builder_id = rc.get_tile_builder_bot_id(tile)
        if builder_id is None or rc.get_team(builder_id) == my_team:
            continue
        old = _builder_tracks.get(builder_id)
        if old is not None and old[0] == tile and old[2] == current_round - 1:
            turns = old[1] + 1
        else:
            turns = 1
        new_tracks[builder_id] = (tile, turns, current_round)
        stationary_by_tile[tile.x + tile.y * map_info._width] = turns
    _builder_tracks = new_tracks
    _stationary_builder_turns = stationary_by_tile


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


def _claimed_by_other_gunner(target: Position) -> bool:
    """Whether another allied gunner's current raw ray covers target."""
    w = map_info._width
    my_bit = 1 << (my_pos.x + my_pos.y * w)
    allied = (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[map_info._my_team_idx]
        & ~my_bit
    )
    for gunner in map_info.iter_mask(allied):
        n = gunner.x + gunner.y * w
        di = map_info._building_dir[n]
        if not (0 <= di < len(map_info._GUNNER_RAYS)):
            continue
        for dx, dy in map_info._GUNNER_RAYS[di]:
            tile = Position(gunner.x + dx, gunner.y + dy)
            if not map_info.in_bounds(tile):
                break
            if map_info.ground_at(tile.x, tile.y) == Environment.WALL:
                break
            if tile == target:
                return True
    return False


def _enemy_builder_adjacent_to(tile: Position) -> bool:
    """Whether a visible enemy builder can cardinally repair this tile."""
    w = map_info._width
    bit = 1 << (tile.x + tile.y * w)
    cardinal_ring = map_info.expand_manhattan(bit) & ~bit
    return bool(
        cardinal_ring
        & map_info._bm_enemy_bots
        & map_info._bm_visible
    )


def _line_eval(direction: Direction, ignore_leading_friendly_builder: bool = False):
    """Walk the gunner's firing line in `direction` (nearest first) and decide
    whether — and why — we would want to fire.

    Returns (reason, target, etype) or None, where `reason` is one of
    'core' / 'turret' / 'barrier_to_gunner' / 'builder' / 'harvester' /
    'conveyor' and `target` is the tile to fire at (always the first real
    obstruction). We fire when the first enemy
    obstruction is the enemy core, an enemy turret, harvester, or conveyor; or
    when it is an unguarded enemy barrier shielding an otherwise-unclaimed
    enemy gunner,
    or when another enemy obstruction stands between us and the enemy core.

    Walls block the line; a friendly obstruction blocks it too (no shooting
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
            if etype in _TURRET_TYPES:
                return "turret", cur, etype
            if etype == EntityType.BUILDER_BOT:
                return "builder", cur, etype
            if etype == EntityType.HARVESTER:
                return "harvester", cur, etype
            if etype in _CONVEYOR_TYPES:
                return "conveyor", cur, etype
            # Enemy barrier / builder bot: only worth a shot if it's shielding
            # the enemy core further along this same line — keep scanning.
            cur = map_info.pos_add(cur, direction)
            continue

        # Past the first obstruction: an unguarded barrier may be chipped through
        # to expose an enemy gunner, but only one allied gunner should take that
        # job. Do not enter a repair race while an enemy builder is cardinally
        # adjacent to the particular barrier we would shoot.
        if not enemy:
            break                                # ally permanently breaks LOS
        if (
            etype == EntityType.GUNNER
            and first[1] == EntityType.BARRIER
            and not _enemy_builder_adjacent_to(first[0])
            and not _claimed_by_other_gunner(cur)
        ):
            return "barrier_to_gunner", first[0], first[1]
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
    """Best valuable facing, including builders stationary for over 3 turns."""
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
        elif reason == "turret":
            value = _TURRET_ROTATE_VALUE.get(etype, 0)
        elif reason == "barrier_to_gunner":
            value = _TURRET_ROTATE_VALUE[EntityType.GUNNER]
        elif reason in ("harvester", "conveyor"):
            value = _ECON_ROTATE_VALUE.get(etype, 0)
        elif reason == "builder":
            n = tile.x + tile.y * map_info._width
            if _stationary_builder_turns.get(n, 0) <= 3:
                continue
            value = _BUILDER_ROTATE_VALUE
        else:
            continue
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
    _update_builder_stationary()

    # (Titan: turrets fire from the global ammo pool.)
    if rc.get_action_cooldown() > 0:
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
