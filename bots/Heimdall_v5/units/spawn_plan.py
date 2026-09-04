import random
from itertools import combinations

from fcode import Controller, Direction, Environment, Position

import map_info
from log import DRAW_DEBUG

# Opening composition lives in _config (single source of truth); re-exported here
# so existing `from units.spawn_plan import INITIAL_SPAWN_COUNT` callers still work.
from _config import NUM_ATTACK, NUM_ECON, INITIAL_SPAWN_COUNT

# Chebyshev-step cap for a builder bot's initial ray-follow exploration.
# The core draws lines capped to +1 to account for the bot starting one step out from the core.
INITIAL_EXPLORE_MAX_STEPS = 12

DIRECTIONS = map_info._DIRECTIONS

DIAGONAL = {
    Direction.NORTHEAST,
    Direction.SOUTHEAST,
    Direction.SOUTHWEST,
    Direction.NORTHWEST,
}


def dir_distance(a: Direction, b: Direction) -> int:
    ia = DIRECTIONS.index(a)
    ib = DIRECTIONS.index(b)
    diff = abs(ia - ib)
    return min(diff, 8 - diff)


def get_ray_endpoint(start: Position, direction: Direction, width: int, height: int, max_steps: int | None = None) -> Position:
    dx, dy = map_info._DIRECTION_DELTAS[direction]
    x, y = start.x, start.y
    steps = 0
    while True:
        if max_steps is not None and steps >= max_steps:
            return Position(x, y)
        nx, ny = x + dx, y + dy
        if nx < 0 or nx >= width or ny < 0 or ny >= height:
            return Position(x, y)
        x, y = nx, ny
        steps += 1


def _all_dir_endpoints(core_pos: Position, width: int, height: int):
    return [(d, get_ray_endpoint(core_pos, d, width, height)) for d in DIRECTIONS]


def _build_ti_near_mask(rc: Controller) -> int:
    """Bitmap of tiles within Chebyshev 1 (dist² ≤ 2) of a visible titanium ore."""
    w = map_info._width
    ti_mask = 0
    for p in rc.get_nearby_tiles():
        if rc.get_tile_env(p) == Environment.ORE_TITANIUM:
            ti_mask |= 1 << (p.x + p.y * w)
    if not ti_mask:
        return 0
    return map_info.expand_manhattan(ti_mask)


def _ray_hits_mask(core_pos: Position, direction: Direction, width: int, height: int, mask: int) -> bool:
    if not mask:
        return False
    dx, dy = map_info._DIRECTION_DELTAS[direction]
    w = width
    x, y = core_pos.x + dx, core_pos.y + dy
    while 0 <= x < width and 0 <= y < height:
        if mask & (1 << (x + y * w)):
            return True
        x += dx
        y += dy
    return False


def get_valid_directions(rc: Controller, core_pos: Position, width: int, height: int):
    ti_near = _build_ti_near_mask(rc)
    valid = []
    for d, endpoint in _all_dir_endpoints(core_pos, width, height):
        if not rc.is_in_vision(endpoint):
            valid.append((d, endpoint))
        elif _ray_hits_mask(core_pos, d, width, height, ti_near):
            valid.append((d, endpoint))
    return valid


def pick_n_directions(pool, n: int):
    if len(pool) <= n:
        return list(pool)

    best = tuple(range(n))
    best_score = -1
    best_diagonal_count = sum(1 for k in best if pool[k][0] in DIAGONAL)

    for combo in combinations(range(len(pool)), n):
        # Maximize angular distance between chosen directions
        score = 1
        for i in range(n):
            for j in range(i + 1, n):
                score *= dir_distance(pool[combo[i]][0], pool[combo[j]][0])

        # Tiebreak by preferring diagonals
        diagonal_count = sum(1 for k in combo if pool[k][0] in DIAGONAL)
        if score > best_score or (score == best_score and diagonal_count > best_diagonal_count):
            best_score = score
            best_diagonal_count = diagonal_count
            best = combo

    return [pool[k] for k in best]


def draw_spawn_plan(rc: Controller, core_pos: Position, spawn_plan, width: int, height: int) -> None:
    if not DRAW_DEBUG:
        return
    for d in spawn_plan:
        endpoint = get_ray_endpoint(core_pos, d, width, height, max_steps=INITIAL_EXPLORE_MAX_STEPS + 1)
        rc.draw_indicator_line(core_pos, endpoint, 0, 255, 0)


def choose_spawn_plan(rc: Controller, core_pos: Position, n: int):
    width = rc.get_map_width()
    height = rc.get_map_height()

    # Filter directions first.  The validity heuristic can legitimately return
    # fewer than four rays on small, edge, and heavily-obstructed maps.  It is
    # only a preference: the opening still needs exactly ``n`` spawn roles.
    valid = get_valid_directions(rc, core_pos, width, height)
    chosen = pick_n_directions(valid, min(n, len(valid)))
    if len(chosen) < n:
        chosen_directions = {direction for direction, _endpoint in chosen}
        remaining = [
            item for item in _all_dir_endpoints(core_pos, width, height)
            if item[0] not in chosen_directions
        ]
        while remaining and len(chosen) < n:
            # Preserve every valid ray, then fill the opening with the direction
            # that gives the best angular spread. Prefer diagonals on ties.
            best = max(
                remaining,
                key=lambda item: (
                    min(
                        (dir_distance(item[0], old[0]) for old in chosen),
                        default=8,
                    ),
                    item[0] in DIAGONAL,
                    -DIRECTIONS.index(item[0]),
                ),
            )
            chosen.append(best)
            remaining.remove(best)

    # Spawn in order of closeness to center
    center = Position(width // 2, height // 2)
    center_dir = map_info.direction_to(core_pos, center)
    chosen.sort(key=lambda de: (dir_distance(de[0], center_dir), de[1].distance_squared(center)))

    return [d for (d, _) in chosen]
