"""Dedicated two-builder defense for Heimdall v0.

The launcher layout is recalculated from current terrain and structures every
turn. A greedy set-cover pass prioritizes the radius² 9..16 outer ring, prunes
redundant placements, and then assigns the remaining sites to stable clockwise
and counterclockwise half-rings. Ore remains a coverage target but is never a
launcher candidate.
"""

import math

from fcode import Controller, Direction, EntityType, Position

import comms
import map_info
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

target = None  # current launcher build site, for status logging
_wait_round = -1
_wait_position: Position | None = None
_patrol_mode = False
_patrol_target: Position | None = None
_first_launcher_site: Position | None = None
_first_launcher_id = 0
_mirror_enemy_position: Position | None = None
_blocked_site_until: dict[int, int] = {}
_launchers_placed = 0
_gunner_pvp_mode = False
_pvp_focus: Position | None = None
_region_cache_key = None
_region_cache_value = (0, 0, 0, 0)

_CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)


def init(c: Controller, pathfinder: Pathing) -> None:
    global rc, nav, _patrol_mode, _patrol_target, _blocked_site_until
    global _launchers_placed, _gunner_pvp_mode, _pvp_focus
    global _first_launcher_site, _first_launcher_id, _mirror_enemy_position
    rc = c
    nav = pathfinder
    _patrol_mode = False
    _patrol_target = None
    _blocked_site_until = {}
    _launchers_placed = 0
    _gunner_pvp_mode = False
    _pvp_focus = None
    _first_launcher_site = None
    _first_launcher_id = 0
    _mirror_enemy_position = None


def enemy_visible() -> bool:
    """Whether any currently visible enemy builder or structure exists."""
    if map_info._bm_enemy_bots:
        return True
    enemy_buildings = (
        map_info._bm_any_building
        & map_info._bm_team[1 - map_info._my_team_idx]
        & map_info._bm_visible
    )
    return bool(enemy_buildings)


def _bit(pos: Position) -> int:
    return 1 << (pos.x + pos.y * map_info._width)


def _zone(pos: Position) -> int:
    """The launcher's exact 3x3 coverage, clipped by map_info at map edges."""
    return map_info.expand_chebyshev(_bit(pos)) | _bit(pos)


def _region_masks() -> tuple[int, int, int, int]:
    """Return (radius16, radius9, outer_ring, core) masks for our 2x2 core."""
    global _region_cache_key, _region_cache_value
    core = map_info._my_core
    key = (core.x, core.y, map_info._width, map_info._height)
    if key == _region_cache_key:
        return _region_cache_value

    radius16 = 0
    radius9 = 0
    core_mask = 0
    for y in range(max(0, core.y - 4), min(map_info._height, core.y + 6)):
        for x in range(max(0, core.x - 4), min(map_info._width, core.x + 6)):
            if core.x <= x <= core.x + 1 and core.y <= y <= core.y + 1:
                core_mask |= 1 << (x + y * map_info._width)
                continue
            dsq = min(
                (x - cx) * (x - cx) + (y - cy) * (y - cy)
                for cx in (core.x, core.x + 1)
                for cy in (core.y, core.y + 1)
            )
            if dsq <= 16:
                bit = 1 << (x + y * map_info._width)
                radius16 |= bit
                if dsq <= 9:
                    radius9 |= bit

    _region_cache_key = key
    _region_cache_value = (radius16, radius9, radius16 & ~radius9, core_mask)
    return _region_cache_value


def _defense_target() -> int:
    """Passable tiles in the radius² 9..16 shell, excluding allied structures."""
    _radius16, _radius9, outer_ring, _core = _region_masks()
    result = outer_ring & ~map_info._bm_env[map_info._IDX_ENV_WALL]

    allied_structures = map_info._bm_any_building & map_info._bm_team[map_info._my_team_idx]
    return result & ~allied_structures


def _allied_launchers() -> int:
    return (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
    )


def _empty_build_site(pos: Position) -> bool:
    if not map_info.in_bounds(pos):
        return False
    bit = _bit(pos)
    blocked_environment = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    )
    if blocked_environment & bit:
        return False
    return not bool(map_info._bm_any_building & bit)


def _uncovered() -> tuple[int, int]:
    target = _defense_target()
    launchers = _allied_launchers()
    coverage = map_info.expand_chebyshev(launchers) | launchers if launchers else 0
    return target, target & ~coverage


def _candidate_mask() -> int:
    """Legal launcher centres capable of covering at least one shell tile."""
    _radius16, _radius9, outer_ring, core_mask = _region_masks()
    forbidden = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
        | core_mask
    )
    current_round = (rc or map_info._rc).get_current_round()
    expired = [n for n, until in _blocked_site_until.items() if until <= current_round]
    for n in expired:
        del _blocked_site_until[n]
    for n in _blocked_site_until:
        forbidden |= 1 << n
    support = map_info.expand_chebyshev(outer_ring) | outer_ring
    return support & ~forbidden


