"""Builder state: pinwheel gunners on the enemy core's ring corners (#40).

Tyr's geometry. A gunner fires in a straight line and is stopped by the first
thing it hits. From a CORNER of the twelve-tile ring, facing ALONG a core edge,
its line runs down the two edge tiles of that side and then reaches the next
corner -- and the core itself is never on that line, so it CANNOT waste ammunition
on 500 HP of core by accident:

    C E E C        NW -> EAST     covers the two north edges
    E X X E        NE -> SOUTH    covers the two east edges
    E X X E        SE -> WEST     covers the two south edges
    C E E C        SW -> NORTH    covers the two west edges

Every edge tile is then watched by exactly one gunner, and each ray terminates on
the next corner, where our own gunner stands -- a friendly building, which blocks
it harmlessly.

WHY THIS AND NOT JUST BARRIERS. A barrier on a tile a gunner IS watching is worse
than nothing: it denies the enemy one tile but blinds the gunner to everything
behind it, and a gunner that keeps firing beats a 30 HP wall. So barriers are for
the tiles no gunner covers, and this state takes the corners first.

WHY IT MATTERS THAT THE CORE IS OFF THE RAY: measured on plain herbert14, 28% of
all turret shots go into the enemy core (86 of 308 in a midgard pair) -- damage
against a 500 HP pool we can never finish. The corner facing makes that impossible
by construction rather than by tuning a weight table.
"""
import map_info
import units.builder
from fcode import *
from log import log

rc: Controller = None
nav = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


# Above siege (6.0): if we are close enough to place one of these, it beats
# scattering barriers. Below attack (9) / heal (9.5) / block (10).
MAX_SCORE = 7.2
# Only bother once we are actually at the enemy core.
ENGAGE_DIST = 8

_RG=[0,0,0,0]
_cached = None      # (Position, Direction)


def _their_core():
    seen = map_info._bm_their_core_area
    if seen:
        return seen
    try:
        import units.states.relay as relay
        return relay.their_core_area()
    except Exception:
        return 0


def _corners():
    """[(corner tile, facing)] -- the pinwheel, in ring order."""
    core = _their_core()
    if not core:
        return []
    w = map_info._width
    xs = []
    ys = []
    m = core
    while m:
        b = m & -m
        n = b.bit_length() - 1
        m ^= b
        xs.append(n % w)
        ys.append(n // w)
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return [
        (Position(x0 - 1, y0 - 1), Direction.EAST),    # NW -> along the north edge
        (Position(x1 + 1, y0 - 1), Direction.SOUTH),   # NE -> along the east edge
        (Position(x1 + 1, y1 + 1), Direction.WEST),    # SE -> along the south edge
        (Position(x0 - 1, y1 + 1), Direction.NORTH),   # SW -> along the west edge
    ]


def score(can_move=True):
    global _cached
    _cached = None
    _RG[0]+=1
    core = _their_core()
    if not core:
        return 0
    _RG[1]+=1
    my = map_info._my_pos
    w = map_info._width
    # Distance to the core, cheaply: Chebyshev to any core tile.
    best = 999
    m = core
    while m:
        b = m & -m
        n = b.bit_length() - 1
        m ^= b
        d = max(abs(n % w - my.x), abs(n // w - my.y))
        if d < best:
            best = d
    if best > ENGAGE_DIST:
        return 0
    if rc.get_global_resources() < rc.get_gunner_cost() + map_info.ti_reserve():
        return 0
    for pos, facing in _corners():
        if not map_info.in_bounds(pos):
            continue
        bit = 1 << (pos.x + pos.y * w)
        if map_info._bm_any_building & bit:
            continue                      # taken already (possibly by ours)
        _cached = (pos, facing)
        _RG[2]+=1
        return MAX_SCORE
    return 0


def run(can_move=True):
    if _cached is None:
        return
    pos, facing = _cached
    log("RINGGUN", pos, facing)
    if nav.move_adjacent(pos, can_move=can_move):
        return
    if rc.can_build_gunner(pos, facing):
        rc.build_gunner(pos, facing)
        _RG[3]+=1


def rg_report():
    print("RGDIAG uid=%d score=%d have_core=%d CHOSE=%d BUILT=%d" % ((rc.get_id(),)+tuple(_RG)), flush=True)
