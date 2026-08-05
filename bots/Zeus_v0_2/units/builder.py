from fcode import Controller, Direction, Position

import random

import map_info
from pathing import Pathing
import comms
from units.spawn_plan import get_ray_endpoint, INITIAL_EXPLORE_MAX_STEPS, INITIAL_SPAWN_COUNT

import units.atk_states.explore  as explore
import units.econ_states.harvest as harvest
import units.econ_states.route   as route
import units.atk_states.heal     as heal
import units.atk_states.attack   as attack
import units.atk_states.block    as block
import units.econ_states.park    as park
import units.econ_states.core_block as core_block
import units.econ_states.counter_mirror as counter_mirror
import units.econ_states.killbox as killbox
import units.econ_states.trap_launcher as trap_launcher
import units.killbox_plan as killbox_plan

# Builder-type behaviour modules. The cycle (they each import this module for
# shared state) is safe: nothing here touches their attributes at import time.
import units.atk_builder as atk_builder
import units.econ_builder as econ_builder
import units.launch_plan as launch_plan

from log import DRAW_DEBUG, status


rc: Controller
nav: Pathing = None

# Sorted in descending order of max score to allow early break in selection loop
# (Loki: secure state removed — global ammo means turrets don't need protected ore.)
states = tuple(sorted(
    [explore, harvest, route, heal, attack],
    key=lambda s: s.MAX_SCORE,
    reverse=True
))

# Attack builders block enemy-gunner shots on our gunners first, then attack or
# explore (their explore target is pinned to a symmetry-predicted enemy core) —
# no harvesting, routing, healing, disrupting.
_ATK_STATES = tuple(sorted(
    [explore, attack, block, counter_mirror],
    key=lambda s: s.MAX_SCORE,
    reverse=True
))

# Economy builders finish routes/harvesting first (so a route is never abandoned
# half-built), then — once economy is delivering to the core — park near the core
# as their idle behaviour instead of wandering (park outranks explore). They also
# run a DEFENSIVE variant of attack (same placement scoring, but never targeting
# the enemy core — see attack._defensive) so they can drop gunners on threats in
# our territory.
from _config import KILLBOX_ENABLED
_econ_states = [harvest, route, explore, attack, park, core_block, counter_mirror]
_done_states = [attack, park, core_block, counter_mirror]
if KILLBOX_ENABLED:
    _econ_states += [killbox, trap_launcher]
    _done_states += [killbox, trap_launcher]

_ECONOMY_STATES = tuple(sorted(
    _econ_states,
    key=lambda s: s.MAX_SCORE,
    reverse=True
))

