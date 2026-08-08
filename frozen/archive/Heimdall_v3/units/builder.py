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

from log import DRAW_DEBUG, status


rc: Controller
nav: Pathing = None

# Sorted in descending order of max score to allow early break in selection loop
# (Loki: secure state removed — global ammo means turrets don't need protected ore.)
states = tuple(sorted(
    [explore, disrupt, harvest, route, heal, attack],
    key=lambda s: s.MAX_SCORE,
    reverse=True
))

# Attack builders only ever attack or explore (their explore target is pinned to
# a symmetry-predicted enemy core) — no harvesting, routing, healing, disrupting.
_ATK_STATES = tuple(sorted(
    [explore, attack],
    key=lambda s: s.MAX_SCORE,
    reverse=True
))

# Economy builders harvest ore and lay conveyor routes. explore is included only
# as a last-resort fallback (MAX_SCORE 1, below harvest/route) so an econ bot with
# no reachable ore to claim walks off to find some instead of idling forever —
# harvest takes back over the moment ore is claimable.
_ECONOMY_STATES = tuple(sorted(
    [harvest, route, explore],
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

# Opening builders 3 and 4 are the two ATTACK builders. Each is pinned to a
# different symmetry-predicted enemy core (atk 0 -> horizontal-first, atk 1 ->
# vertical-first), so they cross the map early along different guesses —
# scouting, confirming symmetry, and letting attack take over near enemy
# structures. _atk_index selects the symmetry-fallback order.
_atk_bot = False
_atk_index: int | None = None
_economy_builder = False
_defense_lane: int | None = None
_opening_role_checked = False


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
        if not _atk_bot and _defense_lane is None and current_round <= INITIAL_SPAWN_COUNT + 1 and map_info._my_core is not None:
            # Choose explore direction based on where we are relative to core
            spawn_dir = map_info.direction_to(map_info._my_core, map_info._my_pos)
            _initial_explore_target = get_ray_endpoint(map_info._my_pos, spawn_dir, map_info._width, map_info._height, max_steps=INITIAL_EXPLORE_MAX_STEPS)
            _initial_explore_round = current_round
        
        _initial_explore_calculated = True

    # Auto-clear stale initial target if we couldn't reach it in time
    if _initial_explore_target is not None and current_round - _initial_explore_round >= INITIAL_EXPLORE_TIMEOUT:
        _initial_explore_target = None


_chosen_state = None   # last state select_best_state() picked, for status logging


def select_best_state():
    global _chosen_state
    best_state = None
    best_score = 0

    available_states = (
        _ATK_STATES if _atk_bot
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

    _chosen_state = best_state
    return best_state


def atk_symmetry_target():
    """This attack bot's symmetry-predicted enemy core (see
    map_info.atk_symmetry_target)."""
    return map_info.atk_symmetry_target(_atk_index)


def _tile_open(x, y) -> bool:
    if not (0 <= x < map_info._width and 0 <= y < map_info._height):
        return False
    bit = 1 << (x + y * map_info._width)
    return not (map_info._bm_env[map_info._IDX_ENV_WALL] & bit) and not (map_info._bm_any_building & bit)


def _nearest_open(cells):
    mp = map_info._my_pos
    best = None
    best_d = None
    for x, y in cells:
        if not _tile_open(x, y):
            continue
        d = (x - mp.x) ** 2 + (y - mp.y) ** 2
        if best_d is None or d < best_d:
            best_d = d
            best = Position(x, y)
    return best


def atk_target():
    """This attack bot's movement target: the nearest open tile adjacent to the
    gunner it most recently placed (while that gunner is still alive), else the
    nearest open tile adjacent to the enemy core."""
    last = attack.last_gunner_pos
    if last is not None:
        my_team = map_info._bm_team[map_info._my_team_idx]
        if map_info._bm_et[map_info._IDX_GUNNER] & my_team & (1 << (last.x + last.y * map_info._width)):
            adj = _nearest_open(
                (last.x + dx, last.y + dy)
                for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy
            )
            if adj is not None:
                return adj
    core = map_info.atk_symmetry_target(_atk_index)
    if core is None:
        return None
    ring = _nearest_open(
        (x, y)
        for x in range(core.x - 1, core.x + 3)
        for y in range(core.y - 1, core.y + 3)
        if not (core.x <= x <= core.x + 1 and core.y <= y <= core.y + 1)
    )
    return ring if ring is not None else core


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
    global _atk_bot, _atk_index, _economy_builder, _defense_lane, _opening_role_checked
    # Same-round buffered writes can make a non-reinforcement briefly infer lane
    # 1. Correct that provisional role as soon as the committed owner differs.
    if (
        _defense_lane == 1
        and comms.defender_id(1) not in (0, rc.get_id())
    ):
        _defense_lane = None

    if not _opening_role_checked or (
        _defense_lane is None and not _atk_bot and not _economy_builder
    ):
        assigned_lane = comms.defender_lane(rc.get_id())
        if assigned_lane is not None:
            _defense_lane = assigned_lane
        idx = comms.atk_index(rc.get_id())
        if idx is not None:
            _atk_bot = True
            _atk_index = idx
        _economy_builder = _economy_builder or comms.is_economy(rc.get_id())
        # A reinforcement is spawned and launched later in the core's turn.
        # Store writes are buffered, so on that birth turn its id can still be
        # zero in the request. It is the only unassigned specialist at that
        # point and may safely adopt the requested lane immediately.
        if _defense_lane is None and not _atk_bot and not _economy_builder:
            request = comms.reinforcement_claim()
            if request is not None:
                enemy_id, defender_id, launcher, lane = request
                near_launcher = max(
                    abs(map_info._my_pos.x - launcher.x),
                    abs(map_info._my_pos.y - launcher.y),
                ) <= 1
                near_enemy = False
                for entity_id in rc.get_nearby_units():
                    if entity_id != enemy_id:
                        continue
                    enemy_pos = rc.get_position(entity_id)
                    near_enemy = max(
                        abs(map_info._my_pos.x - enemy_pos.x),
                        abs(map_info._my_pos.y - enemy_pos.y),
                    ) <= 1
                    break
                if defender_id in (0, rc.get_id()) and (near_launcher or near_enemy):
                    _defense_lane = lane
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
        and not _atk_bot
        and not _economy_builder
    ):
        return

    _update_initial_explore(current_round)

    # Dispatch: economy > defense lane > attack/generalist.
    if _economy_builder:
        econ_builder.run()
    elif _defense_lane is not None:
        def_builder.run()
    else:
        atk_builder.run()

    _log_status()


def _role_name():
    if _economy_builder:
        return "econ"
    if _defense_lane is not None:
        return "def%d" % _defense_lane
    if _atk_bot:
        return "atk%d" % (_atk_index if _atk_index is not None else -1)
    return "gen"


def _log_status():
    role = _role_name()
    if _defense_lane is not None and not _economy_builder:
        state, target = "defense", getattr(defense, "target", None)
    elif _atk_bot and atk_builder.action is not None:
        state, target = atk_builder.action, atk_symmetry_target()
    elif _chosen_state is not None:
        state = _chosen_state.__name__.rsplit(".", 1)[-1]
        target = getattr(_chosen_state, "target", None)
    else:
        state, target = "idle", None
    status("id=%d %s role=%s state=%s target=%s" % (
        rc.get_id(), map_info._my_pos, role, state, target))
