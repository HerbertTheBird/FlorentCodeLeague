"""Builder state: cut the enemy core's RING conveyors (#42).

The relay gets a builder to the enemy core by t12. This is what makes the trip
worth paying for.

THE TARGET. The twelve tiles at chebyshev 1 of the 2x2 core are simultaneously the
core's ENTIRE spawn ring (CORE_SPAWNING_RADIUS_SQ = 2) and the only tiles from
which a conveyor can deliver into it. So a conveyor standing there is not 3 Ti of
belt -- it is the core's supply line. Killing it and barriering the tile denies
delivery permanently for 3 Ti.

WHY THIS IS NOT THE REJECTED GLOBAL RE-WEIGHTING. Making turrets prefer conveyors
everywhere measured neutral-to-negative, because a generic 20 HP / 3 Ti conveyor
costs 12 Ti of gunner ammo to kill -- 4:1 against us. Here the tool is a builder at
2 damage for 2 Ti against a target whose loss cuts the core's income, and the
follow-up is a 3 Ti barrier that holds the tile for good.

TARGET CHOICE IS BY FLOW. A ring conveyor that is actually carrying is worth far
more than an idle one: prefer observed titanium (_bm_conv_ti), then believed
(_bm_ti_carrying, which v107 widened to harvester-fed belts), then anything.

THE HEALER PROBLEM AND THE STANDING RULE. Heal needs exact Manhattan-1 adjacency
and restores 4 HP for 1 Ti against our 2 damage for 2 Ti, so a guarded conveyor
cannot be chipped down. Standing on the tile OPPOSITE the guard covers the target
and takes one of its four adjacent squares away from them.
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


# Above siege (6.0) -- cutting a live supply line beats scattering barriers on
# empty ring tiles. Below attack (9) / heal (9.5) / block (10).
MAX_SCORE = 7.4
ENGAGE_DIST = 8          # only once the relay has actually delivered us
USE_FLOW = True          # prefer carrying conveyors
LAUNCH_GUARD = True

_RC=[0,0,0,0,0]
_target = None
_launch_site = None


def _their_core():
    seen = map_info._bm_their_core_area
    if seen:
        return seen
    try:
        import units.states.relay as relay
        return relay.their_core_area()
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


def _near_core() -> int:
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
    global _target, _launch_site
    _target = None
    _launch_site = None
    _RC[0]+=1
    if _near_core() > ENGAGE_DIST:
        return 0
    _RC[1]+=1
    theirs = map_info._bm_team[1 - map_info._my_team_idx]
    cand = _ring() & map_info._bm_et[map_info._IDX_CONVEYOR] & theirs
    if not cand:
        return 0
    _RC[2]+=1
    if USE_FLOW:
        # Observed carrying beats believed beats idle.
        for pool in (cand & map_info._bm_conv_ti,
                     cand & map_info._bm_ti_carrying,
                     cand):
            if pool:
                cand = pool
                break
    cand &= ~map_info._bm_enemy_turret_threat
    if not cand:
        return 0
    target, dist = nav.closest(cand, to_adjacent=True)
    if target is None or dist > ENGAGE_DIST:
        return 0
    _target = target
    _RC[3]+=1
    if LAUNCH_GUARD:
        # A guard adjacent to the target out-heals us 4 HP/Ti against 2 dmg/2 Ti.
        # A launcher CARDINALLY ADJACENT to the conveyor can throw it away --
        # turret_launcher._try_throw_enemy_away() already does the throwing.
        w = map_info._width
        tb = 1 << (target.x + target.y * w)
        if map_info.manhattan(tb) & ~tb & map_info._bm_enemy_bots:
            mine = map_info._bm_team[map_info._my_team_idx]
            if not (map_info.manhattan(tb, 2) & map_info._bm_et[map_info._IDX_LAUNCHER] & mine):
                for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                    p = Position(target.x + dx, target.y + dy)
                    if map_info.in_bounds(p) and rc.can_build_launcher(p):
                        _launch_site = p
                        break
    return MAX_SCORE


def run(can_move=True):
    if _target is None:
        return
    log("RINGCUT", _target, "launch_site", _launch_site)
    if _launch_site is not None:
        cost = rc.get_launcher_cost()
        if rc.get_global_resources() >= cost and rc.can_build_launcher(_launch_site):
            rc.build_launcher(_launch_site)
            return
    if rc.can_fire(_target) and rc.get_global_resources() >= GameConstants.BUILDER_BOT_ATTACK_COST:
        rc.fire(_target)
        _RC[4]+=1
        return
    nav.move_adjacent(_target, can_move=can_move)


def rc_report():
    print("RCDIAG uid=%d score=%d near=%d ringconvs=%d chosen=%d FIRED=%d" % ((rc.get_id(),)+tuple(_RC)), flush=True)
