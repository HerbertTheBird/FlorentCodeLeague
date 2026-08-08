from fcode import Controller, Direction, Position, EntityType
import comms
import map_info
import units.def_states.defense as defense
from units.spawn_plan import choose_spawn_plan, draw_spawn_plan, INITIAL_SPAWN_COUNT, INITIAL_EXPLORE_MAX_STEPS

rc: Controller

# --- Configurable ---
SCALE_MULT = 1
DEFENSE_FRIENDLY_RADIUS_SQ = 36

_spawn_plan: list[Direction] | None = None
_num_spawned = 0
_core_area: tuple[Position, ...] = ()
# Heimdall_v3 opening: one launcher defender, two attackers, two economy bots.
_defender_id = 0
_atk_ids = [0, 0]
_econ_ids = [0, 0]


def _core_area_positions(pos: Position, width: int, height: int) -> tuple[Position, ...]:
    # Titan core is 2x2 with top-left = pos (get_position()). Builders may spawn
    # up to two tiles from any core tile. Offer that full clipped region to
    # can_spawn(); the controller remains the authority on exact legality.
    footprint = {(pos.x + dx, pos.y + dy) for dx in (0, 1) for dy in (0, 1)}
    return tuple(
        Position(x, y)
        for x in range(pos.x - 2, pos.x + 4)
        for y in range(pos.y - 2, pos.y + 4)
        if 0 <= x < width and 0 <= y < height and (x, y) not in footprint
    )


def init(c: Controller):
    global rc, _core_area
    rc = c
    _core_area = _core_area_positions(
        rc.get_position(), rc.get_map_width(), rc.get_map_height()
    )


def _record_opening_spawn(spawn_index: int, builder_id: int) -> None:
    """Record the opening role (defense, attack, attack, economy, economy) for
    this builder. The actual mailbox writes happen once per round in run()."""
    global _num_spawned, _defender_id
    if spawn_index == 0:
        _defender_id = builder_id
    elif spawn_index in (1, 2):
        _atk_ids[spawn_index - 1] = builder_id
    elif spawn_index in (3, 4):
        _econ_ids[spawn_index - 3] = builder_id
    _num_spawned += 1


def _spawn_toward_plan(core_pos: Position) -> bool:
    if _spawn_plan is None or _num_spawned >= len(_spawn_plan):
        return False

    spawn_index = _num_spawned

    # Put the opening defender cardinally beside its first launcher so it can
    # build immediately on its first active turn.
    if spawn_index == 0:
        first_launcher = defense.next_launcher_site(0)
        if first_launcher is not None:
            candidates = []
            for direction in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
                pos = map_info.pos_add(first_launcher, direction)
                if pos in _core_area and rc.can_spawn(pos):
                    candidates.append((pos.distance_squared(first_launcher), pos.x, pos.y, pos))
            if candidates:
                spawn_pos = min(candidates)[-1]
                builder_id = rc.spawn_builder(spawn_pos)
                _record_opening_spawn(spawn_index, builder_id)
                return True
        planned_dir = _spawn_plan[0]
    elif spawn_index in (1, 2):
        planned_dir = _spawn_plan[min(spawn_index - 1, len(_spawn_plan) - 1)]
    else:  # economy (spawn 3, 4) -> farthest rays
        planned_dir = _spawn_plan[-(spawn_index - 2)] if len(_spawn_plan) >= spawn_index - 2 else _spawn_plan[-1]
    tried = set()
    for d in (planned_dir, planned_dir.rotate_left(), planned_dir.rotate_right()):
        p = map_info.pos_add(core_pos, d)
        tried.add(p)
        if map_info.in_bounds(p) and rc.can_spawn(p):
            builder_id = rc.spawn_builder(p)
            _record_opening_spawn(spawn_index, builder_id)
            return True

    # A 2x2 core's usable spawn ring is larger than the three Cambridge-era
    # offsets above. If those are obstructed, search every legal ring tile,
    # preserving the intended direction as closely as possible.
    dx, dy = map_info._DIRECTION_DELTAS[planned_dir]
    ray_target = Position(core_pos.x + 10 * dx, core_pos.y + 10 * dy)
    for p in sorted(_core_area, key=lambda tile: (tile.distance_squared(ray_target), tile.x, tile.y)):
        if p in tried or not rc.can_spawn(p):
            continue
        builder_id = rc.spawn_builder(p)
        _record_opening_spawn(spawn_index, builder_id)
        return True
    return False


