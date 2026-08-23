# Harvest quotes harvester + the next PAYG_HORIZON conveyor hops rather than the
# whole remaining chain. See _config.PAYG_HORIZON.
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

def _my_claims(repair=False):
    my_pos = map_info._my_pos
    w = map_info._width
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    available = harvestable_ore() & ~payg.too_expensive(
        _cost_map, rc.get_global_resources(), rc.get_current_round()
    )
    if repair:
        # Repair-quality: only ore one hop from the accepting network -- adjacent
        # to a route target (a conveyor whose chain already reaches the core) or
        # the core itself -- so a harvester built here feeds the network at once.
        # These claims score at the higher repair tier.
        valid_route_targets = map_info._bm_route_targets | map_info._bm_my_core_area
        available &= map_info.expand_manhattan(valid_route_targets)
    if units.builder._stay_near_core:
        available &= units.builder.near_core_mask()
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, available, tie_self=False)

def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav

# Ore this builder could not route to. It used to be a PERMANENT blacklist (set
# once, never cleared), so a tile written off in round 30 stayed dead all game --
# but every reason for writing one off is temporary (an enemy turret that dies, a
# teammate in the corridor, titanium not yet banked). Expire it on a retry clock.
CANT_HARVEST_RETRY_ROUNDS = 16
cant_harvest = 0
_cant_harvest_stamp = -1
_cost_map: dict[int, tuple[int, int]] = {}  # tile index -> (min titanium cost, round recorded)


def _expire_cant_harvest():
    global cant_harvest, _cant_harvest_stamp
    now = rc.get_current_round()
    if now - _cant_harvest_stamp >= CANT_HARVEST_RETRY_ROUNDS:
        cant_harvest = 0
        _cant_harvest_stamp = now


def possible_ore():
    w = map_info._width
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]

    my_team_idx = map_info._my_team_idx

    # Friendly buildings that block harvesting (all but conveyor/barrier/harvester)
    friendly_blocking = (
        map_info._bm_team[my_team_idx]
        & ~map_info._bm_et[map_info._IDX_CONVEYOR]
        & ~map_info._bm_et[map_info._IDX_BARRIER]
        & ~map_info._bm_et[map_info._IDX_HARVESTER]
    )

    landlocking = ore | ~map_info._bm_seen&map_info._board_mask
    landlocked = landlocking & (landlocking >> 1 & map_info._not_right_col) & (landlocking << 1 & map_info._not_left_col) & (landlocking >> w) & (landlocking << w)


    return (ore
            & ~landlocked
            & ~map_info._bm_team[1 - my_team_idx]  # any enemy building blocks harvesting
            & ~friendly_blocking
            & ~map_info._bm_enemy_turret_threat)
def harvestable_ore():
    ore = possible_ore()
    # units.builder.draw_mask(ore, 255, 0, 0)
    # units.builder.draw_mask(secured(), 0, 255, 0)
    # units.builder.draw_mask(cant_harvest, 0, 0, 255)
    # Any ore we can still act on: not already harvested, not known-unroutable.
    # Harvest targets ore even inside the core's own vision.
    return (ore
            & ~map_info._bm_et[map_info._IDX_HARVESTER]
            & ~cant_harvest)

def _find_harvest_target(repair=False):
    """First reachable+affordable ore as (ore, path, ore_bit), else None. Done in
    score() so harvest isn't selected when nothing can be routed/afforded. With
    repair=True, only ore one hop from the network is considered (higher tier)."""
    global cant_harvest
    _expire_cant_harvest()
    available = _my_claims(repair)
    if not available:
        return None
    w = map_info._width
    harvester_cost = rc.get_harvester_cost()
    ti = rc.get_global_resources()
    while available:
        candidate, _ = nav.closest(available)
        if candidate is None:
            # Nothing reachable THIS turn -- a fact about the corridor right now
            # (usually a teammate standing in it), not about the ore. Must NOT
            # write off every remaining tile at once.
            return None
        cand_n = candidate.x + candidate.y * w
        cand_bit = 1 << cand_n
        cand_path = nav.calculate_conveyor_path(candidate)
        if cand_path is None:
            cant_harvest |= cand_bit
            available &= ~cand_bit
            continue
        cost = harvester_cost + nav.conveyor_cost(min(cand_path[2], PAYG_HORIZON), rc.get_scale_percent()/100+0.05)
        _cost_map[cand_n] = (cost, rc.get_current_round())
        if cost > ti:
            available &= ~cand_bit
            continue
        return (candidate, cand_path, cand_bit)
    return None


# Folds in the former harvest_repair state: a repair-quality target (ore one hop
# from the network) scores REPAIR_SCORE, plain harvestable ore scores NORMAL_SCORE.
# MAX_SCORE is the higher of the two so the selection loop's early-break is correct.
NORMAL_SCORE = 4
REPAIR_SCORE = 7
MAX_SCORE = 7
_cached_target = None
def score(can_move=True):
    global _cached_target
    # Prefer a repair-quality target -- ore one hop from the accepting network --
    # which scores at the higher repair tier. Otherwise fall back to any
    # harvestable ore at the plain tier.
    _cached_target = _find_harvest_target(repair=True)
    repair = _cached_target is not None
    if _cached_target is None:
        _cached_target = _find_harvest_target(repair=False)
    if _cached_target is None:
        return 0
    if not can_move:
        # In-place retry: only worth it if the ore is already cardinally adjacent.
        best_ore = _cached_target[0]
        my = map_info._my_pos
        if abs(best_ore.x - my.x) + abs(best_ore.y - my.y) != 1:
            _cached_target = None
            return 0
    return REPAIR_SCORE if repair else NORMAL_SCORE


def run(can_move=True):
    target = _cached_target
    if target is None:
        return
    log("HARVEST")
    best_ore, path, ore_bit = target
    w = map_info._width
    my_team_idx = map_info._my_team_idx

    # Move into position first. bfs_move keeps us put when we're already adjacent
    # and safe (so the destroy/build below runs), but if our tile is now lethal it
    # steps us off it -- we flee instead of standing there to harvest and dying.
    if nav.move_adjacent(best_ore, can_move=can_move):
        return

    # A team bit is only set alongside a building, so this implies one stands here.
    if map_info._bm_team[my_team_idx] & ore_bit and rc.can_destroy(best_ore) and has_op():
        rc.destroy(best_ore)
        map_info.update_at(best_ore)

    if rc.can_build_harvester(best_ore) and rc.get_global_resources() >= rc.get_harvester_cost() + map_info.ti_reserve():
        p0 = path[0]
        # Make sure we don't block ourselves off from the start of the path
        _, reach = nav.closest(1 << (p0.x + p0.y * w),
                               avoid=map_info.get_avoid(False) | ore_bit,
                               side=False)
        if reach != -1:
            log("harvest: building at", best_ore)
            rc.build_harvester(best_ore)
            map_info.update_at(best_ore)
            # A harvester built cardinally next to the core feeds it directly, so
            # this completes a route -- tally it. (Plain harvest only; harvest_repair
            # does NOT count.)
            if map_info.manhattan(1 << (best_ore.x + best_ore.y * w)) & map_info._bm_my_core_area:
                comms.note_route_complete()
            return
        log("harvest: wrong side of", best_ore, "- crossing to", p0)
        nav.move_to(p0, can_move=can_move)
        return

    nav.move_adjacent(best_ore, can_move=can_move)
