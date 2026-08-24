# Route extends our conveyor network one hop at a time: it picks up a dead end
# (or an orphaned harvester) and lays the next conveyor toward the accepting
# network, quoting only PAYG_HORIZON hops rather than the whole remaining chain.
# See _config.PAYG_HORIZON.
from _config import PAYG_HORIZON
from main import has_op
import map_info
import pathing
from pathing import Pathing
from fcode import *
import random
import units.builder
import payg
import comms
from log import log
rc: Controller = None
nav: Pathing = None
_cost_map: dict[int, tuple[int, int]] = {}  # tile index -> (min titanium cost, round recorded)

UNPATHABLE_TTL = 40
_unpathable: dict[int, int] = {}   # tile index -> round recorded

# Conveyors face cardinals only and can only be built on a cardinally adjacent
# tile, so a reconstructed route step is always one of these four -- plus (0, 0),
# which bfs_route returns when the start tile is already a target and which maps
# to CENTRE so the build below simply fails.
_DIR_BY_DELTA = {
    (0, -1): Direction.NORTH,
    (0, 1): Direction.SOUTH,
    (-1, 0): Direction.WEST,
    (1, 0): Direction.EAST,
    (0, 0): Direction.CENTRE,
}

def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav

def _mark_unpathable(mask: int):
    """Record every tile in `mask` as unroutable as of this round."""
    current = rc.get_current_round()
    while mask:
        lsb = mask & -mask
        mask ^= lsb
        _unpathable[lsb.bit_length() - 1] = current


def _unpathable_mask():
    """Bitmask of tiles whose last path attempt failed recently enough to skip."""
    current = rc.get_current_round()
    result = 0
    stale = []
    for n, turn in _unpathable.items():
        if turn + UNPATHABLE_TTL < current:
            stale.append(n)
            continue
        result |= 1 << n
    for n in stale:
        del _unpathable[n]
    return result


def not_blocked():
    '''
    it is not blocked if
    it does not have a conveyor taking it
    and it does have a place to put a conveyor
    '''
    my_team_idx = map_info._my_team_idx
    my_connected = (
        map_info._bm_et[map_info._IDX_SPLITTER]
        | map_info._bm_et[map_info._IDX_CORE]
    ) & map_info._bm_team[my_team_idx]
    w = map_info._width
    conveyors = (map_info._bm_et[map_info._IDX_CONVEYOR])&map_info._bm_team[my_team_idx]
    left_conveyors = ((conveyors&~map_info._bm_conv_by_dir[map_info._DIR_E])&map_info._not_right_col)<<1
    right_conveyors = ((conveyors&~map_info._bm_conv_by_dir[map_info._DIR_W])&map_info._not_left_col)>>1
    up_conveyors = ((conveyors&~map_info._bm_conv_by_dir[map_info._DIR_S]))<<w
    down_conveyors = ((conveyors&~map_info._bm_conv_by_dir[map_info._DIR_N]))>>w
    blocking = (
        map_info._bm_team[1-my_team_idx]
        | ~map_info._bm_env[map_info._IDX_ENV_EMPTY]
        | (map_info._bm_team[my_team_idx]
        & ~map_info._bm_et[map_info._IDX_CONVEYOR])
    )
    already_routed = map_info.expand_manhattan(my_connected) | left_conveyors | right_conveyors | up_conveyors | down_conveyors
    blocked = (
        (((blocking & map_info._not_left_col) >> 1) | ~map_info._not_right_col)
        & (((blocking & map_info._not_right_col) << 1) | ~map_info._not_left_col)
        & ((blocking >> w) | map_info._bottom_row)
        & ((blocking << w) | map_info._top_row)
    )
    return map_info._board_mask & ~already_routed & ~blocked & ~map_info._bm_enemy_turret_threat