# After an econ bot has finished a route (detected the moment its supply line to
# the core is complete — see my_route_complete()), it is locked out of route and
# harvest for the rest of the game: it only defends (attack) or parks near the core.
_DONE_ECON_STATES = tuple(sorted(
    _done_states,
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
_economy_index: int | None = None
_opening_role_checked = False

# Latched True once this econ bot's supply line to the core is complete (see
# my_route_complete()). From then on it can never re-enter route or harvest — it
# only defends or parks (see _DONE_ECON_STATES).
_route_done = False


def init(c: Controller):
    global rc, harvest_radius, nav
    rc = c
    nav = Pathing(c)
    harvest_radius = (c.get_map_width() + c.get_map_height()) // 3
    for s in states:
        s.init(c)
    block.init(c)   # attack-only state, not in `states`; init explicitly
    park.init(c)    # econ-only idle state, not in `states`; init explicitly
    core_block.init(c)
    counter_mirror.init(c)
    killbox.init(c)
    trap_launcher.init(c)


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
        if not _atk_bot and current_round <= INITIAL_SPAWN_COUNT + 1 and map_info._my_core is not None:
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
    global _chosen_state, _route_done
    best_state = None
    best_score = 0

    # The moment an econ bot has a COMPLETE supply line to the core (every one of
    # its conveyors reaches the core, nothing dangling), it is finished routing —
    # lock it out of route and harvest for good so it never grabs another route or
    # harvest, and only defends or parks. Checked BEFORE picking the state so the
    # lock takes effect on the very turn the path completes.
    if _economy_builder and not _route_done and map_info.my_route_complete():
        _route_done = True

    available_states = (
        _ATK_STATES if _atk_bot
        else _DONE_ECON_STATES if (_economy_builder and _route_done)
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
    # The first time an econ bot parks, its route is finished — lock it out of
    # route and harvest permanently.
    if _economy_builder and best_state is park:
        _route_done = True
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
        if (1 << (x + y * map_info._width)) & map_info._bm_my_gunner_claims:
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
        for x in range(core.x - 2, core.x + 4)
        for y in range(core.y - 2, core.y + 4)
        if not (core.x - 1 <= x <= core.x + 2 and core.y - 1 <= y <= core.y + 2)
    )
    return ring if ring is not None else core


def heal_fallback():
    """Heal the best adjacent damaged ally, then self. Used by attack builders;
    economy builders skip healing."""
    heal._do_best_heal()
    if rc.can_heal(map_info._my_pos):
        rc.heal(map_info._my_pos)


def _resolve_opening_role():
    """Fold this builder's comms-assigned opening role (attack or economy) into
    the role flags. Store writes are buffered, so an unrecognized role is retried
    each round rather than permanently defaulting the builder to a generalist."""
    global _atk_bot, _atk_index, _economy_builder, _economy_index, _opening_role_checked

    if not _opening_role_checked or (not _atk_bot and not _economy_builder):
        idx = comms.atk_index(rc.get_id())
        if idx is not None:
            _atk_bot = True
            _atk_index = idx
        _economy_builder = _economy_builder or comms.is_economy(rc.get_id())
        if _economy_builder:
            _economy_index = comms.economy_index(rc.get_id())
        _opening_role_checked = True


def run():
    # Sync round info + shared state, resolve role, then dispatch to the
    # matching builder-type module (each owns its own rebuild/heal policy).
    current_round = rc.get_current_round()
    map_info.update(recompute=False)
    handle_comms()
    map_info.recompute_derived()
    # Keep the killbox (and its adjacent tiles) off-limits to harvesting/routing.
    map_info._bm_killbox_clear = killbox_plan.keep_clear_mask() if killbox_plan.active() else 0

    _resolve_opening_role()
    _update_harvest_zone()

    # An opening assignment may be one buffered round late; wait rather than run
    # a would-be specialist as a generalist for a turn.
    if (
        current_round <= INITIAL_SPAWN_COUNT + 1
        and not _atk_bot
        and not _economy_builder
    ):
        return

    _update_initial_explore(current_round)

    # No builder is allowed to remain in one of our gunners' live firing lanes.
    # Pathfinding already avoids these tiles, but this explicit evacuation also
    # handles launch landings and states whose selected target is the current
    # tile. Do it before any build/park action can consume the turn.
    my_bit = 1 << (map_info._my_pos.x + map_info._my_pos.y * map_info._width)
    if my_bit & map_info._bm_my_gunner_claims:
        safe = []
        for direction in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
            pos = map_info.pos_add(map_info._my_pos, direction)
            bit = 1 << (pos.x + pos.y * map_info._width) if map_info.in_bounds(pos) else 0
            if (
                map_info.in_bounds(pos)
                and map_info.is_passable(pos)
                and _tile_open(pos.x, pos.y)
                and not (bit & (map_info._bm_friendly_bots | map_info._bm_enemy_bots))
                and not (bit & map_info._bm_my_gunner_claims)
                and rc.can_move(direction)
            ):
                safe.append(direction)
        if safe and nav.move(safe[0]):
            _log_status()
            return

    # Central launcher: if we're beside its tile, build it (if missing) or hold
    # still to be flung toward our goal. Only once we've been launched away (or no
    # launcher can serve us) do we fall through to normal behaviour.
    if launch_plan.ensure_launcher_or_wait(rc):
        _log_status()
        return

    # Dispatch: economy > attack/generalist.
    if _economy_builder:
        econ_builder.run()
    else:
        atk_builder.run()

    _log_status()


def _role_name():
    if _economy_builder:
        return "econ"
    if _atk_bot:
        return "atk%d" % (_atk_index if _atk_index is not None else -1)
    return "gen"


def _log_status():
    role = _role_name()
    if _chosen_state is not None:
        state = _chosen_state.__name__.rsplit(".", 1)[-1]
        target = getattr(_chosen_state, "target", None)
    else:
        state, target = "idle", None
    status("id=%d %s role=%s state=%s target=%s" % (
        rc.get_id(), map_info._my_pos, role, state, target))
