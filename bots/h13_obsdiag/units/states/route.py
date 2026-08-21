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
import units.builder
import payg
import metrics
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

def cant_claim():
    my_pos = map_info._my_pos
    my_bit = 1 << (my_pos.x + my_pos.y * map_info._width)
    return map_info._bm_others_3x3 & ~map_info.expand_chebyshev(my_bit)

def _my_claims(repair=False):
    my_pos = map_info._my_pos
    my_mask = 1 << (my_pos.x + my_pos.y * map_info._width)

    # Routable conveyors whose output is not connected to my ore-accepting network.
    candidates = map_info._bm_dead_end & ~map_info._bm_enemy_turret_threat

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
        valid_route_targets = map_info._bm_route_targets | map_info._bm_my_core_area
        candidates &= map_info.expand_manhattan(valid_route_targets)

    if units.builder._stay_near_core:
        candidates &= units.builder.near_core_mask()
    # Route has the highest MAX_SCORE, so this runs first for every builder on
    # every turn -- including the many with no dead end anywhere. Don't pay for
    # the reject sets until we know there is something to reject.
    if not candidates:
        return 0
    avoid = (
        payg.too_expensive(_cost_map, rc.get_global_resources(), rc.get_current_round())
        | cant_claim()
        | _unpathable_mask()
    )
    units.builder.draw_mask(pathing.claim_subset(my_mask, map_info._bm_friendly_bots, candidates & ~avoid, tie_self=True), 0, 255, 0)
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, candidates & ~avoid, tie_self=True)

_cached_target = None        # (destroy, nxt, seg_dist) picked+validated in score()
_cached_plan_action = None   # ("conveyor", pos, facing) | ("harvester", ore) | None


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
    for d in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
        nb = pos.add(d)
        if 0 <= nb.x < w and 0 <= nb.y < h:
            n = nb.x + nb.y * w
            if ((ore >> n) & 1 and not _my_harvester_at(nb)
                    and map_info._building_et_idx[n] < 0):
                return nb
    return None


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


def _plan_next_action():
    """The next unbuilt piece of this builder's opening plan, in build order, or
    None if the plan is complete (or this builder has none). Conveyors go down in
    DFS order; the harvester next to a conveyor follows immediately once that
    conveyor is up."""
    plan = units.builder.conveyor_plan
    if not plan:
        return None
    for pos, facing in plan.items():
        if not _my_conveyor_at(pos):
            # Skip a step the map no longer allows rather than returning it every
            # turn and jamming the rest of the plan behind it.
            if not rc.can_build_conveyor(pos, facing):
                continue
            return ("conveyor", pos, facing)
        ore = _adjacent_ore_needing_harvester(pos)
        if ore is not None:
            return ("harvester", ore, pos)
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
        if map_info._building_et_idx[tc0_n] >= 0:
            # An enemy building on the tile we'd route through -- we don't attack
            # it, so skip this candidate.
            if not (my_team_bm >> tc0_n) & 1:
                _mark_unpathable(cand_bit)
                candidates &= ~cand_bit
                continue
            # One of our own buildings (the hard_block re-orient case) must be
            # destroyed first, which is gated on the spawn reserve; require it.
            if rc.get_global_resources() < need:
                candidates &= ~cand_bit
                continue
        return (tc0, cand_path[1], cand_path[2])
    return None


def _adjacent_to_me(pos) -> bool:
    my = map_info._my_pos
    return abs(pos.x - my.x) + abs(pos.y - my.y) == 1


# Folds in the former route_repair state: a repair-quality target (a candidate one
# hop from the network) scores REPAIR_SCORE; the opening plan and ordinary dead-end
# routing score NORMAL_SCORE. MAX_SCORE is the higher of the two so the selection
# loop's early-break stays correct.
NORMAL_SCORE = 5
REPAIR_SCORE = 8
MAX_SCORE = 8
def _score_orig(can_move=True):
    global _cached_target, _cached_plan_action
    # 1. Repair first -- a candidate one hop from the accepting network -- at the
    # highest tier (formerly the separate route_repair state, which outranked the
    # opening plan and ordinary routing).
    _cached_plan_action = None
    _cached_target = _find_route_target(repair=True)
    if _cached_target is not None:
        if not can_move and not _adjacent_to_me(_cached_target[0]):
            _cached_target = None
            return 0
        return REPAIR_SCORE
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
        need = rc.get_conveyor_cost() + map_info.ti_reserve()
        if rc.get_global_resources() >= need and rc.can_build_conveyor(pos, facing):
            rc.build_conveyor(pos, facing)
            map_info.update_at(pos)
        else:
            nav.move_adjacent(pos, allow_bots=True, can_move=can_move)
    else:  # harvester, right after the conveyor next to its ore
        _, ore, conv = action
        need = rc.get_harvester_cost() + map_info.ti_reserve()
        if rc.get_global_resources() >= need and rc.can_build_harvester(ore):
            rc.build_harvester(ore)
            map_info.update_at(ore)
            comms.note_route_complete()
        else:
            nav.move_to(conv, can_move=can_move)

