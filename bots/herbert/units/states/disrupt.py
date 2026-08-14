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
    all_ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    return (all_ore
            & (~map_info._bm_any_building)
            & ~units.builder._harvest_zone
            & ~map_info._bm_enemy_turret_threat)

def _my_claims():
    w = map_info._width
    my_mask = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    candidates = _disruptable_ore()
    if units.builder._stay_near_core:
        candidates &= units.builder.near_core_mask()
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, candidates, tie_self=True)

# Denying the enemy an ore tile is the good side of the barrier trade — 3 Ti to
# cost them a harvester site — so raising this above route (5) and harvest (4)
# looked free. It is not: at 6 it won the head-to-head against this version
# 40-26, but lost 4.6 points across the real suite (61.4% -> 56.8%, and 12
# against Khaos). Builders pulled off routing to go wall distant ore are not
# paying for themselves. Left at 2, where it only fires with nothing else to do.
MAX_SCORE = 2
_cached_target = None
def score():
    global _cached_target
    _cached_target = None
    claims = _my_claims()
    if claims:
        best, _ = nav.closest(claims)       # nearest reachable target
        if best is not None:
            _cached_target = best
    return MAX_SCORE if _cached_target is not None else 0

def run():
    best = _cached_target
    if best is None:
        return
    log("DISRUPT")

    if rc.can_build_barrier(best) and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.ti_reserve():
        rc.build_barrier(best)
        map_info.update_at(best)
        return

    nav.move_adjacent(best)
