"""Builder state: spend an otherwise-wasted action on an adjacent enemy building.

MEASURED WASTE (6 local games, 5763 of our builder unit-turns):
    moved   73.6%
    acted   18.9%
    WASTED   7.5%  -- neither moved nor acted
Of the wasted turns, 50.9% had an adjacent enemy BUILDING we could have hit and
26.6% had a damaged friendly to heal; only 26.2% had genuinely nothing to do.

Moving and acting are mutually exclusive (the engine needs BOTH cooldowns at 0
for either), so a builder that does not move has its whole action unspent. The
bot already has a free-action retry in builder.py -- select_best_state with
can_move=False -- but NO state offers "hit the enemy building next to me" as an
in-place action: chip only scores when standing on one of its precomputed barrier
tiles, and attack builds turrets. So the retry finds nothing and the action is
thrown away.

Builders can only damage BUILDINGS, never other bots ("Builder Bots can attack
the building on any orthogonally adjacent tile"), so enemy bots are not targets.
2 Ti per hit for 2 damage.

SCORE 0.5 is deliberately below explore (1), the idle fallback. This must never
displace real work -- it exists solely to consume an action that would otherwise
evaporate, which is why it also sits behind a titanium gate: at 2 Ti a hit, an
idle builder chipping every turn is exactly the kind of slow drain that the
ammo bound (herbert11, +0.2833) was shipped to stop.
"""
import map_info
import units.builder
from fcode import *
from log import log

rc: Controller = None


def init(c: Controller):
    global rc
    rc = c


MAX_SCORE = 0.5
# Titanium required ABOVE the reserve before we will spend 2 Ti on a free chip.
TI_BUFFER = 0

_target = None


def score(can_move=True):
    global _target
    _target = None
    need = GameConstants.BUILDER_BOT_ATTACK_COST + map_info.ti_reserve() + TI_BUFFER
    if rc.get_global_resources() < need:
        return 0
    my = map_info._my_pos
    w = map_info._width
    adj = map_info.manhattan(1 << (my.x + my.y * w))
    enemy = adj & map_info._bm_any_building & map_info._bm_team[1 - map_info._my_team_idx]
    if not enemy:
        return 0
    # Prefer a conveyor believed to be carrying titanium -- damage there costs
    # them delivery, not just hit points.
    pref = enemy & map_info._bm_ti_carrying
    pool = pref or enemy
    b = pool & -pool
    n = b.bit_length() - 1
    p = Position(n % w, n // w)
    if not rc.can_fire(p):
        return 0
    _target = p
    return MAX_SCORE


def run(can_move=True):
    if _target is None:
        return
    log("FREECHIP", _target)
    if rc.can_fire(_target):
        rc.fire(_target)
