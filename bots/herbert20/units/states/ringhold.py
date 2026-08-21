"""Builder state: ETERNAL SIEGE -- the siege bot completes the enemy ring (#42b).

MEASURED FAILURE: after arriving, the siege bot's state histogram was
    heal 171, chase 81, route 41, relay 18, attack 15
...all of it back at OUR OWN core. It crossed the map, ran out of unguarded ring
conveyors, and then drifted home as an ordinary builder. siege.py DOES barrier the
ring, but it scores 6.0 against heal 9.5 and route 8, so it never wins a turn.

THE RING IS THE WHOLE POINT. A 2x2 core has exactly twelve tiles at chebyshev 1,
and CORE_SPAWNING_RADIUS_SQ = 2 makes that same set its ENTIRE spawn ring -- it is
also the only place a conveyor can stand and deliver into the core. Seal all twelve
and the enemy core can neither spawn a builder nor be paid. It is not a siege, it
is a shutdown.

So for a builder that is ALREADY THERE, ring work outranks everything: a barrier on
an open ring tile is 3 Ti for a permanent denial, and going home to heal a conveyor
is worth a fraction of that. Ordinary builders are unaffected -- this only applies
to the designated siege bot (id <= SIEGE_ID_MAX), which is why it cannot drag the
economy around.
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


MAX_SCORE = 9.65         # under ringcut(9.7) -- kill a live supply belt first --
                         # but ABOVE heal(9.5)/route(8) so it never goes home.
SIEGE_ID_MAX = 4
HOLD_DIST = 10           # stay within this of their core; beyond it, walk back

_target = None           # open ring tile to barrier
_walk_to = None          # where to stand when there is nothing to build


def _their_core():
    seen = map_info._bm_their_core_area
    if seen:
        return seen
    try:
        import relaygeom
        c = relaygeom.their_core()
        if c is None:
            return 0
        w = map_info._width
        out = 0
        for dx in (0, 1):
            for dy in (0, 1):
                out |= 1 << ((c[0] + dx) + (c[1] + dy) * w)
        return out
    except Exception:
        return 0


def _ring():
    core = _their_core()
    if not core:
        return 0
    w, h = map_info._width, map_info._height
    out = 0
    m = core
    while m:
        b = m & -m
        n = b.bit_length() - 1
        m ^= b
        x, y = n % w, n // w
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if 0 <= x + dx < w and 0 <= y + dy < h:
                    out |= 1 << ((x + dx) + (y + dy) * w)
    return out & ~core & map_info._board_mask


def _dist_to_core():
    core = _their_core()
    if not core:
        return 999
    w = map_info._width
    my = map_info._my_pos
    best = 999
    m = core
    while m:
        b = m & -m
        n = b.bit_length() - 1
        m ^= b
        d = max(abs(n % w - my.x), abs(n // w - my.y))
        if d < best:
            best = d
    return best


def score(can_move=True):
    global _target, _walk_to
    _target = None
    _walk_to = None
    if rc.get_id() > SIEGE_ID_MAX:
        return 0
    ring = _ring()
    if not ring:
        return 0
    d = _dist_to_core()
    if d > HOLD_DIST:
        return 0                        # still travelling; relay owns this bot
    # Open ring tiles are the prize: every barrier removes a spawn slot AND a
    # delivery tile at once, for 3 Ti, permanently.
    open_ring = ring & ~map_info._bm_any_building & map_info.passable()
    open_ring &= ~map_info._bm_enemy_turret_threat
    if open_ring and rc.get_global_resources() >= rc.get_barrier_cost():
        t, dist = nav.closest(open_ring, to_adjacent=True)
        if t is not None:
            _target = t
            return MAX_SCORE
    # Nothing to build. Only claim the turn if we still need to WALK to the ring;
    # otherwise stand down so chip/attack/block can use it.
    # Measured: returning MAX_SCORE unconditionally made the siege bot idle 68% of
    # its turns -- it outranked every state that had real work and then did nothing.
    if d > 3:
        _walk_to = nav.closest(ring, to_adjacent=True)[0]
        if _walk_to is not None:
            return MAX_SCORE
    return 0


def run(can_move=True):
    if _target is not None:
        my = map_info._my_pos
        if abs(my.x - _target.x) + abs(my.y - _target.y) == 1:
            if rc.can_build_barrier(_target):
                rc.build_barrier(_target)
            return
        nav.move_adjacent(_target, can_move=can_move)
        return
    if _walk_to is not None:
        log("SIEGEHOLD walk", _walk_to)
        nav.move_adjacent(_walk_to, can_move=can_move)
        return
    log("SIEGEHOLD hold")          # in position, nothing to build: hold the ring
