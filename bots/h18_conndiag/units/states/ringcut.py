"""Builder state: cut the enemy core's ring conveyors (#42).

The relay delivers a builder to the enemy core by t12. This is what makes the
trip worth paying for: the twelve tiles at chebyshev 1 of the 2x2 core are BOTH
its entire spawn ring (CORE_SPAWNING_RADIUS_SQ = 2) and the only tiles a conveyor
can deliver into it from. A conveyor standing there is the core's supply line.

THREE BUGS THIS FIXES, all found by reading replays:

1. STEPPING ONTO THE TARGET. `move_adjacent` will path THROUGH a conveyor because
   conveyors are standable. Bot 4 stepped onto (3,4) -- the very tile it was
   attacking -- which makes the attack impossible (you cannot fire on your own
   tile) and hands the enemy the adjacency it needs to heal. Once cardinally
   adjacent we now STOP, and if we cannot afford the shot we WAIT rather than
   wander.

2. TARGET THRASHING. Two ring conveyors that are adjacent to each other made the
   nearest-target choice flip every turn, so the bot oscillated between (3,4) and
   (4,4) from t122 to t393 without ever firing. The target is now STICKY: once
   chosen it is kept until it dies or leaves the ring.

3. ATTACKING WHAT WE CANNOT KILL. 709 ringcut turns and 408 attacks produced ZERO
   dead ring conveyors, because a guarded conveyor is repaired faster than we chip
   it: heal is 4 HP for 1 Ti against our 2 damage for 2 Ti, so one guard beats one
   attacker outright and does it at a quarter of the cost. We now only commit to
   an UNGUARDED target -- the user's "free takedown" -- and leave guarded ones to
   the launcher, which can throw the guard away.
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


MAX_SCORE = 9.7          # above heal(9.5)/route(8): do not get dragged home
ENGAGE_DIST = 8          # ordinary builders only bother when already close
SIEGE_LEASH = 99         # the siege bot (id <= 4) always wants to come back
UNGUARDED_ONLY = True    # only commit to a target no enemy bot can heal
STICKY = True            # keep a target until it dies
LAUNCH_GUARD = False     # build a launcher to throw a guard away
SIEGE_ID_MAX = 4

_CD=[0,0]   # ring conveyors considered, of which connected to THEIR core
_target = None
_launch_site = None
_committed = {}          # unit id -> (x, y) of the target we are finishing


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
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    out |= 1 << (nx + ny * w)
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


def _guarded(bit) -> bool:
    """An enemy bot adjacent to the target repairs it faster than we chip it."""
    return bool(map_info.manhattan(bit) & ~bit & map_info._bm_enemy_bots)


def score(can_move=True):
    global _target, _launch_site
    _target = None
    _launch_site = None
    uid = rc.get_id()
    leash = SIEGE_LEASH if uid <= SIEGE_ID_MAX else ENGAGE_DIST
    if _dist_to_core() > leash:
        return 0
    theirs = map_info._bm_team[1 - map_info._my_team_idx]
    ring = _ring()
    cand = ring & map_info._bm_et[map_info._IDX_CONVEYOR] & theirs
    if not cand:
        _committed.pop(uid, None)
        return 0
    w = map_info._width

    # Stick to the target we are already finishing, while it is still valid.
    if STICKY:
        prev = _committed.get(uid)
        if prev is not None:
            pb = 1 << (prev[0] + prev[1] * w)
            if (cand & pb) and not (UNGUARDED_ONLY and _guarded(pb)):
                _target = Position(prev[0], prev[1])
                return MAX_SCORE
            _committed.pop(uid, None)

    if UNGUARDED_ONLY:
        free = 0
        m = cand
        while m:
            b = m & -m
            m ^= b
            if not _guarded(b):
                free |= b
        if LAUNCH_GUARD and not free:
            # Everything is guarded: place a launcher beside the best target so
            # turret_launcher._try_throw_enemy_away() can remove the guard.
            b = cand & -cand
            n = b.bit_length() - 1
            t = Position(n % w, n // w)
            mine = map_info._bm_team[map_info._my_team_idx]
            if not (map_info.manhattan(b, 2) & map_info._bm_et[map_info._IDX_LAUNCHER] & mine):
                for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                    p = Position(t.x + dx, t.y + dy)
                    if map_info.in_bounds(p) and rc.can_build_launcher(p):
                        _launch_site = p
                        _target = t
                        return MAX_SCORE
            return 0
        cand = free
    if not cand:
        return 0
    cand &= ~map_info._bm_enemy_turret_threat
    if not cand:
        return 0
    # Prefer a carrying belt: observed, then believed, then anything.
    for pool in (cand & map_info._bm_conv_ti, cand & map_info._bm_ti_carrying, cand):
        if pool:
            cand = pool
            break
    target, dist = nav.closest(cand, to_adjacent=True)
    if target is None:
        return 0
    _tb = 1 << (target.x + target.y * w)
    _CD[0] += 1
    if _enemy_reaches_core(_tb):
        _CD[1] += 1
    _target = target
    _committed[uid] = (target.x, target.y)
    return MAX_SCORE


def _cardinally_adjacent(a, b) -> bool:
    return abs(a.x - b.x) + abs(a.y - b.y) == 1


def run(can_move=True):
    if _target is None:
        return
    log("RINGCUT", _target)
    my = map_info._my_pos
    if _launch_site is not None:
        if (rc.get_global_resources() >= rc.get_launcher_cost()
                and rc.can_build_launcher(_launch_site)):
            rc.build_launcher(_launch_site)
        return
    # Adjacent: fire, or wait for the 2 Ti. NEVER step -- see bug 1.
    if _cardinally_adjacent(my, _target):
        if (rc.can_fire(_target)
                and rc.get_global_resources() >= GameConstants.BUILDER_BOT_ATTACK_COST):
            rc.fire(_target)
        return
    nav.move_adjacent(_target, can_move=can_move)


def _enemy_reaches_core(bit) -> bool:
    """Does this enemy conveyor's chain actually reach THEIR core?

    Flood the enemy conveyor graph outward from their core area. A chain that does
    not touch the core delivers nothing, so damaging it buys the enemy nothing --
    that is the whole point of #46.
    """
    core = _their_core()
    if not core:
        return True
    theirs = map_info._bm_team[1 - map_info._my_team_idx]
    convs = map_info._bm_et[map_info._IDX_CONVEYOR] & theirs
    seen = core
    for _ in range(60):
        nxt = map_info.manhattan(seen) & convs & ~seen
        if not nxt:
            break
        seen |= nxt
    return bool(seen & bit)


def cd_report():
    print("CONNDIAG uid=%d considered=%d connected=%d" % ((rc.get_id(),) + tuple(_CD)), flush=True)
