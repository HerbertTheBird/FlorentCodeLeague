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
from log import log
rc: Controller = None
nav: Pathing = None

def _my_claims():
    my_pos = map_info._my_pos
    w = map_info._width
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    available = harvestable_ore() & ~payg.too_expensive(
        _cost_map, rc.get_global_resources(), rc.get_current_round()
    )
    if units.builder._stay_near_core:
        available &= units.builder.near_core_mask()
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, available, tie_self=False)

def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav

cant_harvest = 0
_cost_map: dict[int, tuple[int, int]] = {}  # tile index -> (min titanium cost, round recorded)
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
    return (ore
            & ~map_info._bm_et[map_info._IDX_HARVESTER]
            & ~cant_harvest)

MAX_SCORE = 4
_cached_claims = 0
def score():
    global _cached_claims
    _cached_claims = _my_claims()
    return MAX_SCORE if _cached_claims else 0


def run():
    global cant_harvest
    log("HARVEST")

    available = _cached_claims
    if not available:
        return

    w = map_info._width
    my_team_idx = map_info._my_team_idx

    best_ore = None
    path = None
    ore_bit = 0
    # Deliberately re-read after the destroy below, which does move the cost scale.
    harvester_cost = rc.get_harvester_cost()
    ti = rc.get_global_resources()
    while available:
        candidate, _ = nav.closest(available)
        log("harvesting", candidate)
        if candidate is None:
            cant_harvest |= available
            return
        cand_n = candidate.x + candidate.y * w
        cand_bit = 1 << cand_n
        cand_path = nav.calculate_conveyor_path(candidate)
        if cand_path is None:
            cant_harvest |= cand_bit
            available &= ~cand_bit
            log("cant route", candidate, "— retrying")
            continue
        cost = harvester_cost + nav.conveyor_cost(min(cand_path[2], PAYG_HORIZON), rc.get_scale_percent()/100+0.05)
        _cost_map[cand_n] = (cost, rc.get_current_round())
        if cost > ti:
            available &= ~cand_bit
            log("too expensive", candidate, "— retrying")
            continue
        best_ore = candidate
        path = cand_path
        ore_bit = cand_bit
        break

    if best_ore is None:
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
            return
        log("harvest: wrong side of", best_ore, "- crossing to", p0)
        nav.move_to(p0)
        return

    nav.move_adjacent(best_ore)
