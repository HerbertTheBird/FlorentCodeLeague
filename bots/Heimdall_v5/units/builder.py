from fcode import Controller, Position

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
import units.atk_states.core_siege as core_siege
import units.atk_states.post_turret as post_turret
import units.econ_states.anti_builder as anti_builder
import units.econ_states.break_turret as break_turret
import units.econ_states.core_repair as core_repair
import units.econ_states.follow as follow
import units.econ_states.park    as park

# Builder-type behaviour modules. The cycle (they each import this module for
# shared state) is safe: nothing here touches their attributes at import time.
import units.atk_builder as atk_builder
import units.econ_builder as econ_builder

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
    [explore, attack, block, core_siege, post_turret],
    key=lambda s: s.MAX_SCORE,
    reverse=True
))

# Economy builders finish routes/harvesting first (so a route is never abandoned
# half-built), then — once economy is delivering to the core — park near the core
# as their idle behaviour instead of wandering (park outranks explore). They also
# run a DEFENSIVE variant of attack (same placement scoring, but never targeting
# the enemy core — see attack._defensive) so they can drop gunners on threats in
# our territory.
_ECONOMY_STATES = tuple(sorted(
    [
        harvest,
        route,
        explore,
        attack,
        block,
        park,
        core_siege,
        anti_builder,
        break_turret,
        core_repair,
        follow,
    ],
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

# Opening roles are assigned by spawn index (one attacker, then three economy).
_atk_bot = False
_atk_index: int | None = None
_economy_builder = False
_economy_index: int | None = None
_opening_role_checked = False

# Latched True once this econ bot has completed at least one route. This marks
# economy readiness and enables parking between jobs, but no longer locks the
# bot out of harvesting or routing additional mines.
_route_done = False
_offense_unlocked_round: int | None = None

# Builder #2 (economy lane 0) commits to the titanium visible on its first
# builder turn. Until that exact mine is connected to the core, no defensive or
# exploration state may interrupt it.
_opening_ore_checked = False
_opening_ore_target: Position | None = None
_opening_route_locked = False


def init(c: Controller):
    global rc, harvest_radius, nav
    rc = c
    nav = Pathing(c)
    harvest_radius = (c.get_map_width() + c.get_map_height()) // 3
    for s in states:
        s.init(c)
    block.init(c)   # attack-only state, not in `states`; init explicitly
    core_siege.init(c)
    post_turret.init(c)
    anti_builder.init(c)
    break_turret.init(c)
    core_repair.init(c)
    follow.init(c)
    park.init(c)    # econ-only idle state, not in `states`; init explicitly


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
    global _chosen_state, _route_done, _opening_route_locked
    best_state = None
    best_score = 0

    # The second opening builder must finish its first visible titanium route
    # before doing anything else. Harvest remains selected while unaffordable;
    # route does the same for missing conveyors, so returning None here means a
    # genuine blocked turn and must not fall through into combat or exploration.
    if _opening_route_locked and _opening_ore_target is not None:
        if map_info.ore_route_reaches_core(_opening_ore_target):
            _opening_route_locked = False
            _route_done = True
            comms.mark_economy_ready(rc.get_id())
        else:
            target_n = _opening_ore_target.x + _opening_ore_target.y * map_info._width
            target_bit = 1 << target_n
            own_mine = (
                map_info._bm_et[map_info._IDX_HARVESTER]
                & map_info._bm_team[map_info._my_team_idx]
                & target_bit
            )
            forced = route if own_mine else harvest
            if forced.score() > 0:
                _chosen_state = forced
                return forced
            # Both economy states deliberately keep a positive score while
            # merely waiting for titanium. Reaching zero means this particular
            # opening mine/line is physically unavailable (destroyed, blocked,
            # or proven unrouteable), so retaining the lock would idle forever.
            # Release it and let normal defense/economy selection choose another
            # mine in this same turn.
            _opening_route_locked = False

    # Once an economy route is within its final completion window, finish the
    # conveyor line before taking any defensive interruption. This also keeps
    # the route selected while titanium is temporarily insufficient, so the bot
    # waits and resumes instead of wandering into combat.
    if _economy_builder:
        route_score = route.score()
        if route_score > 0 and route.should_finish_before_defense():
            _chosen_state = route
            return route

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
    # The first time an econ bot parks, its route is finished — lock it out of
    # route and harvest permanently.
    if _economy_builder and best_state is park:
        _route_done = True
    if _economy_builder and _route_done:
        comms.mark_economy_ready(rc.get_id())
    return best_state


def enemy_core_visible() -> bool:
    return bool(map_info._bm_their_core_area & map_info._bm_visible)


def visible_enemy_builders() -> int:
    """Currently visible enemy builders as a position bitmask."""
    return map_info._bm_enemy_bots & map_info._bm_visible


def offense_unlocked() -> bool:
    """Latch the slow chip advance after economy, or on direct core contact."""
    global _offense_unlocked_round
    if _offense_unlocked_round is not None:
        return True
    from _config import (
        ECON_READY_TO_ATTACK,
        OFFENSE_EARLIEST_ROUND,
        OFFENSE_FALLBACK_ROUND,
    )
    current = rc.get_current_round()
    if (
        enemy_core_visible()
        or current >= OFFENSE_FALLBACK_ROUND
        or (
            current >= OFFENSE_EARLIEST_ROUND
            and comms.economy_ready_count() >= ECON_READY_TO_ATTACK
        )
    ):
        _offense_unlocked_round = current
        return True
    return False


def chip_target() -> Position | None:
    """A deliberately slow waypoint from our core toward the enemy core."""
    own = map_info._my_core
    enemy = map_info._their_core or map_info._predicted_enemy_core
    if own is None or enemy is None:
        return own
    from _config import CHIP_STEP_INTERVAL
    own_x, own_y = own.x + 1, own.y + 1
    enemy_x, enemy_y = enemy.x, enemy.y
    total = max(abs(enemy_x - own_x), abs(enemy_y - own_y), 1)
    if enemy_core_visible() or _atk_bot:
        steps = total
    elif not offense_unlocked():
        steps = min(4, total)
    else:
        elapsed = rc.get_current_round() - (_offense_unlocked_round or rc.get_current_round())
        steps = min(total, 5 + elapsed // CHIP_STEP_INTERVAL)
    wanted_x = own_x + (enemy_x - own_x) * steps // total
    wanted_y = own_y + (enemy_y - own_y) * steps // total

    candidates = []
    for radius in range(0, 5):
        for x in range(wanted_x - radius, wanted_x + radius + 1):
            for y in range(wanted_y - radius, wanted_y + radius + 1):
                if max(abs(x - wanted_x), abs(y - wanted_y)) != radius:
                    continue
                if _tile_open(x, y) and map_info.is_passable(Position(x, y)):
                    candidates.append(Position(x, y))
        if candidates:
            break
    if not candidates:
        return own
    return min(
        candidates,
        key=lambda p: (
            p.distance_squared(Position(wanted_x, wanted_y)),
            p.distance_squared(map_info._my_pos),
            p.x,
            p.y,
        ),
    )


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
    if last is not None and attack.active_chip_gunner():
        my_team = map_info._bm_team[map_info._my_team_idx]
        if map_info._bm_et[map_info._IDX_GUNNER] & my_team & (1 << (last.x + last.y * map_info._width)):
            adj = _nearest_open(
                (last.x + dx, last.y + dy)
                for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy
            )
            if adj is not None:
                return adj
    if not enemy_core_visible():
        return chip_target()
    core = map_info.atk_symmetry_target(_atk_index)
    if core is None:
        return chip_target()
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


def _resolve_opening_ore() -> None:
    """Latch builder #2's nearest route-reachable titanium on its first turn."""
    global _opening_ore_checked, _opening_ore_target, _opening_route_locked
    if _opening_ore_checked or _economy_index != 0:
        return
    visible_ore = (
        map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        & map_info._bm_visible
        & ~map_info._bm_et[map_info._IDX_HARVESTER]
    )
    target = comms.opening_ore()
    if target is not None:
        target_bit = 1 << (target.x + target.y * map_info._width)
        if not (visible_ore & target_bit):
            target = None
    distance = -1
    if target is None:
        target, distance = nav.closest(visible_ore)
    else:
        _reachable, distance = nav.closest(target_bit)
    if target is not None and distance >= 0:
        _opening_ore_target = target
        _opening_route_locked = True
    _opening_ore_checked = True


def opening_ore_target() -> Position | None:
    """The mine reserved by builder #2 while its opening route is locked."""
    return _opening_ore_target if _opening_route_locked else None


def run():
    # Sync round info + shared state, resolve role, then dispatch to the
    # matching builder-type module (each owns its own rebuild/heal policy).
    current_round = rc.get_current_round()
    map_info.update(recompute=False)
    handle_comms()
    map_info.recompute_derived()

    _resolve_opening_role()
    _resolve_opening_ore()
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
        gunner_site = getattr(_chosen_state, "gunner_site", None)
    else:
        state, target, gunner_site = "idle", None, None
    status("id=%d %s role=%s state=%s target=%s gunner_site=%s" % (
        rc.get_id(), map_info._my_pos, role, state, target, gunner_site))