def _my_claims(repair=False):
    my_pos = map_info._my_pos
    my_mask = 1 << (my_pos.x + my_pos.y * map_info._width)

    # Routable conveyors whose output is not connected to my ore-accepting network.
    # We normally steer clear of enemy turret threat, but a dead end ADJACENT to a
    # route target (one hop from the accepting network -- a conveyor whose chain
    # already reaches the core, or the core itself) is exempt: finishing that last
    # connection into the network is worth building into the threat zone.
    adj_route = map_info.expand_manhattan(
        map_info._bm_route_targets | map_info._bm_my_core_area)
    candidates = map_info._bm_dead_end & (~map_info._bm_enemy_turret_threat | adj_route)

    # Orphaned harvesters, masked to my team. Was unmasked, so ENEMY harvesters
    # entered route's candidate set -- and route (5) outranks harvest (4), heal
    # (3), disrupt (2) and explore (1), so every such turn outranked our own
    # economy. Measured against loki: enemy harvesters present in the mask on
    # 528/614/753 builder-turns on saga/jackpot/hive, and actually selected as
    # the build target 11-15 times a game.
    my_harvesters = map_info._bm_et[map_info._IDX_HARVESTER] & map_info._bm_team[map_info._my_team_idx]
    if my_harvesters:
        candidates |= my_harvesters & not_blocked()

    if repair:
        # Repair-quality: only a candidate one hop from the accepting network --
        # cardinally adjacent to a valid route target (a conveyor whose chain
        # already reaches the core, or the core itself). Scores at the higher
        # repair tier (formerly the separate route_repair state).
        candidates &= adj_route

    if units.builder._stay_near_core:
        candidates &= units.builder.near_core_mask()
    # Route has the highest MAX_SCORE, so this runs first for every builder on
    # every turn -- including the many with no dead end anywhere. Don't pay for
    # the reject sets until we know there is something to reject.
    if not candidates:
        return 0
    avoid = (
        payg.too_expensive(_cost_map, rc.get_global_resources(), rc.get_current_round())
        | _unpathable_mask()
    )
    others = map_info.claim_bots()   # drop dedicated rushers -- they never route
    units.builder.draw_mask(pathing.claim_subset(my_mask, others, candidates & ~avoid, tie_self=True), 0, 255, 0)
    return pathing.claim_subset(my_mask, others, candidates & ~avoid, tie_self=True)

_cached_target = None        # (destroy, nxt, seg_dist) picked+validated in score()
_cached_plan_action = None   # ("conveyor", pos, facing) | ("harvester", ore) | None
_is_repair = False           # was _cached_target the REPAIR (max-score) tier target?


# --- Opening conveyor plan (handed to us by the core in comms slot 0, decoded
# into units.builder.conveyor_plan). We build it in the DFS order the core sent
# it: parent conveyor before child, so each conveyor's output already has the
# tile it feeds. A harvester is placed right after the conveyor that sits next to
# its ore, which is the order the plan assumes.
def _bit(pos) -> int:
    return 1 << (pos.x + pos.y * map_info._width)


def _my_conveyor_at(pos) -> bool:
    return bool(map_info._bm_et[map_info._IDX_CONVEYOR]
                & map_info._bm_team[map_info._my_team_idx] & _bit(pos))


def _my_harvester_at(pos) -> bool:
    return bool(map_info._bm_et[map_info._IDX_HARVESTER]
                & map_info._bm_team[map_info._my_team_idx] & _bit(pos))


def _adjacent_ore_needing_harvester(pos):
    """An orthogonally adjacent, currently empty ore tile with no harvester yet,
    or None. `pos` is a plan conveyor we've just confirmed is built. (The harvest
    state masks core-visible ore out of its own targets; the plan places its
    harvesters unconditionally.)"""
    w, h = map_info._width, map_info._height
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    valid = []
    for d in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
        nb = pos.add(d)
        if 0 <= nb.x < w and 0 <= nb.y < h:
            n = nb.x + nb.y * w
            if ((ore >> n) & 1 and not _my_harvester_at(nb)
                    and map_info._building_et_idx[n] < 0):
                valid.append(nb)
    return random.choice(valid) if valid else None


def _core_ward_dir(pos):
    """If `pos` is orthogonally adjacent to one of our core tiles, the direction
    that points into it, else None. A conveyor next to the core should always
    output straight into it -- strictly better than routing one more hop."""
    w, h = map_info._width, map_info._height
    core = map_info._bm_my_core_area
    for d in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
        nb = pos.add(d)
        if 0 <= nb.x < w and 0 <= nb.y < h and (core >> (nb.x + nb.y * w)) & 1:
            return d
    return None


_ADJ_COST_CAP = 8   # how far we BFS to compare which plan conveyor is closer to reach


