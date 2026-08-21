"""Builder state: body-block enemy builders walking at our buildings (#29 F2).

Movement is 4-WAY and bots collide, so a body on the tile an attacker needs is a
real wall, not a suggestion. This is the cheapest of the six blocking situations
because our builders SPAWN on the core ring (CORE_SPAWNING_RADIUS_SQ=2 makes the
12-tile ring exactly the spawn area), so they are already standing where the
defending has to happen -- none of the travel cost that sank the three
travelling-barrier plays (+0.0012, -4.6, +0.0385).

HOW THE PIN WORKS. Under 4-way movement a runner heading for a POINT has at most
two productive moves (one that decreases dx, one that decreases dy) and exactly
one once it is axis-aligned. We compute BFS bands outward from what we are
protecting; an enemy sitting in band d can only close the distance by stepping
into band d-1. Those tiles are the cut. Standing on them is the block, and
because everyone moves at the same rate the pin is maintainable -- if the
attacker sidesteps, the band structure moves with it and we re-derive next turn.

WHY BODIES AND NOT BARRIERS HERE. A barrier is 3 Ti once and permanent, and for
any tile we want denied indefinitely it dominates -- that is what block/disrupt/
siege already do. A body is free but costs a builder-turn every turn, so it is
right only when we need the cut RIGHT NOW and cannot afford or cannot wait for a
barrier. Defending against a builder already walking at us is exactly that case:
the bank is routinely at 3-4 Ti, and a barrier placed after they arrive is late.

WHAT THIS DOES NOT DO. It does not chase. An attacker already adjacent to our
buildings (band 0) is past blocking -- heal/attack/chase handle that. We only
engage while there is still distance to deny.
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


# Sits between harvest (7) and siege (6.0) by default: defending an existing
# harvester beats starting a new economy project, but never outranks route (8),
# attack (9), heal (9.5) or block (10).
MAX_SCORE = 7.5
# Only consider attackers this many steps out. Beyond it they may not be coming
# for us at all, and a body spent on a guess is a builder-turn burned.
PIN_RADIUS = 6
# Never park on our own spawn ring: it IS the spawn area, so our own bodies there
# can block builder production. Ablated by the _RING variant.
AVOID_SPAWN_RING = True

_cached_target = None
_BAND_ROUND = -1
_BANDS = ()


def _protected() -> int:
    """What is worth body-blocking for: our core and our harvesters.

    Deliberately NOT every conveyor -- conveyors are numerous and cheap, and
    including them would make almost every enemy bot look like an attacker.
    """
    mine = map_info._bm_team[map_info._my_team_idx]
    return map_info._bm_my_core_area | (map_info._bm_et[map_info._IDX_HARVESTER] & mine)


def _bands():
    """BFS bands outward from the protected set, over passable tiles only.

    bands[d] = tiles exactly d steps from anything protected. Walls are respected
    because each dilation is intersected with passable(), so a corridor collapses
    the cut to one tile automatically -- which is precisely where blocking is
    cheapest and most effective.
    """
    global _BAND_ROUND, _BANDS
    r = rc.get_current_round()
    if r == _BAND_ROUND:
        return _BANDS
    prot = _protected()
    _BAND_ROUND = r
    if not prot:
        _BANDS = ()
        return _BANDS
    walk = map_info.passable()
    bands = [prot]
    seen = prot
    for _ in range(PIN_RADIUS):
        nxt = map_info.manhattan(seen) & ~seen & walk
        if not nxt:
            break
        bands.append(nxt)
        seen |= nxt
    _BANDS = tuple(bands)
    return _BANDS


def _cut_tiles():
    """Tiles that attackers must step through to close on our buildings."""
    bands = _bands()
    if len(bands) < 2:
        return 0
    enemies = map_info._bm_enemy_bots
    if not enemies:
        return 0
    w = map_info._width
    cut = 0
    # Band 0 is already-adjacent: past blocking. Start at 1.
    for d in range(1, len(bands)):
        here = enemies & bands[d]
        if not here:
            continue
        inner = bands[d - 1]
        m = here
        while m:
            b = m & -m
            m ^= b
            # The enemy's 4-neighbours that actually close the distance.
            cut |= map_info.manhattan(b) & ~b & inner
    return cut


def score(can_move=True):
    global _cached_target
    _cached_target = None
    cut = _cut_tiles()
    if not cut:
        return 0
    # Never stand in turret fire to hold a tile, and never on a tile we cannot
    # legally occupy.
    cut &= map_info.passable()
    cut &= ~map_info._bm_enemy_turret_threat
    if AVOID_SPAWN_RING:
        core = map_info._bm_my_core_area
        cut &= ~(map_info.manhattan(core) & ~core)
    if not cut:
        return 0
    claims = pathing.claim_subset(
        1 << (map_info._my_pos.x + map_info._my_pos.y * map_info._width),
        map_info._bm_friendly_bots, cut, tie_self=True)
    if not claims:
        return 0
    my = map_info._my_pos
    if (claims >> (my.x + my.y * map_info._width)) & 1:
        _cached_target = my            # already holding the cut: stand still
        return MAX_SCORE
    if not can_move:
        return 0
    target, dist = nav.closest(claims, to_adjacent=False)
    if target is None:
        return 0
    # Only commit if we can get there before they walk through it.
    if dist > 1:
        return 0
    _cached_target = target
    return MAX_SCORE


def run(can_move=True):
    target = _cached_target
    if target is None:
        return
    log("PIN", target)
    my = map_info._my_pos
    if target.x != my.x or target.y != my.y:
        nav.move_to(target, can_move=can_move)
        return
    # HOLDING. Moving and acting are mutually exclusive -- the engine requires
    # BOTH cooldowns at 0 for either, so a bot that moves cannot act and a bot
    # that stands still has its whole action free. Standing still is the block,
    # so the action is pure profit: chip whoever we are pinning. 2 damage a turn
    # for 2 Ti, from a tile we were going to occupy regardless.
    if rc.get_global_resources() < GameConstants.BUILDER_BOT_ATTACK_COST:
        return
    adj = map_info.manhattan(1 << (my.x + my.y * map_info._width))
    m = adj & map_info._bm_enemy_bots
    w = map_info._width
    while m:
        b = m & -m
        n = b.bit_length() - 1
        m ^= b
        p = Position(n % w, n // w)
        if rc.can_fire(p):
            rc.fire(p)
            return