def run(can_move=True):
    # Opening plan takes precedence when this builder has one still going up.
    if _cached_plan_action is not None:
        log("ROUTE-PLAN")
        _run_plan_action(_cached_plan_action, can_move)
        return

    target = _cached_target      # (destroy, nxt, seg_dist), validated in score()
    if target is None:
        return
    log("ROUTE")
    width = map_info._width
    need = rc.get_conveyor_cost() + map_info.ti_reserve()

    destroy, nxt, seg_dist = target
    # Move into position first. bfs_move keeps us put when we're already adjacent
    # and safe (so we destroy/build below), but steps us off our tile if it's now
    # lethal -- flee instead of standing there to build and dying.
    if nav.move_adjacent(destroy, can_move=can_move):
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
    if metrics.ENABLED and rc.can_build_conveyor(destroy, direction):
        # Why this facing? Log the chosen next-tile and what each cardinal
        # neighbour would have offered, so a bad facing can be attributed to the
        # route tie-break (_core_ward_key: core tiles, then conv_dist_core, then
        # Manhattan) rather than guessed at.
        _w = map_info._width
        _cd = map_info.conv_dist_core
        _alt = []
        for _d, _dx, _dy in (("N", 0, -1), ("E", 1, 0), ("S", 0, 1), ("W", -1, 0)):
            _nx, _ny = destroy.x + _dx, destroy.y + _dy
            if not (0 <= _nx < _w and 0 <= _ny < map_info._height):
                continue
            _n = _nx + _ny * _w
            _alt.append("%s:c%d,d%s,ti%d" % (
                _d,
                (map_info._bm_conveyors >> _n) & 1,
                (_cd[_n] if _n < len(_cd) else -1),
                (map_info._bm_ti_carrying >> _n) & 1))
        metrics.act("route", "conveyor",
                    at=(destroy.x, destroy.y),
                    face=str(direction).split(".")[-1][:1],
                    nxt=(nxt.x, nxt.y),
                    alts="|".join(_alt))
    if rc.can_build_conveyor(destroy, direction):
        rc.build_conveyor(destroy, direction)
        map_info.update_at(destroy)
        built = True

    # A conveyor built as the last hop (seg_dist == 1) connects the routed source
    # to a route target, so a real route is completed this turn -- tally it. Both
    # repair-tier and ordinary routing count (formerly route + route_repair both did).
    if built and seg_dist == 1:
        comms.note_route_complete()


# ---- #35 instrumentation ------------------------------------------------------
_OBS = [0, 0, 0]     # route score() calls, bails with no reachable target, of
                     # those where relaxing ENEMY BREAKABLE buildings opens a path


def _obs_probe():
    """Would treating enemy breakables as passable open something we cannot reach?

    _bm_blocked contains every enemy building except conveyors/splitters, so a
    single enemy barrier does not make a route EXPENSIVE -- it deletes it from the
    search entirely. This counts how often that is what stopped us.
    """
    import map_info as _mi
    _OBS[1] += 1
    try:
        mine = _mi._bm_team[_mi._my_team_idx]
        breakable = _mi._bm_blocked & ~mine & ~_mi._bm_my_core_area & ~_mi._bm_their_core_area
        if not breakable:
            return
        walk = _mi.passable()
        my = _mi._my_pos
        seen = 1 << (my.x + my.y * _mi._width)
        relaxed = walk | breakable
        for _ in range(40):
            nxt = _mi.manhattan(seen) & relaxed & ~seen
            if not nxt:
                break
            seen |= nxt
        strict = 1 << (my.x + my.y * _mi._width)
        for _ in range(40):
            nxt = _mi.manhattan(strict) & walk & ~strict
            if not nxt:
                break
            strict |= nxt
        if (seen & ~strict & _mi._bm_route_targets):
            _OBS[2] += 1
    except Exception:
        pass


def obs_report():
    print("OBSDIAG route_calls=%d bailed=%d breakable_would_open=%d" % tuple(_OBS), flush=True)


def score(can_move=True):
    _OBS[0] += 1
    v = _score_orig(can_move)
    if not v:
        _obs_probe()
    return v
