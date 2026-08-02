from fcode import Controller, Position

import random

import map_info
from pathing import Pathing
import comms
from units.spawn_plan import get_ray_endpoint, INITIAL_EXPLORE_MAX_STEPS, INITIAL_SPAWN_COUNT

import units.atk_states.explore  as explore
import units.atk_states.disrupt  as disrupt
import units.econ_states.harvest as harvest
import units.econ_states.route   as route
import units.atk_states.heal     as heal
import units.atk_states.attack   as attack
import units.def_states.defense  as defense

# Builder-type behaviour modules. The cycle (they each import this module for
# shared state) is safe: nothing here touches their attributes at import time.
import units.atk_builder as atk_builder
import units.def_builder as def_builder
import units.econ_builder as econ_builder

from log import DRAW_DEBUG


rc: Controller
nav: Pathing = None

# Sorted in descending order of max score to allow early break in selection loop
# (Loki: secure state removed — global ammo means turrets don't need protected ore.)
states = tuple(sorted(
    [explore, disrupt, harvest, route, heal, attack],
    key=lambda s: s.MAX_SCORE,
    reverse=True
))

# The rush builder only ever attacks or explores (its explore target is pinned
# to the enemy core) — no harvesting, routing, healing, or disrupting.
_RUSH_STATES = tuple(sorted(
    [explore, attack],
    key=lambda s: s.MAX_SCORE,
    reverse=True
))

# Economy builders are strict specialists. If neither harvesting nor an
# unfinished route is available, they wait rather than exploring or helping
# with combat/defense work.
_ECONOMY_STATES = tuple(sorted(
    [harvest, route],
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

# Heimdall's third opening builder is the RUSH builder. Its explore target is
# pinned to the (predicted) enemy core, so it crosses the map early — scouting,
# confirming symmetry, and letting attack take over near enemy structures.
_rush_builder = False
_economy_builder = False
_defense_lane: int | None = None
_opening_role_checked = False
_reinforcement_enemy_id = 0
_reinforcement_position: Position | None = None
_reinforcement_launched = False


def init(c: Controller):
    global rc, harvest_radius, nav
    rc = c
    nav = Pathing(c)
    defense.init(c, nav)
    harvest_radius = (c.get_map_width() + c.get_map_height()) // 3
    for s in states:
        s.init(c)


def draw_mask(mask, r, g, b):
    if not DRAW_DEBUG:
        return
    for p in map_info.iter_mask(mask):
        rc.draw_indicator_dot(p, r, g, b)



def handle_comms():
    # Loki: comms is the 16-slot store holding the whole board (see comms.py).
    comms.update()

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
            zone = map_info.expand_manhattan(zone)  # movement reach
        _harvest_zone = zone


def _update_initial_explore(current_round: int):
    global _initial_explore_target, _initial_explore_calculated, _initial_explore_round

    if not _initial_explore_calculated:
        # Only first few builders follow initial explore plan (not the rusher —
        # its explore target is pinned to the enemy core instead)
        if not _rush_builder and _defense_lane is None and current_round <= INITIAL_SPAWN_COUNT + 1 and map_info._my_core is not None:
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

    available_states = (
        _RUSH_STATES if _rush_builder
        else _ECONOMY_STATES if _economy_builder
        else states
    )
    for state in available_states:
        # Since states are sorted, break early if we can't beat best score
        if best_score >= state.MAX_SCORE:
            break

        score = state.score()
        if score > best_score:
            best_score = score
            best_state = state

    return best_state


def heal_fallback():
    """Heal the best adjacent damaged ally, then self. Shared by the attack and
    defense builders; economy and reinforcement builders skip healing."""
    heal._do_best_heal()
    if rc.can_heal(map_info._my_pos):
        rc.heal(map_info._my_pos)


def _resolve_opening_role():
    """Fold this builder's comms-assigned opening role into the role flags.
    Store writes are buffered, so an unrecognized role is retried each round
    rather than permanently defaulting the builder to a generalist."""
    global _rush_builder, _economy_builder, _defense_lane, _opening_role_checked
    global _reinforcement_enemy_id, _reinforcement_position, _reinforcement_launched

    reinforcement = comms.reinforcement_for_builder(rc.get_id())
    if reinforcement is not None:
        (_reinforcement_enemy_id, _reinforcement_position,
         _reinforcement_launched) = reinforcement

    if not _opening_role_checked or (
        _defense_lane is None and not _rush_builder and not _economy_builder
    ):
        assigned_lane = comms.defender_lane(rc.get_id())
        if assigned_lane is not None:
            _defense_lane = assigned_lane
        _rush_builder = _rush_builder or comms.is_rusher(rc.get_id())
        _economy_builder = _economy_builder or comms.is_economy(rc.get_id())
        _opening_role_checked = True


def run():
    # Sync round info + shared state, resolve role, then dispatch to the
    # matching builder-type module (each owns its own rebuild/heal policy).
    current_round = rc.get_current_round()
    map_info.update(recompute=False)
    handle_comms()
    map_info.recompute_derived()

    _resolve_opening_role()
    _update_harvest_zone()

    # An opening assignment may be one buffered round late; wait rather than run
    # a would-be specialist as a generalist for a turn.
    if (
        current_round <= INITIAL_SPAWN_COUNT + 1
        and _defense_lane is None
        and not _rush_builder
        and not _economy_builder
    ):
        return

    _update_initial_explore(current_round)

    # Dispatch (order matches the original precedence):
    #   reinforcement > economy > defense lane > attack/generalist.
    if _reinforcement_enemy_id:
        def_builder.run_reinforcement()
    elif _economy_builder:
        econ_builder.run()
    elif _defense_lane is not None:
        def_builder.run()
    else:
        atk_builder.run()
