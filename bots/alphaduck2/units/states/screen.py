"""Builder state: plug an enemy gunner's muzzle with a barrier.

A gunner fires a single ray that is absorbed by the first occupied tile, so one
barrier on the tile it faces switches it off entirely -- everything behind is
safe until they break the barrier or pay 10 Ti to rotate.

The trade is the point. A barrier is 3 Ti base and 30 HP; GUNNER_DAMAGE is 7, so
five shots are needed to clear it. Each shot costs GUNNER_AMMO_COST (4) ammo,
which the enemy core bought 1:1 with titanium -- 20 Ti of their ammo against 3 Ti
of our barrier, and we can rebuild it for 3 more. That is why we do NOT heal
these barriers: see map_info.screen_barriers(). Letting one die and replacing it
is strictly cheaper than topping it off at 1 Ti per 4 HP, and every turn a
builder spends healing it is a turn not spent on the economy.

Sentinels are not screenable (their line ignores obstacles), so only gunners are
considered.

Ranks above route-repair (8) and harvest (7) but below heal's rescue tier (8.75)
and attack (9): worth interrupting economy work, not worth losing a building
that is actively being killed.
"""
import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import Controller
from log import log

rc: Controller = None
nav: Pathing = None

# Don't cross the map for this. Screening pays because it is cheap and local; a
# builder walking eight tiles to place 3 Ti of barrier is the same mistake the
# disrupt state's comment documents (raising distant barrier work above routing
# cost 4.6 points across the suite).
MAX_SCREEN_DIST = 6

MAX_SCORE = 8.5
_cached_target = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _my_claims() -> int:
    sites = map_info.screen_barrier_sites()
    if not sites:
        return 0
    w = map_info._width
    my_mask = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, sites,
                                tie_self=True)


def _affordable() -> bool:
    return rc.get_global_resources() >= rc.get_barrier_cost() + map_info.ti_reserve()


def score(can_move=True):
    global _cached_target
    _cached_target = None
    if not _affordable():
        return 0
    claims = _my_claims()
    if not claims:
        return 0
    if not can_move:
        # In-place retry: only a muzzle we can already reach from this tile.
        my = map_info._my_pos
        claims &= map_info.manhattan(1 << (my.x + my.y * map_info._width))
        if not claims:
            return 0
        best, _ = nav.closest(claims)
    else:
        best, _ = nav.closest_within(claims, max_dist=MAX_SCREEN_DIST)
    if best is None:
        return 0
    _cached_target = best
    return MAX_SCORE


def run(can_move=True):
    target = _cached_target
    if target is None:
        return
    log("SCREEN", target)
    # Move into position first -- a no-op when we're already adjacent and safe,
    # a forced step when our tile is lethal. Only build if we didn't move.
    if nav.move_adjacent(target, can_move=can_move):
        return
    if rc.can_build_barrier(target) and _affordable():
        rc.build_barrier(target)
        map_info.update_at(target)
