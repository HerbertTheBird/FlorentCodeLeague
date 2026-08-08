from main import has_op
from fcode import Controller, Position

import random

import map_info
import pathing
from pathing import Pathing
import comms
from units.spawn_plan import get_ray_endpoint, INITIAL_EXPLORE_MAX_STEPS, INITIAL_SPAWN_COUNT

import units.states.explore  as explore
import units.states.disrupt  as disrupt
import units.states.harvest  as harvest
import units.states.route    as route
import units.states.heal     as heal
import units.states.attack   as attack

from log import DRAW_DEBUG


rc: Controller
nav: Pathing = None

# Sorted in descending order of max score to allow early break in selection loop
states = tuple(sorted(
    [explore, disrupt, harvest, route, heal, attack],
    key=lambda s: s.MAX_SCORE,
    reverse=True
))

# Harvvest zones are calculated based on map symmetry with fallback
harvest_radius = 0
_harvest_zone = 0
_harvest_zone_final = False

# Initial explore target for first few builders
INITIAL_EXPLORE_TIMEOUT = 30
_initial_explore_calculated = False
_initial_explore_target: Position | None = None
_initial_explore_round = -1

# Builders spawned on round 1 stay close to the core; their state targets and
# explore tiles are restricted to within STAY_NEAR_CORE_DSQ of the core.
STAY_NEAR_CORE_DSQ = 100
_stay_near_core = False
_first_run_done = False
_near_core_mask_cache: tuple[Position | None, int] = (None, 0)


def near_core_mask() -> int:
    """Bitmask of in-bounds tiles within STAY_NEAR_CORE_DSQ of my core."""
    global _near_core_mask_cache
    core = map_info._my_core
    if core is None:
        return map_info._board_mask
    if _near_core_mask_cache[0] == core:
        return _near_core_mask_cache[1]
    w = map_info._width
    h = map_info._height
    cx, cy = core.x, core.y
    result = 0
    for y in range(h):
        dy2 = (y - cy) * (y - cy)
        if dy2 > STAY_NEAR_CORE_DSQ:
            continue
        for x in range(w):
            dx = x - cx
            if dx * dx + dy2 <= STAY_NEAR_CORE_DSQ:
                result |= 1 << (x + y * w)
    _near_core_mask_cache = (core, result)
    return result


def init(c: Controller):
    global rc, harvest_radius, nav
    rc = c
    nav = Pathing(c)
    harvest_radius = (c.get_map_width() + c.get_map_height()) // 3
    for s in states:
        s.init(c)


def draw_mask(mask, r, g, b):
    if not DRAW_DEBUG:
        return
    for p in map_info.iter_mask(mask):
        rc.draw_indicator_dot(p, r, g, b)


def _compute_voronoi_harvest_zone():
    """Flood-fill Manhattan from both cores simultaneously.
    Tiles reached by my core first are my harvest zone."""
    w = map_info._width
    h = map_info._height
    board = (1 << (w * h)) - 1
    walls = map_info._bm_env[map_info._IDX_ENV_WALL]
    passable = board & ~walls

    my_core = map_info._my_core
    enemy_core = map_info._predicted_enemy_core

    my_front = 1 << (my_core.x + my_core.y * w)
    enemy_front = 1 << (enemy_core.x + enemy_core.y * w)

    my_claimed = my_front
    enemy_claimed = enemy_front
    claimed = my_claimed | enemy_claimed

    while my_front or enemy_front:
        if my_front:
            my_expand = map_info.expand_manhattan(my_front) & passable & ~claimed
            my_claimed |= my_expand
            claimed |= my_expand
            my_front = my_expand
        if enemy_front:
            enemy_expand = map_info.expand_manhattan(enemy_front) & passable & ~claimed
            enemy_claimed |= enemy_expand
            claimed |= enemy_expand
            enemy_front = enemy_expand

    return my_claimed


def _update_harvest_zone():
    global _harvest_zone, _harvest_zone_final

    my_core = map_info._my_core
    if not my_core or _harvest_zone_final:
        return

    if map_info._solved_sym and map_info._predicted_enemy_core is not None:
        # Symmetry solved - compute Voronoi partition once
        _harvest_zone = _compute_voronoi_harvest_zone()
        _harvest_zone_final = True
        return

    if not _harvest_zone:
        # Fallback: radius-based until symmetry is solved
        w = map_info._width
        zone = 1 << (my_core.x + my_core.y * w)
        for _ in range(harvest_radius):
            zone = map_info.expand_chebyshev(zone)
        _harvest_zone = zone


def _update_initial_explore(current_round: int):
    global _initial_explore_target, _initial_explore_calculated, _initial_explore_round

    if not _initial_explore_calculated:
        # Only first few builders follow initial explore plan
        if current_round <= INITIAL_SPAWN_COUNT + 1 and map_info._my_core is not None:
            # Choose explore direction based on where we are relative to core
            spawn_dir = map_info.direction_to(map_info._my_core, map_info._my_pos)
            _initial_explore_target = get_ray_endpoint(map_info._my_pos, spawn_dir, map_info._width, map_info._height, max_steps=INITIAL_EXPLORE_MAX_STEPS)
            _initial_explore_round = current_round
        
        _initial_explore_calculated = True

    # Auto-clear stale initial target if we couldn't reach it in time
    if _initial_explore_target is not None and current_round - _initial_explore_round >= INITIAL_EXPLORE_TIMEOUT:
        _initial_explore_target = None


def select_best_state():
    best_state = None
    best_score = 0

    for state in states:
        # Since states are sorted, break early if we can't beat best score
        if best_score >= state.MAX_SCORE:
            break

        score = state.score()
        if score > best_score:
            best_score = score
            best_state = state

    return best_state


def run():
    global _stay_near_core, _first_run_done

    # Sync round info
    current_round = rc.get_current_round()
    if not _first_run_done:
        _first_run_done = True
        if current_round == 1:
            _stay_near_core = True
    map_info.update(recompute=False)
    comms.update()          # absorb every slot's shared tiles/symmetry, broadcast our own
    map_info.recompute_derived()
    _update_harvest_zone()

    # First few builder bots derive explore target from spawn position
    _update_initial_explore(current_round)

    # Run state-specific logic
    best_state = select_best_state()
    best_state.run()



    # End-of-turn fallback: if the selected state used neither movement nor an
    # action, repair a damaged adjacent core before considering other buildings.
    if has_op() and not heal._try_heal_core():
        heal._do_best_heal()
