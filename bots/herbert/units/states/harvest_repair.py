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
    # harvest_repair: only ore that is immediately connected to a valid route
    # target -- cardinally adjacent to a conveyor whose chain already reaches the
    # core (or the core itself) -- so a harvester built here feeds the network at
    # once. Runs above plain harvest but below route_repair.
    valid_route_targets = map_info._bm_route_targets | map_info._bm_my_core_area
    available &= map_info.expand_manhattan(valid_route_targets)
    if units.builder._stay_near_core:
        available &= units.builder.near_core_mask()
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, available, tie_self=False)

def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav

cant_harvest = 0
_cost_map: dict[int, tuple[int, int]] = {}  # tile index -> (min titanium cost, round recorded)

_vision_cache = (None, 0)   # (core Position, vision mask) -- geometry only


def _core_vision_mask() -> int:
    """Tiles the core actually sees. The core is 2x2 and its vision is measured to
    the NEAREST of its four cells (r^2=36), so from the core's CENTRE the reach is
    42.5, not 36 -- and on the integer grid that union of four cell-disks is
    exactly a single r^2=42.5 disk about the centre (verified tile-for-tile vs the
    engine's is_in_vision). `_my_core` is the top-left corner, so the centre is
    +(0.5, 0.5). Pure geometry, cached on the core."""
    global _vision_cache
    core = map_info._my_core
    if core is None:
        return 0
    if _vision_cache[0] == core:
        return _vision_cache[1]
    w, h = map_info._width, map_info._height
    r2 = 42.5
    cx, cy = core.x + 0.5, core.y + 0.5
    vis = 0
    for y in range(h):
        dy2 = (y - cy) * (y - cy)
        if dy2 > r2:
            continue
        for x in range(w):
            if (x - cx) * (x - cx) + dy2 <= r2:
                vis |= 1 << (x + y * w)
    _vision_cache = (core, vis)
    return vis


def _core_vision_reach() -> int:
    """Tiles a cardinal BFS from the core reaches while treating everything
    outside the core's view (and walls) as impassable -- the ore the core can
    already see a straight conveyor run to. We keep harvesters off these and leave
    that ore to the core's own connected network. Recomputed live so it tracks
    revealed walls; the BFS is cheap (bitset flood over ~a vision disk)."""
    vis = _core_vision_mask()
    if not vis:
        return 0
    passable = vis & ~map_info._bm_env[map_info._IDX_ENV_WALL]
    reach = map_info._bm_my_core_area
    frontier = reach
    while frontier:
        nxt = map_info.expand_manhattan(frontier) & passable & ~reach
        if not nxt:
            break
        reach |= nxt
        frontier = nxt
    return reach


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
    # Repair may re-place a harvester on ore inside core vision too -- a killed
    # feeding harvester can be anywhere -- so, unlike plain harvest, do NOT
    # subtract _core_vision_reach() here. (Still skip ore that already has a
    # harvester or is known unroutable.)
    return (ore
            & ~map_info._bm_et[map_info._IDX_HARVESTER]
            & ~cant_harvest)

def _find_harvest_target():
    """First reachable+affordable ore as (ore, path, ore_bit), else None -- done
    in score() so the state isn't selected when nothing can be routed/afforded."""
    global cant_harvest
    available = _my_claims()
    if not available:
        return None
    w = map_info._width
    harvester_cost = rc.get_harvester_cost()
    ti = rc.get_global_resources()
    while available:
        candidate, _ = nav.closest(available)
        if candidate is None:
            cant_harvest |= available
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


MAX_SCORE = 7
_cached_target = None
def score():
    global _cached_target
    _cached_target = _find_harvest_target()
    return MAX_SCORE if _cached_target is not None else 0


def run():
    target = _cached_target
    if target is None:
        return
    log("HARVEST")
    best_ore, path, ore_bit = target
    w = map_info._width
    my_team_idx = map_info._my_team_idx

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
