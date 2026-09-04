from collections import deque

from fcode import Controller, Direction, Environment, Position
import comms
import map_info
from units.spawn_plan import choose_spawn_plan, draw_spawn_plan, INITIAL_SPAWN_COUNT, INITIAL_EXPLORE_MAX_STEPS

rc: Controller

_spawn_plan: list[Direction] | None = None
_num_spawned = 0
_core_area: tuple[Position, ...] = ()
# Opening builder ids by spawn index. Broadcast to comms so the first builder
# learns its attack role and the remaining three learn stable economy lanes.
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


def _planned_spawn_tile(core_pos: Position, direction: Direction):
    """Choose the legal spawn tile farthest along an economy lane's ray.

    Builders can spawn up to two tiles from the 2x2 core, so use that full
    region directly instead of clustering them around an opening launcher.
    """
    dx, dy = map_info._DIRECTION_DELTAS[direction]
    center_x2 = 2 * core_pos.x + 1
    center_y2 = 2 * core_pos.y + 1
    candidates = [p for p in _core_area if rc.can_spawn(p)]
    if not candidates:
        return None

    def key(p):
        rel_x2 = 2 * p.x - center_x2
        rel_y2 = 2 * p.y - center_y2
        projection = rel_x2 * dx + rel_y2 * dy
        perpendicular = abs(rel_x2 * dy - rel_y2 * dx)
        return (-projection, perpendicular, p.x, p.y)

    return min(candidates, key=key)


def _visible_titanium() -> tuple[Position, ...]:
    """Titanium tiles actually visible to the core this turn."""
    return tuple(
        p for p in rc.get_nearby_tiles()
        if rc.get_tile_env(p) == Environment.ORE_TITANIUM
    )


def _route_distances(starts: tuple[Position, ...]) -> list[int]:
    """Cardinal walking distances from one or more opening spawn tiles."""
    width = map_info._width
    height = map_info._height
    size = width * height
    distance = [-1] * size
    blocked = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
    )
    queue = deque()
    for p in starts:
        n = p.x + p.y * width
        if distance[n] < 0:
            distance[n] = 0
            queue.append(n)

    while queue:
        n = queue.popleft()
        x, y = n % width, n // width
        next_distance = distance[n] + 1
        for nx, ny in ((x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y)):
            if not (0 <= nx < width and 0 <= ny < height):
                continue
            nn = nx + ny * width
            if distance[nn] >= 0 or blocked & (1 << nn):
                continue
            distance[nn] = next_distance
            queue.append(nn)
    return distance


def _ore_approach_distance(ore: Position, distance: list[int]) -> int | None:
    """Shortest distance to a cardinal tile from which the ore can be built."""
    width = map_info._width
    best = None
    for x, y in (
        (ore.x, ore.y - 1),
        (ore.x + 1, ore.y),
        (ore.x, ore.y + 1),
        (ore.x - 1, ore.y),
    ):
        if not (0 <= x < width and 0 <= y < map_info._height):
            continue
        d = distance[x + y * width]
        if d >= 0 and (best is None or d < best):
            best = d
    return best


def _resource_spawn_tile() -> Position | None:
    """Best legal spawn tile toward the route-nearest visible titanium.

    Route distance, rather than squared/straight-line distance, keeps the
    second builder on the correct side of walls and narrow core exits.
    """
    ore = _visible_titanium()
    candidates = tuple(p for p in _core_area if rc.can_spawn(p))
    if not ore or not candidates:
        return None

    # Select the mine by its best legal opening route from the core region.
    all_distances = _route_distances(candidates)
    reachable = []
    for p in ore:
        d = _ore_approach_distance(p, all_distances)
        if d is not None:
            reachable.append((d, p.x, p.y, p))
    if not reachable:
        return None
    _distance, _x, _y, target = min(reachable)
    comms.publish_opening_ore(target)

    # Then use the legal spawn tile that is furthest along that same route.
    best = None
    for spawn in candidates:
        distances = _route_distances((spawn,))
        d = _ore_approach_distance(target, distances)
        if d is None:
            continue
        key = (d, spawn.distance_squared(target), spawn.x, spawn.y)
        if best is None or key < best[0]:
            best = (key, spawn)
    return None if best is None else best[1]


def _spawn_toward_plan(core_pos: Position) -> bool:
    if _spawn_plan is None or _num_spawned >= INITIAL_SPAWN_COUNT:
        return False
    spawn_index = _num_spawned
    # Builder #2 is the first economy builder. If the core can see titanium,
    # start it as far along the shortest cardinal route to that ore as possible.
    tile = _resource_spawn_tile() if spawn_index == 1 else None
    if tile is None:
        tile = _planned_spawn_tile(core_pos, _spawn_plan[spawn_index])
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

    # Fixed roster: one immediate attacker and three economy/defense builders.
    if _num_spawned < INITIAL_SPAWN_COUNT:
        _spawn_toward_plan(core_pos)

    # Rebroadcast all four opening role ids (buffered writes need
    # repeating until every builder has recognized its assignment).
    if any(_opening_ids):
        comms.rebroadcast_opening(_opening_ids)

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
