from main import has_op
from fcode import Controller, Position, Direction

import random

import map_info
import pathing
from pathing import Pathing
import comms
import conveyor_plan as convplan
from units.spawn_plan import get_ray_endpoint, INITIAL_EXPLORE_MAX_STEPS, INITIAL_SPAWN_COUNT

import units.states.explore  as explore
import units.states.disrupt  as disrupt
import units.states.harvest  as harvest
import units.states.route    as route
import units.states.heal     as heal
import units.states.attack   as attack
import units.states.chase    as chase
import units.states.chip     as chip
import units.states.block    as block
import units.states.siege    as siege
import units.states.relay    as relay
import units.states.ringcut  as ringcut
import units.states.ringhold as ringhold
import units.states.unblock  as unblock
import units.defense as defense

from log import DRAW_DEBUG, log, DEBUG_STATE
import metrics


rc: Controller
nav: Pathing = None

# Sorted in descending order of max score to allow early break in selection loop
states = tuple(sorted(
    [explore, disrupt, harvest, route, heal, attack, chase, chip, block, siege, relay, ringcut, ringhold, unblock],
    key=lambda s: s.MAX_SCORE,
    reverse=True
))

# Harvest zones are calculated based on map symmetry with fallback
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

# Conveyor plan the core hands this builder on its spawn turn via slot 0. Decoded
# once, on this builder's first run, into {Position: facing} where facing is the
# conveyor's output direction (down-tree toward the core). None until read; stays
# None for builders the core did not root a plan at. Foundation only -- nothing
# acts on it yet.
conveyor_plan: dict | None = None
_plan_read = False


def _core_ward_dir(pos: Position):
    """The cardinal direction from `pos` to its single core-adjacent neighbour,
    or None if `pos` touches no core tile. A plan root is always orthogonally
    adjacent to exactly one core cell, and that side is the one it outputs to."""
    core = map_info._bm_my_core_area
    w, h = map_info._width, map_info._height
    for d in convplan.CARDINALS:
        dx, dy = d.delta()
        nx, ny = pos.x + dx, pos.y + dy
        if 0 <= nx < w and 0 <= ny < h and (core >> (nx + ny * w)) & 1:
            return d
    return None


def _try_read_conveyor_plan():
    """First-run only: pull the opening conveyor plan out of slot 0 and decode it
    from my own tile. The core writes a plan word only on a turn it spawns one
    plan-builder, so a marker on my first turn means the plan is mine and my tile
    is its root. Accept it only if I actually sit next to the core (a valid root),
    deriving the core-ward output side from that neighbour -- a stray reader that
    isn't core-adjacent is rejected."""
    global conveyor_plan
    dfs_bits = comms.read_core_plan()
    if dfs_bits is None:
        return
    excluded = _core_ward_dir(map_info._my_pos)
    if excluded is None:
        return
    conveyor_plan = convplan.decode_dfs(map_info._my_pos, excluded, dfs_bits)


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


def select_best_state(can_move=True, exclude=None):
    best_state = None
    best_score = 0
    _scored = []
    for state in states:
        if state is exclude:
            continue
        # Since states are sorted, break early if we can't beat best score
        if best_score >= state.MAX_SCORE:
            break

        score = state.score(can_move)
        if metrics.ENABLED:
            _scored.append((state.__name__.rsplit(".", 1)[-1], score))
        if score > best_score:
            best_score = score
            best_state = state
    if metrics.ENABLED and can_move:
        metrics.turn(map_info._my_pos, rc.get_global_resources(),
                     best_state.__name__.rsplit(".", 1)[-1] if best_state else "none",
                     _scored)
    if best_state is not None:
        log(f"Builder selected state {best_state.__name__} with score {best_score}"
            f"{'' if can_move else ' (in-place)'}")
    return best_state


