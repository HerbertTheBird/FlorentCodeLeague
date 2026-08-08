from main import has_op
from fcode import Controller, Direction, Position, EntityType
import map_info
from log import log
from units.spawn_plan import choose_spawn_plan, draw_spawn_plan, INITIAL_SPAWN_COUNT, INITIAL_EXPLORE_MAX_STEPS
import comms
rc: Controller

# --- Configurable ---
SCALE_MULT = 1
DEFENSE_FRIENDLY_RADIUS_SQ = 36

_spawn_plan: list[Direction] | None = None
_num_spawned = 0
_spawn_tiles: tuple[Position, ...] = ()   # tiles immediately surrounding the 2x2 core


def _compute_spawn_tiles() -> tuple[Position, ...]:
    """Tiles immediately surrounding the core's 2x2 footprint (never on the core
    itself). These are the only legal builder-spawn positions."""
    core = map_info._my_core
    if core is None:
        return ()
    w = map_info._width
    h = map_info._height
    core_tiles = {(core.x + dx, core.y + dy) for dx in (0, 1) for dy in (0, 1)}
    ring: set[tuple[int, int]] = set()
    for tx, ty in core_tiles:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nx, ny = tx + dx, ty + dy
                if (nx, ny) in core_tiles:
                    continue
                if 0 <= nx < w and 0 <= ny < h:
                    ring.add((nx, ny))
    return tuple(Position(x, y) for x, y in ring)


def init(c: Controller):
    global rc
    rc = c


def _spawn_best_toward(target: Position) -> bool:
    """Spawn a builder on the surrounding tile closest to `target`. Returns True
    if a builder was spawned."""
    best = None
    best_d = None
    for p in _spawn_tiles:
        if rc.can_spawn(p):
            d = p.distance_squared(target)
            if best_d is None or d < best_d:
                best_d = d
                best = p
    if best is None:
        return False
    rc.spawn_builder(best)
    return True


def _spawn_toward_plan(core_pos: Position) -> bool:
    global _num_spawned
    if _spawn_plan is None or _num_spawned >= len(_spawn_plan):
        return False

    planned_dir = _spawn_plan[_num_spawned]
    dx, dy = map_info._DIRECTION_DELTAS[planned_dir]
    # Aim well past the footprint so the closest surrounding tile is the one best
    # aligned with the planned direction.
    target = Position(core_pos.x + dx * 8, core_pos.y + dy * 8)
    if _spawn_best_toward(target):
        _num_spawned += 1
        return True
    return False


def _spawn_toward_center() -> bool:
    """Spawn on the surrounding tile closest to map center."""
    center = Position(map_info._width // 2, map_info._height // 2)
    return _spawn_best_toward(center)


def _spawn_toward_enemy_if_undefended(has_close_ally: bool, closest_enemy: Position | None) -> bool:
    """If an enemy builder bot is in vision and no friendly builder bot sits
    within dist² DEFENSE_FRIENDLY_RADIUS_SQ of the core, spawn a defender on the
    surrounding tile closest to the nearest enemy bot. Returns True if spawned."""
    if has_close_ally or closest_enemy is None:
        return False
    return _spawn_best_toward(closest_enemy)


def _scan_nearby_builders(core_pos: Position, my_team):
    ally_builder_count = 0
    has_close_ally = False
    closest_enemy = None
    closest_enemy_d = None

    for uid in rc.get_nearby_units():
        if rc.get_entity_type(uid) != EntityType.BUILDER_BOT:
            continue
        p = rc.get_position(uid)
        if rc.get_team(uid) == my_team:
            if p.distance_squared(core_pos) <= DEFENSE_FRIENDLY_RADIUS_SQ:
                ally_builder_count += 1
                has_close_ally = True
        else:
            d = p.distance_squared(core_pos)
            if (closest_enemy_d is None or d < closest_enemy_d) and d <= 20:
                closest_enemy_d = d
                closest_enemy = p

    return ally_builder_count, has_close_ally, closest_enemy


def run():
    global _spawn_plan, _spawn_tiles
    # if rc.get_current_round() == 200:

    #     rc.resign()
    # Sync round info
    map_info.update()
    comms.read()          # absorb every slot's shared tiles/symmetry, broadcast our own
    comms.write()
    map_info.recompute_derived()
    for i in map_info.iter_mask(map_info._bm_env[map_info._IDX_ENV_WALL]):
        rc.draw_indicator_dot(i, 255, 255, 255)
    # The core's footprint never moves; compute the surrounding spawn ring once
    # _my_core is known (after the first observation).
    if not _spawn_tiles:
        _spawn_tiles = _compute_spawn_tiles()
    titanium = rc.get_global_resources()
    scaling = rc.get_scale_percent()
    core_pos = map_info._my_pos
    my_team = map_info._my_team
    
    # Initialize spawn plan
    if _spawn_plan is None:
        _spawn_plan = choose_spawn_plan(rc, core_pos, INITIAL_SPAWN_COUNT)
    if rc.get_current_round() <= INITIAL_SPAWN_COUNT + INITIAL_EXPLORE_MAX_STEPS:
        draw_spawn_plan(rc, core_pos, _spawn_plan, rc.get_map_width(), rc.get_map_height())

    # Spawn bot toward enemy if we see one and don't have a close ally
    ally_builder_count, has_close_ally, closest_enemy = _scan_nearby_builders(core_pos, my_team)
    if not _spawn_toward_enemy_if_undefended(has_close_ally, closest_enemy):
        
        # Otherwise only spawn if we have extra resources
        threshold = 400 if ally_builder_count >= 12 else 200
        if scaling * SCALE_MULT + threshold < titanium:
            
            # First spawn according to initial plan, then spawn toward center
            if not _spawn_toward_plan(core_pos):
                _spawn_toward_center()
    ammo_amount = min(min(50, rc.get_global_resources()-2), rc.get_current_round() * 2) - rc.get_global_ammo()
    if ammo_amount > 0 and rc.can_convert_ammo(ammo_amount):
        rc.convert_ammo(ammo_amount)
