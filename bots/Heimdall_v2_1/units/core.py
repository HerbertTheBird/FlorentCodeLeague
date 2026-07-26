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
# Heimdall_v2_1 opening follows v0's defender-first sequencing: two launcher
# defenders establish the ring, then two coordinated attackers spawn, followed
# by the economy builder. The lane mailboxes retain their original high-half
# attacker / low-half defender split.
_atk_ids = [0, 0]
_def_ids = [0, 0]
_economy_id = 0
_direct_core_opening = False


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


def _opening_role(spawn_index: int) -> tuple[str, int]:
    """Small visible-core maps rush immediately; normal maps restore v0 order."""
    if _direct_core_opening:
        roles = (("atk", 0), ("atk", 1), ("econ", 0), ("def", 0), ("def", 1))
    else:
        roles = (("def", 0), ("def", 1), ("atk", 0), ("atk", 1), ("econ", 0))
    return roles[spawn_index]


def _record_opening_spawn(spawn_index: int, builder_id: int) -> None:
    """Record the dynamically ordered five-role opening."""
    global _num_spawned, _economy_id
    role, index = _opening_role(spawn_index)
    if role == "def":
        _def_ids[index] = builder_id
    elif role == "atk":
        _atk_ids[index] = builder_id
    else:
        _economy_id = builder_id
    _num_spawned += 1


def _spawn_toward_plan(core_pos: Position) -> bool:
    if _spawn_plan is None or _num_spawned >= len(_spawn_plan):
        return False

    spawn_index = _num_spawned
    role, role_index = _opening_role(spawn_index)

    # Spawn each defender cardinally beside its first launcher whenever the
    # core's full two-tile spawn radius permits an immediate first-turn build.
    initial_launcher = (
        defense.next_launcher_site(role_index)
        if role == "def" and not _direct_core_opening
        else None
    )
    if initial_launcher is not None:
        for p in sorted(
            _core_area,
            key=lambda tile: (
                abs(tile.x - initial_launcher.x) + abs(tile.y - initial_launcher.y) != 1,
                tile.distance_squared(initial_launcher), tile.x, tile.y,
            ),
        ):
            if rc.can_spawn(p):
                builder_id = rc.spawn_builder(p)
                _record_opening_spawn(spawn_index, builder_id)
                return True

    if initial_launcher is not None:
        planned_dir = map_info.direction_to(core_pos, initial_launcher)
    elif role == "atk":
        planned_dir = _spawn_plan[min(role_index, len(_spawn_plan) - 1)]
    elif role == "econ":
        planned_dir = _spawn_plan[-1]
    else:
        target = map_info._their_core or Position(map_info._width // 2, map_info._height // 2)
        planned_dir = map_info.direction_to(core_pos, target)
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
    """Spawn the third claimed defender beside a launch-capable launcher."""
    claim = comms.reinforcement_claim()
    if claim is None:
        return False
    _enemy_id, defender_id, enemy_pos, _launched = claim
    if defender_id or not all(comms.defender_intercepting(lane) for lane in (0, 1)):
        return False

    launchers = (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    if not launchers:
        return False
    launcher_positions = tuple(map_info.iter_mask(launchers))
    candidates = []
    for pos in _core_area:
        if not rc.can_spawn(pos):
            continue
        adjacent_launchers = [
            launcher for launcher in launcher_positions
            if max(abs(pos.x - launcher.x), abs(pos.y - launcher.y)) <= 1
        ]
        if not adjacent_launchers:
            continue
        candidates.append((
            min(launcher.distance_squared(enemy_pos) for launcher in adjacent_launchers),
            pos.distance_squared(enemy_pos),
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
    global _spawn_plan, _direct_core_opening
    # if rc.get_current_round() == 200:

    #     rc.resign()
    # Sync round info
    map_info.update()
    if _num_spawned == 0:
        _direct_core_opening = bool(
            map_info._bm_their_core_area & map_info._bm_visible
        )
    if _direct_core_opening:
        # Every defender adopts PvP immediately, even if its spawn tile happens
        # to lose direct sight of the nearby enemy core.
        comms.mark_gunner_pvp()
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
    # The five-role opening is mandatory: defense, defense, attack, attack,
    # economy. This restores v0's ring-first timing.
    # It takes precedence over reactive spawns so every early builder receives
    # the intended role. can_spawn() checks affordability/cooldown/cap/occupancy.
    if _num_spawned < INITIAL_SPAWN_COUNT:
        _spawn_toward_plan(core_pos)
    else:
        # A third unclaimed intruder takes precedence over ordinary reactive or
        # economy spawning once both permanent defenders have been launched.
        if _spawn_claimed_reinforcement():
            pass
        # After the five opening roles exist, restore Loki's reactive spawn.
        elif not _spawn_toward_enemy_if_undefended(has_close_ally, closest_enemy):
            # Once the opening five exist, retain Loki's conservative economy
            # gate for all additional builders.
            threshold = 400 if ally_builder_count >= 12 else 200
            if scaling * SCALE_MULT + threshold < titanium:
                _spawn_toward_center()

    # One combined write per lane prevents buffered attacker/defender role
    # assignments from clobbering one another.
    for lane in (0, 1):
        if _atk_ids[lane] or _def_ids[lane]:
            comms.rebroadcast_lane(lane, _atk_ids[lane], _def_ids[lane])
    if _economy_id:
        comms.assign_economy(_economy_id)

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
