"""Builder state: last-resort unfreeze -- if we have not moved in N turns, move (#38).

FOUND BY tools/anomaly.py. Builders idle for 900+ CONSECUTIVE turns of a 1000-turn
game, in the SHIPPED bot, on both teams. Whole-game idle rates of 39-57% of builder
unit-turns with no move and no action.

IT IS NOT ENTRAPMENT. Traced on vase (game 42, turn 300):
    unit 3  (team0) at (8,6): (8,5) enemy builder, (9,6) enemy CONVEYOR,
                              (8,7) WALL, (7,6) WALL
    unit 37 (team1) at (8,5): (8,4) own barrier,   (9,5) own CONVEYOR,
                              (8,6) enemy builder, (7,5) WALL
Both had a legal escape the whole time -- a builder may stand on EITHER team's
conveyors, and _bm_blocked already treats them as passable. Neither moved. The
problem is that NO STATE WANTED THE TURN: explore returns 0 when it cannot find a
reachable target, everything else scores 0, and the builder simply stands there.

So the fix is not to break out of a box, it is to refuse to stand still forever.
Scored just above explore (1) so it never displaces real work -- it only fires when
the alternative is another wasted turn.
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


MAX_SCORE = 1.5          # just above explore (1); below every real state
IDLE_LIMIT = 30

_last = {}               # unit id -> (x, y, consecutive turns there)
_target = None


def score(can_move=True):
    global _target
    _target = None
    if not can_move:
        return 0
    uid = rc.get_id()
    my = map_info._my_pos
    px, py, n = _last.get(uid, (None, None, 0))
    if px == my.x and py == my.y:
        n += 1
    else:
        n = 0
    _last[uid] = (my.x, my.y, n)
    if n < IDLE_LIMIT:
        return 0
    # Any legal step will do -- the point is to stop being stuck, and the normal
    # states take over again the moment we are somewhere new.
    walk = map_info.passable()
    w = map_info._width
    best = None
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        p = Position(my.x + dx, my.y + dy)
        if not map_info.in_bounds(p):
            continue
        if not ((walk >> (p.x + p.y * w)) & 1):
            continue
        # Prefer stepping AWAY from our own core so we spread out rather than
        # oscillating in the same pocket.
        core = map_info._bm_my_core_area
        d = 0
        if core:
            n0 = (core & -core).bit_length() - 1
            d = abs(p.x - n0 % w) + abs(p.y - n0 // w)
        if best is None or d > best[0]:
            best = (d, p)
    if best is None:
        return 0
    _target = best[1]
    return MAX_SCORE


def run(can_move=True):
    if _target is None:
        return
    log("UNFREEZE", _target)
    _last[rc.get_id()] = (_target.x, _target.y, 0)
    nav.move_to(_target, can_move=can_move)
