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

def _my_claims():
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
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, candidates & ~avoid, tie_self=True)

_cached_claims = 0

MAX_SCORE = 5
def score():
    global _cached_claims
    units.builder.draw_mask(map_info._bm_dead_end, 0, 0, 255)
    _cached_claims = _my_claims()
    return MAX_SCORE if _cached_claims else 0

def run():
    log("ROUTE")
    candidates = _cached_claims
    if not candidates:
        log("no candidates?")
        return
    width = map_info._width
    my_team_bm = map_info._bm_team[map_info._my_team_idx]
    # Priced before any destroy, which would lower the build scale, so the
    # candidate-loop gate and the destroy gate below quote the same number.
    need = rc.get_conveyor_cost() + map_info.ti_reserve()

    target = None
    while candidates:
        candidate, _ = nav.closest(candidates)
        if candidate is None:
            log("no closest???")
            _mark_unpathable(candidates)
            return
        cand_n = candidate.x + candidate.y * width
        cand_bit = 1 << cand_n
        cand_is_harvester = bool(map_info._bm_et[map_info._IDX_HARVESTER] & cand_bit)
        cand_path = nav.calculate_conveyor_path(candidate, update=not cand_is_harvester)
        log("PATH", cand_path)
        if cand_path is None:
            _mark_unpathable(cand_bit)
            candidates &= ~cand_bit
            continue
        cost = nav.conveyor_cost(min(cand_path[2], PAYG_HORIZON))
        _cost_map[cand_n] = (cost, rc.get_current_round())
        if rc.get_global_resources() < cost:
            log("can't afford", cost)
            candidates &= ~cand_bit
            continue

        tc0 = cand_path[0]
        tc0_n = tc0.x + tc0.y * width
        if map_info._building_et_idx[tc0_n] >= 0:
            # If an enemy building sits on the tile we'd route through we can't
            # path here — we don't attack it. Skip this candidate and try the
            # next rather than returning: `_bm_dead_end` includes the target tile
            # of our own conveyor pointing into an enemy building, and bailing
            # outright meant a bot whose closest candidate was such a tile
            # re-picked it every round and did nothing for as long as that
            # building stood.
            if not (my_team_bm >> tc0_n) & 1:
                _mark_unpathable(cand_bit)
                candidates &= ~cand_bit
                continue
            # A tile that already holds one of our buildings — the hard_block
            # re-orient case, our conveyor whose output can neither accept
            # titanium nor be built on — must be destroyed before it can be
            # re-laid, and the destroy is gated on the spawn reserve. Quote that
            # here instead of discovering it after the pick, where failing the
            # gate leaves the conveyor standing, makes can_build_conveyor False,
            # and burns the turn on a candidate we could never act on — every
            # turn, until titanium recovers past the flat 40 Ti reserve.
            if rc.get_global_resources() < need:
                log("destroy blocked by reserve")
                candidates &= ~cand_bit
                continue

        target = (tc0, cand_path[1], cand_path[2])
        break

    if target is None:
        return

    destroy, nxt, seg_dist = target
    # `need` is only known to be covered on the branch that found a building
    # here, and can_destroy is engine truth about a tile we may remember as empty.
    if rc.can_destroy(destroy) and has_op() and rc.get_global_resources() >= need:
        rc.destroy(destroy)
        map_info.update_at(destroy)

    direction = _DIR_BY_DELTA.get((nxt.x - destroy.x, nxt.y - destroy.y))
    if direction is None:
        # bfs_route only ever steps cardinally; belt and braces.
        direction = map_info.direction_to(destroy, nxt)
    built = False
    if rc.can_build_conveyor(destroy, direction):
        rc.build_conveyor(destroy, direction)
        map_info.update_at(destroy)
        built = True

    # seg_dist is the segment distance from the routed source to the accepting
    # network; a distance-1 segment built now is the last hop, so the route is
    # fully connected this turn. Report it so the core can tally completions.
    if built and seg_dist == 1:
        comms.note_route_complete()
    nav.move_adjacent(destroy)
