"""Builder state: chase enemy builders that have entered our harvest zone.

Enemy builders raiding our half come to chip our conveyors and harvesters. We
can't damage them (builders only heal), but following the nearest raider keeps a
builder next to it -- so the free per-turn _do_best_heal() repairs whatever it
attacks, and we contest the ground. Enemies are Voronoi-partitioned across our
builders like every other target, so we spread out over multiple raiders.

Ranks above harvest and below route: worth interrupting a harvest run to defend
the economy, but not our active routing.
"""
import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import Controller
from log import log

rc: Controller = None
nav: Pathing = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _my_claims() -> int:
    zone = units.builder._harvest_zone
    if not zone:
        return 0
    enemies = map_info._bm_enemy_bots & zone
    if not enemies:
        return 0
    w = map_info._width
    my_pos = map_info._my_pos
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, enemies, tie_self=False)


MAX_SCORE = 4.5
_cached_target = None


def score():
    global _cached_target
    _cached_target = None
    claims = _my_claims()
    if claims:
        target, _ = nav.closest(claims)     # nearest reachable raider
        if target is not None:
            _cached_target = target
    return MAX_SCORE if _cached_target is not None else 0


def run():
    if _cached_target is None:
        return
    log("CHASE", _cached_target)
    nav.move_to(_cached_target)