def _calculate_tiling() -> list[Position]:
    """Calculate a low-overlap launcher cover for the shell.

    A greedy solution supplies a fast upper bound. A bounded branch-and-bound
    set-cover pass then minimizes launcher count and, among equal-size covers,
    minimizes repeated shell coverage. If the search budget is exhausted, the
    best complete cover found so far is retained.
    """
    target, uncovered = _uncovered()
    if not uncovered:
        return []

    candidates = _candidate_mask()
    zone_by_tile: dict[int, int] = {}
    full_zone_by_tile: dict[int, int] = {}
    scan = candidates
    while scan:
        lsb = scan & -scan
        scan ^= lsb
        n = lsb.bit_length() - 1
        full_zone = map_info.expand_chebyshev(lsb) | lsb
        cover = full_zone & target
        if cover & uncovered:
            zone_by_tile[n] = cover & uncovered
            full_zone_by_tile[n] = cover

    reachable = 0
    for cover in zone_by_tile.values():
        reachable |= cover
    remaining_initial = uncovered & reachable
    if not remaining_initial:
        return []

    candidate_tiles = tuple(zone_by_tile)
    coverers: dict[int, list[int]] = {}
    remaining_scan = remaining_initial
    while remaining_scan:
        bit = remaining_scan & -remaining_scan
        remaining_scan ^= bit
        coverers[bit.bit_length() - 1] = [
            n for n in candidate_tiles if zone_by_tile[n] & bit
        ]

    # Greedy upper bound: cover the most new shell tiles, then prefer the
    # candidate with fewer total shell hits (less eventual overlap).
    greedy = []
    remaining = remaining_initial
    while remaining:
        best_n = max(
            candidate_tiles,
            key=lambda n: (
                (zone_by_tile[n] & remaining).bit_count(),
                -full_zone_by_tile[n].bit_count(),
                -n,
            ),
        )
        if not (zone_by_tile[best_n] & remaining):
            break
        greedy.append(best_n)
        remaining &= ~zone_by_tile[best_n]
    if remaining:
        return []

    best_selected = list(greedy)
    best_objective = (
        len(greedy),
        sum(full_zone_by_tile[n].bit_count() for n in greedy),
    )
    seen: dict[int, tuple[int, int]] = {}
    node_budget = 12000
    nodes = 0

    def search(remaining_mask: int, selected: list[int], hit_sum: int) -> None:
        nonlocal best_selected, best_objective, nodes
        nodes += 1
        if nodes > node_budget:
            return
        if not remaining_mask:
            objective = (len(selected), hit_sum)
            if objective < best_objective:
                best_objective = objective
                best_selected = list(selected)
            return
        if len(selected) >= best_objective[0]:
            return

        max_gain = max(
            (zone_by_tile[n] & remaining_mask).bit_count()
            for n in candidate_tiles
        )
        if max_gain == 0:
            return
        lower_bound = (remaining_mask.bit_count() + max_gain - 1) // max_gain
        if len(selected) + lower_bound > best_objective[0]:
            return

        previous = seen.get(remaining_mask)
        state_cost = (len(selected), hit_sum)
        if previous is not None and previous <= state_cost:
            return
        seen[remaining_mask] = state_cost

        # Branch on the hardest uncovered shell tile first.
        scan_mask = remaining_mask
        chosen_bit_n = None
        options = None
        while scan_mask:
            bit = scan_mask & -scan_mask
            scan_mask ^= bit
            bit_n = bit.bit_length() - 1
            valid = [n for n in coverers[bit_n] if zone_by_tile[n] & remaining_mask]
            if options is None or len(valid) < len(options):
                chosen_bit_n = bit_n
                options = valid
                if len(options) == 1:
                    break
        if chosen_bit_n is None or not options:
            return
        options.sort(
            key=lambda n: (
                -(zone_by_tile[n] & remaining_mask).bit_count(),
                full_zone_by_tile[n].bit_count(),
                n,
            )
        )
        for n in options:
            selected.append(n)
            search(
                remaining_mask & ~zone_by_tile[n],
                selected,
                hit_sum + full_zone_by_tile[n].bit_count(),
            )
            selected.pop()

    search(remaining_initial, [], 0)
    return [
        Position(n % map_info._width, n // map_info._width)
        for n in best_selected
    ]


def _clockwise_angle(pos: Position) -> float:
    core = map_info._my_core
    dx = pos.x - (core.x + 0.5)
    dy = pos.y - (core.y + 0.5)
    return math.atan2(dx, -dy) % math.tau


def intruder_lane(pos: Position) -> int:
    """Assign every map position to exactly one defender half-ring."""
    core = map_info._my_core
    center_dx = map_info._width / 2 - (core.x + 0.5)
    center_dy = map_info._height / 2 - (core.y + 0.5)
    center_angle = math.atan2(center_dx, -center_dy) % math.tau
    clockwise_delta = (_clockwise_angle(pos) - center_angle) % math.tau
    return 0 if 0 < clockwise_delta <= math.pi else 1


def coreward_block_tile(enemy: Position) -> Position:
    """Cardinal tile beside an intruder on its dominant coreward axis."""
    core = map_info._my_core
    nearest_core_x = min(max(enemy.x, core.x), core.x + 1)
    nearest_core_y = min(max(enemy.y, core.y), core.y + 1)
    dx = enemy.x - nearest_core_x
    dy = enemy.y - nearest_core_y
    if abs(dx) >= abs(dy):
        step = 0 if dx == 0 else (1 if dx > 0 else -1)
        return Position(enemy.x - step, enemy.y)
    step = 0 if dy == 0 else (1 if dy > 0 else -1)
    return Position(enemy.x, enemy.y - step)


def _lane_sequence(plan: list[Position], lane: int) -> list[Position]:
    """Assign sites to stable clockwise/counterclockwise half-rings."""
    if not plan:
        return []
    core = map_info._my_core
    center_dx = map_info._width / 2 - (core.x + 0.5)
    center_dy = map_info._height / 2 - (core.y + 0.5)
    center_angle = math.atan2(center_dx, -center_dy) % math.tau
    epsilon = 1e-9
    owned = []
    for pos in plan:
        clockwise_delta = (_clockwise_angle(pos) - center_angle) % math.tau
        if lane == 0:
            if epsilon < clockwise_delta <= math.pi + epsilon:
                owned.append((clockwise_delta, pos))
        elif clockwise_delta > math.pi + epsilon or clockwise_delta <= epsilon:
            # The directly center-facing site belongs to this lane but sorts
            # last, so both defenders' first launchers remain slightly offset.
            ccw_delta = (math.tau - clockwise_delta) % math.tau
            if clockwise_delta <= epsilon:
                ccw_delta = math.tau
            owned.append((ccw_delta, pos))
    owned.sort(key=lambda item: (item[0], item[1].x + item[1].y * map_info._width))
    return [pos for _delta, pos in owned]


def _next_site(lane: int) -> Position | None:
    plan = _calculate_tiling()
    if not plan:
        return None
    sequence = _lane_sequence(plan, lane)
    _target, uncovered = _uncovered()
    _radius16, _radius9, outer_ring, _core = _region_masks()
    outer_uncovered = uncovered & outer_ring
    if outer_uncovered:
        planned_outer = 0
        for site in plan:
            planned_outer |= _zone(site) & outer_uncovered
        # If current terrain/ore makes part of the outer ring impossible to
        # cover, do not let that impossible remainder freeze all inner work.
        if not planned_outer:
            return sequence[0] if sequence else plan[0]
        for site in sequence:
            if _zone(site) & planned_outer:
                return site
    if sequence:
        return sequence[0]
    # This lane is complete. Never wrap around and take a site owned by the
    # other defender, even while that half still has uncovered tiles.
    return None


def next_launcher_site(lane: int) -> Position | None:
    """Public planner entry point shared by the core and defense builders."""
    global target
    target = _next_site(lane)
    return target


def _wait_for_new_launcher() -> bool:
    global _wait_round, _wait_position
    current_round = rc.get_current_round()
    if current_round == _wait_round and map_info._my_pos == _wait_position:
        return True
    if current_round >= _wait_round:
        _wait_round = -1
        _wait_position = None
    return False


def _build_and_publish(lane: int, target: Position) -> bool:
    global _wait_round, _wait_position
    dx = abs(target.x - map_info._my_pos.x)
    dy = abs(target.y - map_info._my_pos.y)
    # Builders can only place onto a cardinally adjacent tile. This is distinct
    # from the launcher's 3x3 pickup/coverage neighbourhood.
    if dx + dy != 1:
        return False
    if not _empty_build_site(target):
        return False
    if not rc.can_build_launcher(target):
        return False
    if rc.get_global_resources() < rc.get_launcher_cost() + map_info.builder_ti_reserve():
        return False

    launcher_id = rc.build(EntityType.LAUNCHER, target)
    _publish_built_launcher(lane, target, launcher_id)
    return True


def _publish_built_launcher(lane: int, target: Position, launcher_id: int) -> None:
    """Record every launcher build, including opportunistic combat builds."""
    global _wait_round, _wait_position, _launchers_placed
    global _first_launcher_site, _first_launcher_id
    _launchers_placed += 1
    if _first_launcher_id == 0:
        _first_launcher_id = launcher_id
        _first_launcher_site = target
    map_info.update_at(target)
    next_pos = next_launcher_site(lane)
    comms.publish_launcher_handoff(lane, rc.get_id(), launcher_id, next_pos)
    if next_pos is not None:
        _wait_round = rc.get_current_round() + 1
        _wait_position = map_info._my_pos


def _move_into_build_range(target: Position) -> bool:
    """Path toward, rather than merely step into, a tile adjacent to target."""
    destinations = set()
    for direction in _CARDINALS:
        pos = map_info.pos_add(target, direction)
        if not map_info.in_bounds(pos):
            continue
        if pos == map_info._my_pos:
            return False
        if not map_info.is_passable(pos):
            continue
        if rc.is_in_vision(pos) and rc.get_tile_builder_bot_id(pos):
            continue
        destinations.add(pos)
    if not destinations:
        return False
    return nav.move_to(destinations, avoid_turret=False)


def _temporarily_skip_stuck_site(target: Position) -> None:
    """Let the dynamic tiler select an alternate around new obstructions."""
    n = target.x + target.y * map_info._width
    _blocked_site_until[n] = rc.get_current_round() + 8


def _distance_from_core(pos: Position) -> int:
    core = map_info._my_core
    return min(
        (pos.x - x) * (pos.x - x) + (pos.y - y) * (pos.y - y)
        for x in (core.x, core.x + 1)
        for y in (core.y, core.y + 1)
    )


def _empty_unoccupied(pos: Position) -> bool:
    if not _empty_build_site(pos) or not rc.is_in_vision(pos):
        return False
    bit = _bit(pos)
    return not bool((map_info._bm_friendly_bots | map_info._bm_enemy_bots) & bit)


def _core_ray(origin: Position) -> tuple[Direction, list[Position]] | None:
    """Shortest gunner ray from ``origin`` to one of our four core tiles.

    The returned path excludes the gunner tile and core tile. Walls or occupied
    intermediate tiles mean the line is already defended and are rejected.
    """
    core = map_info._my_core
    best = None
    for core_y in (core.y, core.y + 1):
        for core_x in (core.x, core.x + 1):
            dx = core_x - origin.x
            dy = core_y - origin.y
            if dx == 0 and dy == 0:
                continue
            if dx != 0 and dy != 0 and abs(dx) != abs(dy):
                continue
            step_x = 0 if dx == 0 else (1 if dx > 0 else -1)
            step_y = 0 if dy == 0 else (1 if dy > 0 else -1)
            steps = max(abs(dx), abs(dy))
            direction = map_info.direction_to(origin, Position(core_x, core_y))
            dir_index = map_info._DIRECTIONS.index(direction)
            if steps > len(map_info._GUNNER_RAYS[dir_index]):
                continue
            path = [
                Position(origin.x + step_x * i, origin.y + step_y * i)
                for i in range(1, steps)
            ]
            blocked = False
            for pos in path:
                bit = _bit(pos)
                if (
                    map_info._bm_env[map_info._IDX_ENV_WALL] & bit
                    or map_info._bm_any_building & bit
                ):
                    blocked = True
                    break
            if blocked:
                continue
            key = (steps, core_x, core_y)
            if best is None or key < best[0]:
                best = (key, direction, path)
    if best is None:
        return None
    return best[1], best[2]


def _build_or_approach_barrier(target: Position) -> bool:
    """Claim the turn to block ``target``, building now or approaching it."""
    if not _empty_unoccupied(target):
        return False
    distance = abs(target.x - map_info._my_pos.x) + abs(target.y - map_info._my_pos.y)
    if distance == 1:
        if (
            rc.can_build_barrier(target)
            and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.builder_ti_reserve()
        ):
            rc.build_barrier(target)
            map_info.update_at(target)
        return True
    _move_into_build_range(target)
    return True


def _visible_enemy_gunner_barrier_targets() -> list[Position]:
    enemy_idx = 1 - map_info._my_team_idx
    gunners = (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[enemy_idx]
        & map_info._bm_visible
    )
    targets = set()
    for gunner_pos in map_info.iter_mask(gunners):
        ray = _core_ray(gunner_pos)
        if ray is None:
            continue
        _direction, path = ray
        for pos in path:
            if _empty_unoccupied(pos):
                targets.add(pos)
    return sorted(
        targets,
        key=lambda pos: (
            pos.distance_squared(map_info._my_pos),
            _distance_from_core(pos),
            pos.x + pos.y * map_info._width,
        ),
    )


def _predicted_enemy_gunner_tiles() -> set[Position]:
    """Open core-threatening gunner placements cardinally beside enemy bots."""
    result = set()
    for enemy_pos in map_info.iter_mask(map_info._bm_enemy_bots):
        for direction in _CARDINALS:
            pos = map_info.pos_add(enemy_pos, direction)
            if not map_info.in_bounds(pos) or _distance_from_core(pos) > 9:
                continue
            if not _empty_unoccupied(pos):
                continue
            if _core_ray(pos) is not None:
                result.add(pos)
    return result


def _barrier_between_predicted_gunner_and_core(gunner_tile: Position) -> Position | None:
    ray = _core_ray(gunner_tile)
    if ray is None:
        return None
    _direction, path = ray
    candidates = [pos for pos in path if _empty_unoccupied(pos)]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda pos: (
            pos.distance_squared(map_info._my_pos),
            _distance_from_core(pos),
            pos.x + pos.y * map_info._width,
        ),
    )