def _my_dist_field(cap: int) -> dict:
    """tile index -> builder moves to reach that tile over passable ground, up to
    `cap`. The builder's own tile is 0 even if passable() excludes it."""
    w = map_info._width
    my = map_info._my_pos
    n0 = my.x + my.y * w
    start = 1 << n0
    passable = map_info.passable()
    field = {n0: 0}
    frontier = start
    visited = start
    for d in range(1, cap + 1):
        frontier = map_info.expand_manhattan(frontier) & passable & ~visited
        if not frontier:
            break
        visited |= frontier
        m = frontier
        while m:
            b = m & -m
            m ^= b
            field[b.bit_length() - 1] = d
    return field


def _moves_to_build(field: dict, pos):
    """Fewest builder moves in `field` to a tile from which `pos` can be built -- a
    cardinal NEIGHBOUR of pos (you build from beside a tile, not on top of it). 0 if
    a neighbour is already underfoot (adjacent now); standing directly ON pos costs
    >=1 because its neighbours are a step away. None if no neighbour is within `cap`."""
    w, h = map_info._width, map_info._height
    best = None
    for d in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
        nb = pos.add(d)
        if 0 <= nb.x < w and 0 <= nb.y < h:
            n = nb.x + nb.y * w
            if n in field and (best is None or field[n] < best):
                best = field[n]
    return best


def _plan_next_action():
    """The next piece of this builder's opening plan to work on, or None if the plan
    is complete (or this builder has none). Conveyors go down in DFS order (parent
    before child, so each conveyor's output tile already exists); the harvester next
    to a conveyor follows once that conveyor is up.

    Conveyor choice: at any moment at most TWO conveyors are eligible -- the first
    one not yet built (k), and the one immediately after it in the plan (k+1), the
    latter only if it too is still unbuilt. We build whichever the builder can get
    ADJACENT to in fewer moves (already adjacent = 0; standing directly on a tile is
    >=1, since you must step off to build it), so we never walk back for an in-order
    tile when the next one is already under our feet. We never build past k+1, so a
    conveyor is at most one step ahead of the first gap."""
    plan = units.builder.conveyor_plan
    if not plan:
        return None
    items = list(plan.items())
    n = len(items)
    for i in range(n):
        pos, facing = items[i]
        if _my_conveyor_at(pos):
            ore = _adjacent_ore_needing_harvester(pos)
            if ore is not None:
                return ("harvester", ore, pos)
            continue
        # items[i] is the first unbuilt conveyor (k). Its only companion candidate is
        # items[i+1] (k+1), and only while that one is unbuilt too.
        candidates = [(pos, facing)]
        if i + 1 < n:
            npos, nfacing = items[i + 1]
            if not _my_conveyor_at(npos):
                candidates.append((npos, nfacing))
        if len(candidates) == 1:
            return ("conveyor", pos, facing)
        # Build whichever is cheaper to reach-and-build; ties keep k (the earlier one).
        field = _my_dist_field(_ADJ_COST_CAP)
        best = None
        best_cost = None
        for cpos, cfacing in candidates:
            c = _moves_to_build(field, cpos)
            if c is None:
                continue
            if best_cost is None or c < best_cost:
                best_cost = c
                best = (cpos, cfacing)
        if best is None:
            # Neither reachable within the cap (builder got pushed far off the line) --
            # default to k and let _run_plan_action walk us back with full pathing.
            return ("conveyor", pos, facing)
        return ("conveyor", best[0], best[1])
    return None