def _spawn_toward_center():
    """Spawn on the core tile closest to map center."""
    center = Position(map_info._width//2, map_info._height//2)
    best = None
    best_dist = float('inf')
    for p in _core_area:
        if rc.can_spawn(p):
            d = p.distance_squared(center)
            if d < best_dist:
                best_dist = d
                best = p
    if best is not None:
        rc.spawn_builder(best)


def _spawn_toward_enemy_if_undefended(has_close_ally: bool, closest_enemy: Position | None) -> bool:
    """If an enemy builder bot is in vision and no friendly builder bot sits
    within dist² DEFENSE_FRIENDLY_RADIUS_SQ of the core, spawn a defender on
    the core tile closest to the nearest enemy bot. Returns True if spawned."""
    if has_close_ally or closest_enemy is None:
        return False
    best = None
    best_d = None
    for p in _core_area:
        if rc.can_spawn(p):
            d = p.distance_squared(closest_enemy)
            if best_d is None or d < best_d:
                best_d = d
                best = p
    if best is None:
        return False
    rc.spawn_builder(best)
    return True


def _spawn_claimed_reinforcement() -> bool:
    """Spawn an on-demand defender beside the launcher that requested it."""
    claim = comms.reinforcement_claim()
    if claim is None:
        return False
    _enemy_id, defender_id, launcher_pos, lane = claim
    if defender_id:
        return False
    candidates = []
    for pos in _core_area:
        if not rc.can_spawn(pos) or max(
            abs(pos.x - launcher_pos.x), abs(pos.y - launcher_pos.y)
        ) > 1:
            continue
        candidates.append((
            pos.distance_squared(launcher_pos),
            pos.x,
            pos.y,
            pos,
        ))
    if not candidates:
        return False
    spawn_pos = min(candidates)[-1]
    builder_id = rc.spawn_builder(spawn_pos)
    comms.assign_reinforcement(builder_id)
    return True


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
    global _spawn_plan
    # if rc.get_current_round() == 200:
        
    #     rc.resign()
    # Sync round info
    map_info.update()
    # Publish our position so units that never see the core (e.g. a launcher
    # built far out) can still compute symmetry targets.
    comms.publish_core_pos(map_info._my_pos)
    titanium = rc.get_global_resources()




    scaling = rc.get_scale_percent()
    core_pos = map_info._my_pos
    my_team = map_info._my_team
    
    # Initialize spawn plan
    if _spawn_plan is None:
        _spawn_plan = choose_spawn_plan(rc, core_pos, INITIAL_SPAWN_COUNT)
    if rc.get_current_round() <= INITIAL_SPAWN_COUNT + INITIAL_EXPLORE_MAX_STEPS:
        draw_spawn_plan(rc, core_pos, _spawn_plan, rc.get_map_width(), rc.get_map_height())

    ally_builder_count, has_close_ally, closest_enemy = _scan_nearby_builders(core_pos, my_team)
    # The opening composition is mandatory: defense, attack, attack, econ, econ.
    # It takes precedence over reactive spawns so every early builder receives
    # the intended role. can_spawn() checks affordability/cooldown/cap/occupancy.
    if _num_spawned < INITIAL_SPAWN_COUNT:
        _spawn_toward_plan(core_pos)
    else:
        # Launcher-issued mirror reinforcements take precedence over economy.
        if _spawn_claimed_reinforcement():
            pass
        else:
            # Do not create unassigned reactive defenders: launchers are the
            # authority on whether an intruder lacks a mirror claim.
            threshold = 400 if ally_builder_count >= 12 else 200
            if scaling * SCALE_MULT + threshold < titanium:
                _spawn_toward_center()

    # Rebroadcast the v3 opening role ids (buffered writes need
    # repeating until every builder has recognized its assignment).
    if _defender_id or any(_atk_ids) or any(_econ_ids):
        comms.rebroadcast_opening(_defender_id, _atk_ids, _econ_ids)
                
    # Global ammo (Titan 2.3.x): turrets fire from a team-wide pool, filled only
    # by the core converting titanium 1:1 (at most once per turn). Slot 15 now
    # carries emergency defender claims, so count locally-known allied gunners
    # and keep the pool topped up to 2 * (gunners + 1).
    ammo = rc.get_global_ammo()
    # The core's vision is local, so it can't see gunners built out on the map;
    # counting only locally-visible ones capped the target at 2 (one gunner) and
    # starved the rest. Use the team-wide counter builders maintain, floored by
    # whatever the core can see itself.
    local_gunners = (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[map_info._my_team_idx]
    ).bit_count()
    allied_gunners = max(comms.gunner_count(), local_gunners)
    ammo_target = 2 * (allied_gunners + 1)
    if ammo < ammo_target:
        amt = min(ammo_target - ammo, titanium)
        if amt > 0 and rc.can_convert_ammo(amt):
            rc.convert_ammo(amt)