def _try_cover_predicted_tiles_with_gunner(gunner_tiles: set[Position]) -> bool:
    if len(gunner_tiles) <= 2:
        return False
    target_mask = 0
    for pos in gunner_tiles:
        target_mask |= _bit(pos)

    plans = []
    for build_direction in _CARDINALS:
        build_pos = map_info.pos_add(map_info._my_pos, build_direction)
        if not map_info.in_bounds(build_pos) or not _empty_unoccupied(build_pos):
            continue
        for direction_index, facing in enumerate(map_info._DIRECTIONS):
            hits = 0
            for dx, dy in map_info._GUNNER_RAYS[direction_index]:
                ray_pos = Position(build_pos.x + dx, build_pos.y + dy)
                if not map_info.in_bounds(ray_pos):
                    break
                ray_bit = _bit(ray_pos)
                if (
                    map_info._bm_env[map_info._IDX_ENV_WALL] & ray_bit
                    or map_info._bm_any_building & ray_bit
                ):
                    break
                if target_mask & ray_bit:
                    hits += 1
            if hits >= 2 and rc.can_build_gunner(build_pos, facing):
                plans.append((
                    hits,
                    -_distance_from_core(build_pos),
                    -direction_index,
                    -(build_pos.x + build_pos.y * map_info._width),
                    build_pos,
                    facing,
                ))
    if not plans:
        return False
    _hits, _core_distance, _direction_tie, _tile_tie, build_pos, facing = max(plans)
    if rc.get_global_resources() < rc.get_gunner_cost() + map_info.builder_ti_reserve():
        return True
    rc.build_gunner(build_pos, facing)
    comms.note_gunner_built()
    map_info.update_at(build_pos)
    return True


