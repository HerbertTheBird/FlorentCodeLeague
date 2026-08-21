"""Builder state: kill a flowing conveyor we can WIN THE RACE to (#44).

The gate that matters is not "is a guard standing there now" -- that is a
snapshot -- but "can a healer GET here before we finish". A conveyor is 20 HP and
a builder does 2 damage a turn, so a clean kill is 10 turns; a healer restores
4 HP a turn against our 2, so once one arrives the tile is unkillable and every
titanium we spent is wasted. So we commit only when the nearest enemy bot's BFS
distance to OUR CHOSEN ATTACK TILE is at least ENEMY_MIN.

PLAN OVER ALL FOUR APPROACHES, not the nearest one. The four cardinal neighbours
of a conveyor differ in ways that decide the whole engagement: one may be walled,
occupied, under turret threat, or reachable only by a detour -- and, more
importantly, they differ in how far the ENEMY is from each. Picking the nearest
approach and then asking about the race gets it backwards; we score every
(conveyor, approach) pair and take the one we actually win.

Targets prefer FLOW -- observed carrying (_bm_conv_ti) over believed
(_bm_ti_carrying) over idle -- because cutting a loaded belt denies titanium in
transit and the delivery behind it, not 3 Ti of rebuildable belt. That
distinction is what separates this from the global conveyor re-weighting that
measured neutral: there we paid 12 Ti of gunner ammo for a 3 Ti belt tile; here a
builder pays 2 Ti a shot for a tile we have already proved we can finish.
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


MAX_SCORE = 7.6          # above siege(6.0)/harvest(7), below attack(9)
ENEMY_MIN = 3
ALL_FOUR = True          # score every approach, not just the nearest
RING_ONLY = False        # True = only conveyors on the enemy core ring
MAX_CAND = 6             # cap candidates so the BFS work stays bounded
_CARD = ((0, -1), (1, 0), (0, 1), (-1, 0))

_target = None           # the conveyor
_stand = None            # the approach tile we picked


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


def _enemy_dist_to(bit) -> int:
    """BFS distance of the nearest enemy bot to a tile. Large if none can reach."""
    enemies = map_info._bm_enemy_bots
    if not enemies:
        return 999
    _, d = nav.closest(enemies, pos=bit, to_adjacent=False)
    return 999 if d is None or d < 0 else d


def score(can_move=True):
    global _target, _stand
    _target = None
    _stand = None
    theirs = map_info._bm_team[1 - map_info._my_team_idx]
    cand = map_info._bm_et[map_info._IDX_CONVEYOR] & theirs
    if RING_ONLY:
        cand &= _ring()
    if not cand:
        return 0
    # Flow first: observed, then believed, then anything.
    for pool in (cand & map_info._bm_conv_ti, cand & map_info._bm_ti_carrying, cand):
        if pool:
            cand = pool
            break
    walk = map_info.passable()
    threat = map_info._bm_enemy_turret_threat
    w, h = map_info._width, map_info._height
    best = None
    seen = 0
    m = cand
    while m and seen < MAX_CAND:
        b = m & -m
        n = b.bit_length() - 1
        m ^= b
        seen += 1
        tx, ty = n % w, n // w
        approaches = _CARD if ALL_FOUR else _CARD[:1]
        for dx, dy in approaches:
            ax, ay = tx + dx, ty + dy
            if not (0 <= ax < w and 0 <= ay < h):
                continue
            abit = 1 << (ax + ay * w)
            if not (walk & abit) or (threat & abit):
                continue
            _, mine = nav.closest(abit, to_adjacent=False)
            if mine is None or mine < 0:
                continue
            ed = _enemy_dist_to(abit)
            if ed < ENEMY_MIN:
                continue                  # they reach it in time to heal
            key = (mine, -ed)
            if best is None or key < best[0]:
                best = (key, Position(tx, ty), Position(ax, ay))
    if best is None:
        return 0
    _target, _stand = best[1], best[2]
    return MAX_SCORE


def run(can_move=True):
    if _target is None or _stand is None:
        return
    log("CONVKILL", _target, "from", _stand)
    my = map_info._my_pos
    if my.x == _stand.x and my.y == _stand.y:
        if (rc.can_fire(_target)
                and rc.get_global_resources() >= GameConstants.BUILDER_BOT_ATTACK_COST):
            rc.fire(_target)
        return                            # never step off the approach tile
    nav.move_to(_stand, can_move=can_move)
