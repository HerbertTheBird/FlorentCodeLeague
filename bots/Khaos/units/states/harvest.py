from main import has_op
import map_info
import pathing
from pathing import Pathing
from fcode import *
import units.builder
from log import log
import sys
rc: Controller = None
nav: Pathing = None

def _my_claims():
    my_pos = map_info._my_pos
    w = map_info._width
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    available = harvestable_ore() & ~_too_expensive()
    if units.builder._stay_near_core:
        available &= units.builder.near_core_mask()
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, available, tie_self=False)

def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav

cant_harvest = 0
_cost_map: dict[int, tuple[int, int]] = {}  # tile index -> (min titanium cost, round recorded)
COST_MAP_TTL = 100
def possible_ore():
    w = map_info._width
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]

    my_team_idx = map_info._my_team_idx
    enemy_idx = 1 - my_team_idx

    # Enemy buildings that block harvesting (not road/conveyor/bridge/splitter/marker)
    enemy_blocking = (
        map_info._bm_team[enemy_idx]
    )
    # Friendly buildings that block harvesting (not road/barrier/marker)
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
            & ~enemy_blocking
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
        if cost > ti:
            result |= 1 << n
    for n in stale:
        del _cost_map[n]
    return result

MAX_SCORE = 4
_cached_claims = 0
def score():
    global _cached_claims
    _cached_claims = _my_claims()
    return 4 if _cached_claims else 0

CARD = [Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST]


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
        cost = rc.get_harvester_cost() + nav.conveyor_cost(cand_path[2], rc.get_scale_percent()/100+0.05)
        _cost_map[cand_n] = (cost, rc.get_current_round())
        if cost > rc.get_global_resources():
            available &= ~cand_bit
            log("too expensive", candidate, "— retrying")
            continue
        best_ore = candidate
        path = cand_path
        break

    if best_ore is None:
        return

    ore_n = best_ore.x + best_ore.y * w
    ore_bit = 1 << ore_n
    ore_id = map_info._building_id[ore_n]

    if ore_id:
        is_mine = bool(map_info._bm_team[my_team_idx] & ore_bit)
        if is_mine and rc.can_destroy(best_ore) and has_op():
            rc.destroy(best_ore)
            map_info.update_at(best_ore)
    targets = set()
    log(path[0])
    for d in Direction:
        p = map_info.pos_add(path[0], d)
        if p == best_ore or not map_info.in_bounds(p):
            continue
        if p.distance_squared(best_ore) > 1:
            continue
        if map_info.is_passable(p):
            targets.add(p)
    if targets:
        nav.move_to(targets)
    log("targets", targets, path[0])
    # Move to any adjacent tile and build harvester
    if rc.can_build_harvester(best_ore) and rc.get_global_resources() >= rc.get_harvester_cost() + map_info.ti_reserve():
        rc.build_harvester(best_ore)
        map_info.update_at(best_ore)