def _emergency_gunner_defense() -> bool:
    """Pre-ring barrier/gunner response. Return whether it claimed this turn."""
    barrier_targets = _visible_enemy_gunner_barrier_targets()
    if barrier_targets:
        return _build_or_approach_barrier(barrier_targets[0])

    predicted = _predicted_enemy_gunner_tiles()
    if len(predicted) == 1:
        gunner_tile = next(iter(predicted))
        distance = abs(gunner_tile.x - map_info._my_pos.x) + abs(gunner_tile.y - map_info._my_pos.y)
        if distance == 1:
            return _build_or_approach_barrier(gunner_tile)
        barrier_target = _barrier_between_predicted_gunner_and_core(gunner_tile)
        if barrier_target is not None:
            return _build_or_approach_barrier(barrier_target)
        _move_into_build_range(gunner_tile)
        return True
    if len(predicted) > 2:
        return _try_cover_predicted_tiles_with_gunner(predicted)
    return False


def _visible_enemy_gunners() -> set[Position]:
    enemy_idx = 1 - map_info._my_team_idx
    mask = (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[enemy_idx]
        & map_info._bm_visible
    )
    return set(map_info.iter_mask(mask))


def _visible_enemy_core_tiles() -> set[Position]:
    enemy_idx = 1 - map_info._my_team_idx
    mask = (
        map_info._bm_et[map_info._IDX_CORE]
        & map_info._bm_team[enemy_idx]
        & map_info._bm_visible
    )
    if not mask:
        return set()
    # Core entity observations may begin with only one visible tile, but the
    # target is always the complete 2x2 footprint reconstructed by map_info.
    core_area = map_info._bm_their_core_area
    return set(map_info.iter_mask(core_area if core_area else mask))


