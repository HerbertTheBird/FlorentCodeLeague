"""Builder state: clear enemy structures off OUR OWN core ring (#50).

The mirror of ringcut. Our core's twelve chebyshev-1 tiles are simultaneously its
ENTIRE spawn area (CORE_SPAWNING_RADIUS_SQ = 2) and the only tiles a conveyor can
deliver into it from, so an enemy building parked there throttles our income and
our ability to spawn at the same time.

MEASURED FREQUENCY (h18_ourring_diag, 6 games): an enemy structure sat on our ring
on 8328 of 15637 sampled builder-turns -- 53.3% -- and only 501 of those were
guarded by an enemy bot. So the opportunity is common and mostly UNCONTESTED.

TWO WAYS TO CLEAR IT, split into separate arms:
  MODE "bbot"   builder attack, 2 damage for 2 Ti. Our builders SPAWN on this ring,
                so travel cost is near zero. A barrier is 30 HP (15 turns), a
                conveyor 20 HP (10 turns).
  MODE "gun"    build a GUNNER covering the tile: 7 dmg/turn, which out-damages a
                single enemy healer (4 HP/turn) where a builder at 2 dmg/turn does
                NOT. The turret is the answer precisely when the thing is guarded.
  MODE "both"   builder attack normally, gunner when the target is guarded.

The unguarded gate is inherited from ringcut, where it was worth +0.84 -- chipping
something a healer can reach is worse than not attacking at all.
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


MAX_SCORE = 9.6          # above heal(9.5): our own ring is the highest-value tile set
MODE = "bbot"
STICKY = True

_target = None
_gun_site = None
_committed = {}


def _our_ring() -> int:
    core = map_info._bm_my_core_area
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


def _guarded(bit) -> bool:
    return bool(map_info.manhattan(bit) & ~bit & map_info._bm_enemy_bots)


def score(can_move=True):
    global _target, _gun_site
    _target = None
    _gun_site = None
    theirs = map_info._bm_team[1 - map_info._my_team_idx]
    cand = _our_ring() & map_info._bm_any_building & theirs
    if not cand:
        _committed.pop(rc.get_id(), None)
        return 0
    w = map_info._width
    uid = rc.get_id()

    if STICKY:
        prev = _committed.get(uid)
        if prev is not None:
            pb = 1 << (prev[0] + prev[1] * w)
            if cand & pb:
                _target = Position(prev[0], prev[1])
                return MAX_SCORE
            _committed.pop(uid, None)

    free = 0
    guarded = 0
    m = cand
    while m:
        b = m & -m
        m ^= b
        if _guarded(b):
            guarded |= b
        else:
            free |= b

    pool = free if MODE == "bbot" else (guarded if MODE == "gun" else (free or guarded))
    if MODE == "both" and not free and guarded:
        pool = guarded
    if not pool:
        return 0
    target, dist = nav.closest(pool, to_adjacent=True)
    if target is None:
        return 0
    tb = 1 << (target.x + target.y * w)
    want_gun = (MODE == "gun") or (MODE == "both" and (guarded & tb))
    if want_gun:
        # A gunner at 7 dmg/turn beats one healer at 4; a builder at 2 does not.
        if rc.get_global_resources() < rc.get_gunner_cost() + map_info.ti_reserve():
            return 0
        walk = map_info.passable()
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            p = Position(target.x + dx, target.y + dy)
            if not map_info.in_bounds(p):
                continue
            if not ((walk >> (p.x + p.y * w)) & 1):
                continue
            _gun_site = p
            break
        if _gun_site is None:
            return 0
    _target = target
    _committed[uid] = (target.x, target.y)
    return MAX_SCORE


def _adj(a, b) -> bool:
    return abs(a.x - b.x) + abs(a.y - b.y) == 1


def run(can_move=True):
    if _target is None:
        return
    log("DEFRING", _target, "gun", _gun_site)
    my = map_info._my_pos
    if _gun_site is not None:
        facing = map_info.direction_to(_gun_site, _target)
        if _adj(my, _gun_site) or (my.x == _gun_site.x and my.y == _gun_site.y):
            if rc.can_build_gunner(_gun_site, facing):
                rc.build_gunner(_gun_site, facing)
            return
        nav.move_adjacent(_gun_site, can_move=can_move)
        return
    # Never step onto the target -- you cannot attack the tile you stand on, and
    # vacating our square hands the enemy the adjacency it needs to heal.
    if _adj(my, _target):
        if (rc.can_fire(_target)
                and rc.get_global_resources() >= GameConstants.BUILDER_BOT_ATTACK_COST):
            rc.fire(_target)
        return
    nav.move_adjacent(_target, can_move=can_move)
