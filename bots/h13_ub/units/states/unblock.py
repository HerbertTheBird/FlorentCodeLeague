"""Builder state: break an enemy building that makes our route UNREACHABLE.

MEASURED (h13_obsdiag, 4 games): route.score() bailed with no reachable target on
1561 of 1595 calls (97.9%), and in 1004 of those bails -- 64.3% -- treating enemy
breakable buildings as passable would have opened a path to a route target. That
is ~251 occurrences per game.

WHY IT HAPPENS. map_info._bm_blocked contains every enemy building except
conveyors and splitters, and route plans over passable(). So an enemy barrier in
the way does not make a route EXPENSIVE, it deletes the target from the search
entirely. The planner has no concept of a removable obstacle, so the line is
abandoned -- and the builder then has nothing to do. In local game 12136 an enemy
barrier at (10,3) stood from turn 32 to turn 1000 while our builder #37 sat idle
at (8,5) for 588 CONSECUTIVE turns. Whole-game idle rate was 43% of builder turns.

THE TRADE. A barrier is BARRIER_MAX_HP 30 = 15 builder-turns and 30 Ti at 2 dmg
per 2 Ti. A completed route is worth ~2.5 Ti/round forever, so it repays in about
twelve rounds and then compounds. We have the turns: they were being spent idle.
"""
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


# Below route (8) and harvest (7) -- if a route IS reachable, build it instead of
# demolishing something. This only matters on the turns route has already bailed.
MAX_SCORE = 6.8
# Give up on obstacles further than this; a distant blockage is someone else's.
LEASH = 10
# Only break when at most this many enemy buildings stand between us and a target.
MAX_BREAKS = 2

_cached = None
_CACHE_ROUND = -1
_CACHE_VAL = None


def _flood(start: int, walk: int, steps: int = 60) -> int:
    seen = start
    for _ in range(steps):
        nxt = map_info.manhattan(seen) & walk & ~seen
        if not nxt:
            break
        seen |= nxt
    return seen


def _frontier_obstacles():
    """Enemy breakables adjacent to where we can already reach, that unlock a target.

    Cached per round: two floods per builder-turn would be expensive, and every
    builder on the team wants the same answer.
    """
    global _CACHE_ROUND, _CACHE_VAL
    r = rc.get_current_round()
    if r == _CACHE_ROUND:
        return _CACHE_VAL
    _CACHE_ROUND = r
    _CACHE_VAL = 0
    mine = map_info._bm_team[map_info._my_team_idx]
    breakable = (map_info._bm_blocked & ~mine
                 & ~map_info._bm_my_core_area & ~map_info._bm_their_core_area
                 & ~map_info._bm_env[map_info._IDX_ENV_WALL])
    if not breakable:
        return 0
    targets = map_info._bm_route_targets
    if not targets:
        return 0
    walk = map_info.passable()
    my = map_info._my_pos
    start = 1 << (my.x + my.y * map_info._width)
    strict = _flood(start, walk)
    if strict & targets:
        return 0                       # something is reachable already; not our job
    relaxed = _flood(start, walk | breakable)
    if not (relaxed & targets):
        return 0                       # breaking would not help either
    # The obstacles worth hitting are the ones touching our reachable region.
    _CACHE_VAL = breakable & map_info.manhattan(strict) & ~strict
    return _CACHE_VAL


def score(can_move=True):
    global _cached
    _cached = None
    if rc.get_global_resources() < GameConstants.BUILDER_BOT_ATTACK_COST:
        return 0
    obs = _frontier_obstacles()
    if not obs:
        return 0
    # Never stand in turret fire to demolish something; a builder loses that trade.
    obs &= ~map_info._bm_enemy_turret_threat
    if not obs:
        return 0
    claims = pathing.claim_subset(
        1 << (map_info._my_pos.x + map_info._my_pos.y * map_info._width),
        map_info._bm_friendly_bots, obs, tie_self=True)
    if not claims:
        return 0
    target, dist = nav.closest(claims, to_adjacent=True)
    if target is None or dist > LEASH:
        return 0
    _cached = target
    return MAX_SCORE


def run(can_move=True):
    target = _cached
    if target is None:
        return
    log("UNBLOCK", target)
    if nav.move_adjacent(target, can_move=can_move):
        return
    if rc.can_fire(target) and rc.get_global_resources() >= GameConstants.BUILDER_BOT_ATTACK_COST:
        rc.fire(target)