def _core_threatening_enemy_gunners() -> set[Position]:
    return {pos for pos in _visible_enemy_gunners() if _core_ray(pos) is not None}


def _enemy_gunners_under_current_fire(enemy_gunners: set[Position]) -> set[Position]:
    """Enemy gunners already first in an allied gunner's current firing ray."""
    allied_gunners = (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    covered = set()
    for gunner_pos in map_info.iter_mask(allied_gunners):
        n = gunner_pos.x + gunner_pos.y * map_info._width
        direction_index = map_info._building_dir[n]
        if direction_index < 0:
            continue
        for dx, dy in map_info._GUNNER_RAYS[direction_index]:
            pos = Position(gunner_pos.x + dx, gunner_pos.y + dy)
            if not map_info.in_bounds(pos):
                break
            bit = _bit(pos)
            if map_info._bm_env[map_info._IDX_ENV_WALL] & bit:
                break
            if pos in enemy_gunners:
                covered.add(pos)
                break
            if map_info._bm_any_building & bit:
                break
    return covered


def _gunner_ray_targets(
    build_pos: Position,
    direction_index: int,
    targets: set[Position],
) -> set[Position]:
    """Targets this fixed facing can eventually shoot without a rotation."""
    hits = set()
    for dx, dy in map_info._GUNNER_RAYS[direction_index]:
        pos = Position(build_pos.x + dx, build_pos.y + dy)
        if not map_info.in_bounds(pos):
            break
        bit = _bit(pos)
        if map_info._bm_env[map_info._IDX_ENV_WALL] & bit:
            break
        if pos in targets:
            # Count collinear enemy gunners: after the nearer one dies, the same
            # unrotated gunner can continue firing at the next one.
            hits.add(pos)
            continue
        if map_info._bm_any_building & bit:
            break
    return hits


def _candidate_gunner_sites(targets: set[Position]) -> set[Position]:
    candidates = set()
    for target in targets:
        for ray in map_info._GUNNER_RAYS:
            for dx, dy in ray:
                pos = Position(target.x - dx, target.y - dy)
                if not map_info.in_bounds(pos) or not _empty_unoccupied(pos):
                    continue
                # A defender must eventually be able to stand cardinally beside
                # the placement tile. The current tile is always admissible.
                has_build_stance = False
                for direction in _CARDINALS:
                    stance = map_info.pos_add(pos, direction)
                    if stance == map_info._my_pos or (
                        map_info.in_bounds(stance) and map_info.is_passable(stance)
                    ):
                        has_build_stance = True
                        break
                if has_build_stance:
                    candidates.add(pos)
    return candidates


def _best_pvp_gunner_plan(targets: set[Position], core_target: bool):
    best = None
    for build_pos in _candidate_gunner_sites(targets):
        hits_by_direction = [
            _gunner_ray_targets(build_pos, direction_index, targets)
            for direction_index in range(len(map_info._DIRECTIONS))
        ]
        if core_target:
            immediate_counts = [int(bool(hits)) for hits in hits_by_direction]
            rotation_count = int(any(hits_by_direction))
        else:
            immediate_counts = [len(hits) for hits in hits_by_direction]
            rotation_count = len(set().union(*hits_by_direction))
        immediate_count = max(immediate_counts, default=0)
        if immediate_count == 0:
            continue
        direction_index = min(
            i for i, count in enumerate(immediate_counts) if count == immediate_count
        )
        distance_to_build = max(
            0,
            abs(build_pos.x - map_info._my_pos.x)
            + abs(build_pos.y - map_info._my_pos.y)
            - 1,
        )
        # Exact requested priority: fixed-facing coverage first, all-rotation
        # coverage second. Travel and deterministic order only break ties.
        key = (
            immediate_count,
            rotation_count,
            -distance_to_build,
            -(build_pos.x + build_pos.y * map_info._width),
            -direction_index,
        )
        if best is None or key > best[0]:
            best = (
                key,
                build_pos,
                map_info._DIRECTIONS[direction_index],
            )
    return best


def _execute_pvp_gunner_plan(plan) -> bool:
    if plan is None:
        return False
    _key, build_pos, facing = plan
    distance = abs(build_pos.x - map_info._my_pos.x) + abs(build_pos.y - map_info._my_pos.y)
    if distance == 1:
        if (
            rc.can_build_gunner(build_pos, facing)
            and rc.get_global_resources() >= rc.get_gunner_cost() + map_info.builder_ti_reserve()
        ):
            rc.build_gunner(build_pos, facing)
            comms.note_gunner_built()
            map_info.update_at(build_pos)
        return True
    _move_into_build_range(build_pos)
    return True


def _run_gunner_pvp() -> None:
    """Permanent post-launcher phase: enemy gunners first, core second."""
    global _pvp_focus
    # Only counter-build against a gunner that has a clear core shot in at
    # least one of its eight possible facings. Gunners that cannot threaten the
    # core even after rotating do not justify another defensive gunner.
    enemy_gunners = {
        pos for pos in _visible_enemy_gunners() if _core_ray(pos) is not None
    }
    visible_core = _visible_enemy_core_tiles()

    # A barrier is cheaper and immediately protects the core. If this defender
    # is already cardinally positioned to block an exposed enemy gunner ray,
    # do that before considering another turret.
    for barrier_target in _visible_enemy_gunner_barrier_targets():
        if (
            abs(barrier_target.x - map_info._my_pos.x)
            + abs(barrier_target.y - map_info._my_pos.y)
            == 1
        ):
            if _build_or_approach_barrier(barrier_target):
                return

    # Do not duplicate a gunner already being engaged by the current fixed ray
    # of a friendly gunner. A newly-visible or rotated-away gunner remains a
    # valid PvP target on the following turn.
    enemy_gunners -= _enemy_gunners_under_current_fire(enemy_gunners)

    if enemy_gunners:
        _pvp_focus = min(
            enemy_gunners,
            key=lambda p: (p.distance_squared(map_info._my_pos), p.x, p.y),
        )
        if _execute_pvp_gunner_plan(_best_pvp_gunner_plan(enemy_gunners, False)):
            return

    if visible_core:
        _pvp_focus = min(
            visible_core,
            key=lambda p: (p.distance_squared(map_info._my_pos), p.x, p.y),
        )
        if _execute_pvp_gunner_plan(_best_pvp_gunner_plan(visible_core, True)):
            return

    # Preserve the earlier predictive barrier/gunner response when no direct
    # PvP placement is possible, but never fall back to a launcher.
    if _emergency_gunner_defense():
        return

    enemy_buildings = (
        map_info._bm_any_building
        & map_info._bm_team[1 - map_info._my_team_idx]
        & map_info._bm_visible
    )
    if enemy_buildings:
        import units.atk_states.attack as attack
        if attack.score() > 0:
            attack.run()
            return
    if _pvp_focus is not None:
        nav.move_to({_pvp_focus}, avoid_turret=False)


def _active_defense(lane: int, planned_site: Position | None) -> None:
    """React locally while enemies are visible; launchers perform the removal."""
    enemy_bots = map_info._bm_enemy_bots
    if enemy_bots:
        enemy_pos, _ = nav.closest(enemy_bots)
        if enemy_pos is not None and rc.get_action_cooldown() == 0:
            candidates = []
            for direction in _CARDINALS:
                pos = map_info.pos_add(map_info._my_pos, direction)
                if not map_info.in_bounds(pos):
                    continue
                if intruder_lane(pos) != lane:
                    continue
                if max(abs(pos.x - enemy_pos.x), abs(pos.y - enemy_pos.y)) > 1:
                    continue
                if map_info._bm_env[map_info._IDX_ENV_ORE_TI] & _bit(pos):
                    continue
                if rc.can_build_launcher(pos):
                    candidates.append(pos)
            if candidates and rc.get_global_resources() >= rc.get_launcher_cost() + map_info.builder_ti_reserve():
                core = map_info._my_core
                target = min(candidates, key=lambda p: p.distance_squared(core))
                launcher_id = rc.build(EntityType.LAUNCHER, target)
                _publish_built_launcher(lane, target, launcher_id)
                return
        if enemy_pos is not None:
            # Do not let a visible rusher lure both defenders away from the
            # core. If no immediately useful launcher can be placed beside the
            # enemy, continue closing on the planned defensive launcher site.
            _move_into_build_range(planned_site if planned_site is not None else enemy_pos)
            return

    enemy_buildings = (
        map_info._bm_any_building
        & map_info._bm_team[1 - map_info._my_team_idx]
        & map_info._bm_visible
    )
    if planned_site is not None:
        # Visible enemy structures must not lure a ring builder away before the
        # defensive coverage is finished.
        _move_into_build_range(planned_site)
        return
    if enemy_buildings:
        # Reuse Loki's proven structure-attack logic without giving the defender
        # access to harvest/route/economy states.
        import units.atk_states.attack as attack
        if attack.score() > 0:
            attack.run()
        else:
            target, _ = nav.closest(enemy_buildings)
            if target is not None:
                nav.move_to(target, avoid_turret=False)


def _hold_first_launcher() -> None:
    """Return to, then remain beside, this defender's first launcher."""
    if _first_launcher_site is None:
        return
    current = map_info._my_pos
    if max(
        abs(current.x - _first_launcher_site.x),
        abs(current.y - _first_launcher_site.y),
    ) <= 1:
        return
    destinations = set()
    for direction in map_info._DIRECTIONS:
        pos = map_info.pos_add(_first_launcher_site, direction)
        if not map_info.in_bounds(pos) or not map_info.is_passable(pos):
            continue
        if rc.is_in_vision(pos) and rc.get_tile_builder_bot_id(pos):
            continue
        destinations.add(pos)
    if destinations:
        nav.move_to(destinations, avoid_turret=False)


def _pure_mirror_enemy(enemy: Position | None, reported: Position | None) -> None:
    """Copy one observed cardinal delta exactly; never catch up autonomously."""
    global _mirror_enemy_position
    if enemy is None:
        return

    if _mirror_enemy_position is None:
        # The mailbox normally contains the launch-turn position while local
        # vision contains the following position, allowing the first movement
        # to be mirrored immediately after landing.
        _mirror_enemy_position = reported if reported is not None else enemy

    previous = _mirror_enemy_position
    dx = enemy.x - previous.x
    dy = enemy.y - previous.y
    _mirror_enemy_position = enemy
    if abs(dx) + abs(dy) != 1:
        # No movement (or a target identity jump): never reposition or catch up.
        return
    direction = map_info.direction_to(previous, enemy)
    if rc.can_move(direction):
        rc.move(direction)


def _mirror_lane_intruder(lane: int) -> bool:
    """Purely copy this lane's claimed intruder, or stay still."""
    if not comms.defender_intercepting(lane):
        return False
    reported = comms.lane_intruder(lane, rc.get_current_round())
    claimed_enemy = comms.claimed_enemy_id(lane)
    enemy = None
    for entity_id in rc.get_nearby_units():
        if entity_id == claimed_enemy:
            enemy = rc.get_position(entity_id)
            break
    if enemy is None:
        enemy = reported
    _pure_mirror_enemy(enemy, reported)
    return True


def run_reinforcement(enemy_id: int, reported: Position, launched: bool) -> None:
    """Wait beside a launcher until thrown, then purely mirror this claim."""
    if not launched:
        return
    enemy = None
    for entity_id in rc.get_nearby_units():
        if entity_id == enemy_id:
            enemy = rc.get_position(entity_id)
            break
    if enemy is None:
        enemy = reported
    _pure_mirror_enemy(enemy, reported)


def run(lane: int) -> bool:
    """Run one permanent defense half. Defenders never become economy."""
    global _patrol_mode, _gunner_pvp_mode
    threatened = enemy_visible()
    if comms.defender_claim_pending(lane):
        # A claimed defender must remain launchable; do not continue building,
        # pathing, or PvP while a launcher is trying to throw it.
        return False
    if comms.defender_intercepting(lane):
        _mirror_lane_intruder(lane)
        return False
    if not threatened and _wait_for_new_launcher():
        return False

    plan = _calculate_tiling()
    enemy_bots = list(map_info.iter_mask(map_info._bm_enemy_bots))
    enemy_inside_defense_radius = any(
        _distance_from_core(enemy_pos) <= 16 for enemy_pos in enemy_bots
    )
    # Seeing a distant unit must never cancel the launcher opening. The
    # zero-launcher fast path uses the same core-local threat radius as the
    # ordinary unfinished-ring transition.
    emergency_trigger = enemy_inside_defense_radius and (
        _launchers_placed == 0 or bool(plan)
    )
    phase_trigger = (
        emergency_trigger
        or bool(_core_threatening_enemy_gunners())
        or bool(_visible_enemy_core_tiles())
    )
    if phase_trigger:
        _gunner_pvp_mode = True
        comms.mark_gunner_pvp()
    elif comms.gunner_pvp():
        _gunner_pvp_mode = True

    if _gunner_pvp_mode:
        # Re-broadcast every turn so a simultaneous ring-complete store write
        # cannot make the other defender miss this permanent phase transition.
        comms.mark_gunner_pvp()
        _run_gunner_pvp()
        return False
    if not plan:
        _target, uncovered = _uncovered()
        if uncovered and _blocked_site_until:
            return False
        comms.mark_ring_complete()
        _patrol_mode = True
        if _first_launcher_id:
            comms.publish_defender_home(
                lane, rc.get_id(), _first_launcher_id
            )
        if not _mirror_lane_intruder(lane):
            _hold_first_launcher()
        return False

    target = next_launcher_site(lane)
    if target is None:
        # Our half is done while the partner is still working. Hold at our
        # first launcher and never wrap into or opportunistically build on the
        # other defender's half.
        _hold_first_launcher()
        return False
    if threatened:
        # A rusher becoming visible must not cancel a launcher that is already
        # in build range. That was the main reason early defenders appeared to
        # chase enemies without ever constructing the wall.
        if target is not None and _build_and_publish(lane, target):
            return False
        _active_defense(lane, target)
        return False
    dx = abs(map_info._my_pos.x - target.x)
    dy = abs(map_info._my_pos.y - target.y)
    if dx + dy != 1:
        if not _move_into_build_range(target):
            _temporarily_skip_stuck_site(target)
    _build_and_publish(lane, target)
    return False
