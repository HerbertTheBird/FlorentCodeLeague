import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import *
from log import log

rc: Controller = None
nav: Pathing = None

def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav

def _disruptable_ore():
    return (map_info._bm_env[map_info._IDX_ENV_ORE_TI]
            & ~map_info._bm_any_building
            & ~units.builder._harvest_zone
            & ~map_info._bm_enemy_turret_threat
            & ~map_info._bm_enemy_launch_adj)

def _my_claims():
    w = map_info._width
    my_mask = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    candidates = _disruptable_ore()
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, candidates, tie_self=True)

MAX_SCORE = 2
_cached_claims = 0
def score():
    global _cached_claims
    if rc.get_global_resources() < rc.get_harvester_cost()*5:
        _cached_claims = 0
        return 0
    _cached_claims = _my_claims()
    return 2 if _cached_claims else 0

def run():
    log("DISRUPT")
    available = _cached_claims
    if not available:
        return

    best, _ = nav.closest(available)
    if best is None:
        return

    width = map_info._width

    best_n = best.x + best.y * width
    best_id = map_info._building_id[best_n]
    best_bit = 1 << best_n
    my_team_idx = map_info._my_team_idx

    nav.move_adjacent(best)
    if best_id and (map_info._bm_team[my_team_idx] & best_bit):
        if rc.can_destroy(best) and rc.get_action_cooldown() == 0:
            rc.destroy(best)
            map_info.update_at(best)

    if rc.can_build_barrier(best) and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.builder_ti_reserve():
        rc.build_barrier(best)
        map_info.update_at(best)