def _find_route_target(repair=False):
    """Scan claimed candidates and return the first (destroy, nxt, seg_dist) we can
    actually route -- pathable and affordable -- or None. Doing this in score()
    (rather than run()) means route isn't selected at all when nothing is
    reachable, so a lower state gets the turn instead of it being wasted. With
    repair=True only candidates one hop from the network are considered, and every
    conveyor end is seeded at cost 0 (end_cost_mask=0)."""
    candidates = _my_claims(repair)
    if not candidates:
        return None
    width = map_info._width
    my_team_bm = map_info._bm_team[map_info._my_team_idx]
    # Priced before any destroy, which would lower the build scale, so this gate
    # and the destroy gate in run() quote the same number.
    need = rc.get_conveyor_cost() + map_info.ti_reserve()
    while candidates:
        candidate, _ = nav.closest(candidates)
        if candidate is None:
            _mark_unpathable(candidates)
            return None
        cand_n = candidate.x + candidate.y * width
        cand_bit = 1 << cand_n
        cand_is_harvester = bool(map_info._bm_et[map_info._IDX_HARVESTER] & cand_bit)
        # Seed only the core at cost 0 (every conveyor end pays conveyor_end_cost),
        # so a nearer attach always wins on distance regardless of loaded state.
        cand_path = nav.calculate_conveyor_path(
            candidate, update=not cand_is_harvester,
            end_cost_mask=(0 if repair else map_info._bm_conveyors))
        if cand_path is None:
            _mark_unpathable(cand_bit)
            candidates &= ~cand_bit
            continue
        cost = nav.conveyor_cost(min(cand_path[2], PAYG_HORIZON))
        _cost_map[cand_n] = (cost, rc.get_current_round())
        if rc.get_global_resources() < cost:
            candidates &= ~cand_bit
            continue
        tc0 = cand_path[0]
        tc0_n = tc0.x + tc0.y * width
        et_tc0 = map_info._building_et_idx[tc0_n]
        if et_tc0 >= 0:
            is_mine = bool((my_team_bm >> tc0_n) & 1)
            if not is_mine:
                # An enemy BARRIER on the tile we'd route through is NOT a skip: bfs_route
                # weighted it (BARRIER_ROUTE_COST) and chose it anyway, so run() attacks it
                # down and then builds. Any OTHER enemy building we still can't route
                # through -- skip the candidate.
                if et_tc0 != map_info._IDX_BARRIER:
                    _mark_unpathable(cand_bit)
                    candidates &= ~cand_bit
                    continue
            # One of our own buildings (the hard_block re-orient case) must be
            # destroyed first, which is gated on the spawn reserve; require it.
            elif rc.get_global_resources() < need:
                candidates &= ~cand_bit
                continue
        return (tc0, cand_path[1], cand_path[2])
    return None


def _adjacent_to_me(pos) -> bool:
    my = map_info._my_pos
    return abs(pos.x - my.x) + abs(pos.y - my.y) == 1


# Folds in the former route_repair state: a repair-quality target (a candidate one
# hop from the network) scores REPAIR_SCORE; the opening plan and ordinary dead-end
# routing score NORMAL_SCORE. When a repair target is ALREADY adjacent (buildable
# this turn) it jumps to ADJ_REPAIR_SCORE -- above attack (9), so an immediate econ
# reconnect isn't preempted by a fight. MAX_SCORE is the highest so the selection
# loop's early-break stays correct.
NORMAL_SCORE = 6.1
REPAIR_SCORE = 8
ADJ_REPAIR_SCORE = 9.2
MAX_SCORE = 9.2
def score(can_move=True):
    global _cached_target, _cached_plan_action, _is_repair
    _is_repair = False
    # Rush mode: this builder has finished its economy and no longer routes at all.
    # The dedicated patrol/defence bot likewise never routes -- it ignores its opening
    # plan and patrols the belts from the start (units.states.patrol). (It still has a
    # conveyor_plan set, which keeps it out of the plan-less first-run rush path.)
    if units.builder.in_rush_mode() or units.builder.is_patrol_builder():
        _cached_plan_action = None
        _cached_target = None
        return 0
    # 1. Repair first -- a candidate one hop from the accepting network -- at the
    # highest tier (formerly the separate route_repair state, which outranked the
    # opening plan and ordinary routing).
    _cached_plan_action = None
    _cached_target = _find_route_target(repair=True)
    if _cached_target is not None:
        adj = _adjacent_to_me(_cached_target[0])
        if not can_move and not adj:
            _cached_target = None
            return 0
        _is_repair = True
        # Already adjacent -> we lay the reconnect THIS turn; a tier above attack.
        return ADJ_REPAIR_SCORE if adj else REPAIR_SCORE
    # 2. Building our own opening plan runs at route's normal tier -- "forced valid"
    # while unbuilt, but not a max that overrides everything: attack (tier 9) can
    # still preempt it. We DON'T bail on it just because an enemy or turret threat
    # showed up -- only once the builder is actually pulled into heal or attack
    # (builder.run clears conveyor_plan then), after which _plan_next_action returns
    # None and we fall through to normal routing.
    _cached_plan_action = _plan_next_action()
    if _cached_plan_action is not None:
        _cached_target = None
        # In-place retry: only if the next plan piece is buildable right here
        # (action[1] is the tile it places -- conveyor pos or harvester ore).
        if not can_move and not _adjacent_to_me(_cached_plan_action[1]):
            _cached_plan_action = None
            return 0
        return NORMAL_SCORE
    # 3. Ordinary dead-end routing at route's normal tier.
    # units.builder.draw_mask(map_info._bm_dead_end, 0, 0, 255)
    _cached_target = _find_route_target(repair=False)
    if _cached_target is not None and not can_move and not _adjacent_to_me(_cached_target[0]):
        _cached_target = None
    return NORMAL_SCORE if _cached_target is not None else 0


