"""Builder state: break out when we have sealed ourselves in (#38).

FOUND BY tools/anomaly.py on its first run. Four builders across two vase games
were idle for 926-978 turns of a 1000-turn game -- they stop at turn ~20-75 and
never move or act again. Traced: they wall themselves in WITH THEIR OWN BARRIERS.

    unit 6 (team1) at (1,3):
        (1,2) enemy builder   (2,3) WALL
        (1,4) barrier #65 OURS   (0,3) barrier #59 OURS
    Its turn-66 "build" was barrier #65 -- the second wall of its own prison.

Nothing can free it on its own: a builder cannot damage bots at all, and neither
chip nor attack will ever target a FRIENDLY barrier. So it idles for the rest of
the game. Sampled every 50 turns over six replays, 13.0% of team1 builder-turns
were fully enclosed and every one of them involved our own barrier.

THE ESCAPE IS FREE. destroy() costs no cooldown -- a bot can destroy an adjacent
allied barrier AND still move the same round -- so breaking out costs us nothing
but the 3 Ti barrier, against a builder that is otherwise lost for the game.

Scored above everything: being unable to act at all dominates any other choice.
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


MAX_SCORE = 11.0     # above block (10): nothing matters more than being able to move
# Break out when this many or more of our 4 cardinal neighbours are impassable.
# 4 = fully sealed. 3 is deliberately available as an arm: a builder with one exit
# is nearly as stuck, and the exit may be another builder that never moves.
SEAL_AT = 4

_cached = None


def _neighbours():
    my = map_info._my_pos
    w, h = map_info._width, map_info._height
    out = []
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        x, y = my.x + dx, my.y + dy
        if 0 <= x < w and 0 <= y < h:
            out.append(Position(x, y))
    return out


def score(can_move=True):
    """Fire only when we are actually sealed and can do something about it."""
    global _cached
    _cached = None
    my = map_info._my_pos
    w = map_info._width
    walk = map_info.passable()
    mine = map_info._bm_team[map_info._my_team_idx]
    barriers = map_info._bm_et[map_info._IDX_BARRIER] & mine
    blocked = 0
    escape = None
    n = _neighbours()
    # Off-board sides count as blocked: a corner pocket is as sealed as a walled one.
    blocked += 4 - len(n)
    for p in n:
        bit = 1 << (p.x + p.y * w)
        if walk & bit:
            continue
        blocked += 1
        if (barriers & bit) and escape is None:
            escape = p
    if blocked < SEAL_AT or escape is None:
        return 0
    _cached = escape
    return MAX_SCORE


def run(can_move=True):
    target = _cached
    if target is None:
        return
    log("UNSTICK", target)
    if rc.can_destroy(target):
        rc.destroy(target)
        # destroy() is cooldown-free, so we can leave through the hole immediately.
        if can_move:
            nav.move_to(target, can_move=can_move)
