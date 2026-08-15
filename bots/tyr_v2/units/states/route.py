# --- pay as you go ------------------------------------------------------------
# harvest and route both quote the *entire* remaining conveyor chain up front and
# refuse the project unless all of it is affordable this turn. That silently caps
# how far from our network a harvester can ever be built, and it is why our
# economy stays small: across five games against sporks we finish on 145
# conveyors and 22 harvesters to their 490 and 46.
#
# Quoting only the next few hops fixes it. A conveyor laid this turn is useful
# next turn whether or not the rest of the chain exists, and route picks the dead
# end up and extends it.
#
# Swept against Champion_v45 over all 33 maps, both sides. The horizon has a
# plateau, not a threshold -- too short is worse than not doing it at all,
# because the builder commits to chains it cannot finish:
#
#     3   42.4%      10  60.6%
#     5   40.9%      11  54.5%
#     8   56.1%      12  48.5%
#     9   51.5%      16  42.4%
#                    unbounded (v45) is the 50.0% baseline
#
# 8-11 are all above the baseline and 3/5/12/16 are all below, so the plateau is
# supported by 264 matches rather than by the single best cell; 10 is the argmax
# and sits in the middle of it, but read 60.6% as the top of a noisy peak whose
# true value is nearer the ~55% the plateau averages.
#
# The effect on the maps we were losing is not marginal. saga -- 1-21 on the
# ladder, our worst map by a wide margin -- goes from a loss at turn 144 on 34
# conveyors and 750 Ti to a *win* at turn 487 on 101 conveyors and 4500 Ti.
# hive goes 26 -> 97 conveyors and 1890 -> 4480 Ti. heart is the exception and
# builds slightly fewer (42 -> 37); it still wins.
#
# Both call sites must use the same horizon: harvest quotes harvester + chain and
# route quotes chain alone, and a harvester admitted under one budget whose chain
# is refused under the other is the stranded-harvester case this is meant to fix.
PAYG_HORIZON = 10
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
    # Was unmasked by team, so ENEMY harvesters entered route's candidate set --
    # and route (5) outranks harvest (4), heal (3), disrupt (2) and explore (1),
    # so every such turn outranked our own economy. Measured against loki: enemy
    # harvesters present in the mask on 528/614/753 builder-turns on
    # saga/jackpot/hive, and actually selected as the build target 11-15 times
    # a game.
    my_harvesters = map_info._bm_et[map_info._IDX_HARVESTER] & map_info._bm_team[map_info._my_team_idx]
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

MAX_SCORE = 11
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
        cost = nav.conveyor_cost(min(cand_path[2], PAYG_HORIZON))
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