def run():
    global _stay_near_core, _first_run_done, _plan_read, conveyor_plan

    # Sync round info
    current_round = rc.get_current_round()
    if not _first_run_done:
        _first_run_done = True
    #     if current_round == 1:
    #         _stay_near_core = True
    map_info.update(recompute=False)
    comms.read()          # absorb every slot's shared tiles/symmetry, broadcast our own
    map_info.add_comm_allies(comms.ally_positions())   # out-of-vision teammates -> friendly masks
    map_info.recompute_derived()
    if metrics.ENABLED:
        metrics.begin(current_round, rc.get_id())
        if current_round % 25 == 0:
            # Network health: how much of our conveyor network actually DELIVERS
            # to the core. conv_dist_core >= 0 iff a conveyor transitively points
            # into a core tile; -1 means it carries nothing home no matter what
            # it holds.
            _mine = (map_info._bm_conveyors
                     & map_info._bm_team[map_info._my_team_idx])
            _cd = map_info.conv_dist_core
            _tot = _mine.bit_count()
            _conn = 0
            _m = _mine
            while _m:
                _b = _m & -_m
                _n = _b.bit_length() - 1
                _m ^= _b
                if _n < len(_cd) and _cd[_n] >= 0:
                    _conn += 1
            metrics.act("net", "health", conv=_tot, connected=_conn,
                        harv=(map_info._bm_et[map_info._IDX_HARVESTER]
                              & map_info._bm_team[map_info._my_team_idx]).bit_count(),
                        ti=rc.get_global_resources())
    draw_mask(map_info._bm_enemy_bots, 255, 255, 255)

    # First run only: decode the opening conveyor plan the core queued in slot 0
    # on the turn it spawned us (read here after comms.read() cached the slot).
    if not _plan_read:
        _plan_read = True
        _try_read_conveyor_plan()
    # Hold the defender-spawn reserve only while something is actually at our
    # door. A builder out on the map can't see the core, so it takes the sentry's
    # alarm as the shared signal.
    alarm = comms.read_alarm()
    map_info.arm_reserve(bool(alarm and alarm[1] is not None)
                         or bool(defense.threatening_enemies()))
    _update_harvest_zone()

    # First few builder bots derive explore target from spawn position
    _update_initial_explore(current_round)

    # Run state-specific logic.
    best_state = select_best_state()
    # LOCAL DEBUG (log.DEBUG_STATE): one line per BUILDER per TURN so any local
    # replay can be read directly -- state, position, titanium, build scale, the
    # inferred enemy core and distance-to-ring. ON by default for development;
    # tools/make_submission.py flips it OFF for ladder builds, because the platform
    # discards stdout and the output measured ~2.3% CPU.
    if DEBUG_STATE:
        try:
            import relaygeom as _rg
            _tc = _rg.their_core()
            _d = _rg.dist_at((map_info._my_pos.x, map_info._my_pos.y)) if _tc else -1
        except Exception:
            _tc, _d = None, -1
        _p = map_info._my_pos
        print("ST r=%-4d u=%-4d pos=(%2d,%2d) state=%-9s ti=%-4d scale=%-4d theircore=%s dist=%s" % (
            rc.get_current_round(), rc.get_id(), _p.x, _p.y,
            best_state.__name__.rsplit(".", 1)[-1] if best_state else "none",
            rc.get_global_resources(), rc.get_scale_percent(), _tc, _d), flush=True)
    # The opening conveyor plan is a pre-combat build order; the moment this builder
    # is actually pulled into a fight (heal or attack), that plan is stale -- throw
    # it away for good so it never resumes, and route falls to normal routing.
    # Was: conveyor_plan = None -- permanent, the first time this builder was
    # ever pulled into a fight. attack scores 9 and heal 9.5 against route's 5,
    # so one enemy sighting ended the build order for the rest of the game and
    # stranded whatever conveyors were already laid. Being pulled into a fight is
    # a PAUSE. _plan_next_action() re-checks every step against the live map, so
    # a plan that really has gone stale simply yields nothing.
    pass
    best_state.run()

    # The relay builder standing on a launcher's pickup tile is deliberately
    # spending its turn on nothing. The heal below would spend the action it is
    # holding, and the free-action retry after it can MOVE -- either strands the
    # throw, and with it every hop that was going to follow.
    # ORDER MATTERS: this must come AFTER best_state.run(). Putting it before
    # meant a holding relay returned without ever running relay.run() -- the call
    # that BUILDS the next launcher -- so the chain stopped after one hop.
    if best_state is relay and relay.holding():
        comms.write()
        return

    heal._do_best_heal()

    # Free-action retry: if we've neither moved nor acted this turn, our single
    # move-or-action is still unspent. Rather than waste it, pick the best thing
    # we can do WITHOUT moving (a DIFFERENT state than we already chose) and do
    # only its in-place action -- so we hold position but still use the action.
    if has_op():
        second = select_best_state(can_move=False, exclude=best_state)
        if second is not None:
            second.run(False)

    comms.write()
