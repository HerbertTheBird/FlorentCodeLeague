from fcode import Controller, Direction, Position
import comms
import map_info
import units.launch_plan as plan
from units.spawn_plan import choose_spawn_plan, draw_spawn_plan, INITIAL_SPAWN_COUNT, INITIAL_EXPLORE_MAX_STEPS

rc: Controller

_spawn_plan: list[Direction] | None = None
_num_spawned = 0
_core_area: tuple[Position, ...] = ()
# Opening builder ids by spawn index (first NUM_ATTACK are attackers, rest are
# economy — see spawn_plan). Broadcast to comms so each builder learns its role.
_opening_ids = [0] * INITIAL_SPAWN_COUNT


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
    """Record an opening builder's id by spawn index (role is derived from the
    index in comms). The broadcast happens once per round in run()."""
    global _num_spawned
    if 0 <= spawn_index < INITIAL_SPAWN_COUNT:
        _opening_ids[spawn_index] = builder_id
    _num_spawned += 1


def _direct_spawn_tile(spawn_index: int, core_pos: Position):
    """Spawn each role up to two tiles from the core toward its first job."""
    cands = [p for p in _core_area if rc.can_spawn(p)]
    if not cands:
        return None

    if spawn_index == 0:
        # Fallback when no opening-launcher spawn is available: give the
        # attacker the most forward legal core spawn.
        goal = map_info._predicted_enemy_core
        if goal is None and _spawn_plan:
            goal = get_ray_endpoint(
                core_pos, _spawn_plan[spawn_index], map_info._width, map_info._height
            )
    else:
        # Economy builders still start toward their deterministic ore routes.
        ores = plan.ranked_ore_from_core()
        ore_index = spawn_index - 1
        goal = ores[ore_index] if ore_index < len(ores) else None
        if goal is None and _spawn_plan:
            goal = get_ray_endpoint(
                core_pos, _spawn_plan[spawn_index], map_info._width, map_info._height
            )
    goal = goal or core_pos
    return min(cands, key=lambda p: (p.distance_squared(goal), p.x, p.y))


def _launcher_spawn_tile():
    """Spawn beside the planned opening launcher, within its pickup ring."""
    launcher = plan.launcher_position()
    if launcher is None:
        return None
    bit = 1 << (launcher.x + launcher.y * map_info._width)
    have_launcher = bool(
        bit
        & map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
    )

    def in_position(pos: Position) -> bool:
        dx = abs(pos.x - launcher.x)
        dy = abs(pos.y - launcher.y)
        return max(dx, dy) == 1 if have_launcher else dx + dy == 1

    candidates = [
        pos for pos in _core_area if in_position(pos) and rc.can_spawn(pos)
    ]
    if not candidates:
        candidates = [pos for pos in _core_area if rc.can_spawn(pos)]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda pos: (pos.distance_squared(launcher), pos.x, pos.y),
    )


def _spawn_toward_plan(core_pos: Position) -> bool:
    if _spawn_plan is None or _num_spawned >= INITIAL_SPAWN_COUNT:
        return False
    spawn_index = _num_spawned
    tile = _launcher_spawn_tile()
    if tile is None:
        return False
    builder_id = rc.spawn_builder(tile)
    _record_opening_spawn(spawn_index, builder_id)
    return True


def run():
    global _spawn_plan
    # if rc.get_current_round() == 200:
        
    #     rc.resign()
    # Sync round info
    map_info.update()
    # Identify the map from our own core origin and publish its id, then load
    # the whole board (walls, ore, both cores, symmetry) locally. Every other
    # unit reads the id and loads the same board — no per-tile pooling needed.
    comms.publish_identified_map()
    titanium = rc.get_global_resources()




    core_pos = map_info._my_pos
    
    # Initialize spawn plan
    if _spawn_plan is None:
        _spawn_plan = choose_spawn_plan(rc, core_pos, INITIAL_SPAWN_COUNT)
    if rc.get_current_round() <= INITIAL_SPAWN_COUNT + INITIAL_EXPLORE_MAX_STEPS:
        draw_spawn_plan(rc, core_pos, _spawn_plan, rc.get_map_width(), rc.get_map_height())

    # Fixed roster: spawn exactly the opening 3 builders (1 attack + 2 econ) and
    # never grow past it — no reactive or economy-gated growth spawns.
    if _num_spawned < INITIAL_SPAWN_COUNT:
        _spawn_toward_plan(core_pos)

    # Rebroadcast the 1-attack / 2-economy opening role ids (buffered writes need
    # repeating until every builder has recognized its assignment).
    if any(_opening_ids):
        comms.rebroadcast_opening(_opening_ids)
                
    # Global ammo (Titan 2.3.x): turrets fire from a team-wide pool, filled only
    # by the core converting titanium 1:1 (at most once per turn). Slot 15 now
    # carries emergency defender claims, so count locally-known allied gunners
    # and keep the pool topped up for both gunner and sentinel volleys.
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
    local_sentinels = (
        map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[map_info._my_team_idx]
    ).bit_count()
    allied_sentinels = max(comms.sentinel_count(), local_sentinels)
    ammo_target = 2 * (allied_gunners + 1) + 10 * allied_sentinels
    if ammo < ammo_target:
        amt = min(ammo_target - ammo, titanium)
        if amt > 0 and rc.can_convert_ammo(amt):
            rc.convert_ammo(amt)
