from main import has_op
import map_info
import pathing
from pathing import Pathing
from fcode import *
import units.builder
import comms
from log import log
import sys
rc: Controller = None
nav: Pathing = None
_cost_map: dict[int, tuple[int, int]] = {}  # tile index -> (min titanium cost, round recorded)
COST_MAP_TTL = 100

unpathable = 0

def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav
def _too_expensive():
    """Bitmask of tiles we know we can't afford right now."""
    ti = rc.get_global_resources()
    current = rc.get_current_round()
    result = 0
    stale = []
    for n, (cost, turn) in _cost_map.items():
        if turn + COST_MAP_TTL < current:
            stale.append(n)
            continue
        log("cost of", n%map_info._width, n//map_info._width, cost)
        if cost > ti:
            result |= 1 << n
    for n in stale:
        del _cost_map[n]
    return result

def _dead_end_conveyors():
    """Bitmask of routable conveyors whose output is not connected to my ore-accepting network."""
    return map_info._bm_dead_end & ~map_info._bm_enemy_turret_threat
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
    bottom_row = ((1 << w) - 1) << (w * (map_info._height - 1))
    top_row = (1 << w) - 1
    blocked = (
        (((blocking & map_info._not_left_col) >> 1) | ~map_info._not_right_col)
        & (((blocking & map_info._not_right_col) << 1) | ~map_info._not_left_col)
        & ((blocking >> w) | bottom_row)
        & ((blocking << w) | top_row)
    )
    return map_info._board_mask & ~already_routed & ~blocked & ~map_info._bm_enemy_turret_threat

def _orphan_harvesters(not_blocked_mask: int):
    my_harvesters = map_info._bm_et[map_info._IDX_HARVESTER]
    if not my_harvesters:
        return 0
    return my_harvesters & not_blocked_mask
def cant_claim():
    w = map_info._width
    my_pos = map_info._my_pos
    my_bit = 1 << (my_pos.x + my_pos.y * w)
    cant = map_info._bm_others_3x3 & ~map_info.expand_chebyshev(my_bit)
    return cant
def _my_claims():
    w = map_info._width
    my_mask = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    avoid = _too_expensive() | cant_claim() | unpathable
    not_blocked_mask = not_blocked()
    candidates = (
        _dead_end_conveyors()
        | _orphan_harvesters(not_blocked_mask)
    ) & ~avoid
    if units.builder._stay_near_core:
        candidates &= units.builder.near_core_mask()
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, candidates, tie_self=True)

_cached_claims = 0

# Connecting a harvester to the core sits just under claiming one: an unrouted
# harvester delivers nothing, so the two have to move together.
MAX_SCORE = 13
def score():
    global _cached_claims
    units.builder.draw_mask(map_info._bm_dead_end, 0, 0, 255)
    _cached_claims = _my_claims()
    return MAX_SCORE if _cached_claims else 0

def run():
    global unpathable
    log("ROUTE")
    candidates = _cached_claims
    if not candidates:
        log("no candidates?")
        return
    width = map_info._width

    best = None
    target_conveyor = [None] * 2

    while candidates:
        candidate, _ = nav.closest(candidates)
        if candidate is None:
            log("no closest???")
            unpathable |= candidates
            return
        cand_n = candidate.x + candidate.y * width
        cand_bit = 1 << cand_n
        cand_is_harvester = bool(map_info._bm_et[map_info._IDX_HARVESTER] & cand_bit)
        cand_path = nav.calculate_conveyor_path(candidate, update=not cand_is_harvester)
        log("PATH", cand_path)
        if cand_path is None:
            unpathable |= cand_bit
            candidates &= ~cand_bit
            continue
        cost = nav.conveyor_cost(cand_path[2])
        _cost_map[cand_n] = (cost, rc.get_current_round())
        if rc.get_global_resources() < cost:
            log("can't afford", cost)
            candidates &= ~cand_bit
            continue
        best = candidate
        target_conveyor = [cand_path[0], cand_path[1]]
        break

    if best is None:
        return

    # If an enemy building sits on the tile we'd route through, we simply can't
    # path here — we don't attack it, so just bail this turn.
    tc0 = target_conveyor[0]
    if (map_info.type_at(tc0.x, tc0.y) is not None
            and map_info.team_at(tc0.x, tc0.y) != map_info._my_team):
        return

    def attempt_build():
        destroy = target_conveyor[0]
        nxt = target_conveyor[1]
        cost = rc.get_conveyor_cost() + map_info.ti_reserve()
        if rc.can_destroy(destroy) and has_op() and rc.get_global_resources() >= cost:
            rc.destroy(destroy)
            map_info.update_at(destroy)
        direction = map_info.direction_to(destroy, nxt)
        if rc.can_build_conveyor(destroy, direction):
            rc.build_conveyor(destroy, direction)
            map_info.update_at(destroy)
            return True
        return False

    built = attempt_build()
    # cand_path[2] is the segment distance from the routed source to the accepting
    # network; a distance-1 segment built now is the last hop, so the route is
    # fully connected this turn. Report it so the core can tally completions.
    if built and cand_path[2] == 1:
        comms.note_route_complete()
    nav.move_adjacent(target_conveyor[0])