def _run_plan_action(action, can_move=True) -> None:
    """Move adjacent to the next plan tile and place it (conveyor or harvester)."""
    if action[0] == "conveyor":
        _, pos, facing = action
        cd = _core_ward_dir(pos)
        if cd is not None:
            facing = cd                     # next to the core: always output into it
        # Move into place first: already adjacent+safe -> bfs_move keeps us put and we
        # build below; on a friendly gunner's lane -> it steps us off instead.
        if nav.move_adjacent(pos, allow_bots=True, can_move=can_move):
            return
        need = rc.get_conveyor_cost() + map_info.ti_reserve()
        if rc.get_global_resources() >= need and rc.can_build_conveyor(pos, facing):
            rc.build_conveyor(pos, facing)
            map_info.update_at(pos)
    else:  # harvester, right after the conveyor next to its ore
        _, ore, conv = action
        if nav.move_adjacent(ore, allow_bots=True, can_move=can_move):
            return
        need = rc.get_harvester_cost() + map_info.ti_reserve()
        if rc.get_global_resources() >= need and rc.can_build_harvester(ore):
            rc.build_harvester(ore)
            map_info.update_at(ore)
            # route-tally removed; siege gates on predicted income now

def run(can_move=True):
    # Opening plan takes precedence when this builder has one still going up.
    if _cached_plan_action is not None:
        log("ROUTE-PLAN")
        _run_plan_action(_cached_plan_action, can_move)
        # Completing the opening plan (its last piece just went up -> nothing left to
        # build) latches this builder into rush mode.
        if units.builder.conveyor_plan and _plan_next_action() is None:
            units.builder.enter_rush_mode()
        return

    target = _cached_target      # (destroy, nxt, seg_dist), validated in score()
    if target is None:
        return
    log("ROUTE")
    width = map_info._width
    # The REPAIR (max-score) tier ignores the ti reserve; ordinary dead-end routing
    # (NORMAL tier) keeps it. The opening plan is also NORMAL -- see _run_plan_action.
    need = rc.get_conveyor_cost() + (0 if _is_repair else map_info.ti_reserve())

    destroy, nxt, seg_dist = target
    # Move into position first. bfs_move keeps us put when we're already adjacent
    # and safe (so we destroy/build below), but steps us off our tile if it's now
    # lethal -- flee instead of standing there to build and dying.
    if nav.move_adjacent(destroy, can_move=can_move):
        return
    # An ENEMY barrier sitting where the next conveyor must go: bfs_route routed
    # through it (at BARRIER_ROUTE_COST), so attack it down (2 dmg/shot, like chip)
    # instead of building. Once it dies the tile is clear and the conveyor lays next
    # time. Enemy barriers can't be rc.destroy()'d (that's for our own buildings).
    d_n = destroy.x + destroy.y * width
    if ((map_info._bm_et[map_info._IDX_BARRIER]
         & map_info._bm_team[1 - map_info._my_team_idx]) >> d_n) & 1:
        if rc.get_global_resources() >= 2 and rc.can_fire(destroy):
            rc.fire(destroy)
            log("ROUTE-ATTACK-BARRIER", destroy)
        return
    # `need` is only known to be covered on the branch that found a building
    # here, and can_destroy is engine truth about a tile we may remember as empty.
    if rc.can_destroy(destroy) and has_op() and rc.get_global_resources() >= need:
        rc.destroy(destroy)
        map_info.update_at(destroy)

    direction = _DIR_BY_DELTA.get((nxt.x - destroy.x, nxt.y - destroy.y))
    if direction is None:
        # bfs_route only ever steps cardinally; belt and braces.
        direction = map_info.direction_to(destroy, nxt)
    cd = _core_ward_dir(destroy)
    if cd is not None:
        direction = cd                      # next to the core: always output into it
    built = False
    if rc.can_build_conveyor(destroy, direction):
        rc.build_conveyor(destroy, direction)
        map_info.update_at(destroy)
        built = True
    # A repair build is the LAST step of a route -- it connects a dead end straight
    # into the accepting network. Doing it latches this builder into rush mode.
    if built and _is_repair:
        units.builder.enter_rush_mode()
