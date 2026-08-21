"""Builder state: the SIEGE BOT -- launcher-chain to the enemy core (#33).

The first builder we spawn is designated a trekker. Instead of walking, it builds
a launcher beside itself, stands still, and gets thrown ~5 tiles toward the enemy
core; then repeats. On arrival it stops trekking and the normal states (siege,
block, chip) take over at the far end.

WHY A CHAIN BEATS WALKING. A throw is a TELEPORT on a launcher-centred disk,
1 <= d2 <= 26 (~5.1 tiles), with NO line of sight and NO terrain check -- 60.4% of
778 observed throws crossed a wall. And the LAUNCHER pays the action, never the
thrown builder: a throw does not touch the builder's move budget, so it can land
and still act. Walking the same distance costs ~5 turns AND forfeits 5 actions,
because moving and acting are mutually exclusive.

WHY THIS SHAPE AND NOT ANOTHER AGGRESSIVE STATE. Four separate plays have failed
here on travel cost alone -- core-ring barriers +0.0012, disrupt 2->6 -4.6,
siege-with-travel +0.0385, body-blocking net -0.016 on the full panel. Every one
paid builder-turns to walk somewhere while an economy was already running. This
commits the FIRST builder, before there is an economy to steal turns from, and
buys the travel out with titanium instead of turns.

THE COST, which is the real risk: LAUNCHER_BASE_COST is 20 Ti -- exactly a
harvester, which pays 2.5 Ti/round forever. So each hop must be worth a harvester.
Tyr_Jython averages only 3.38 launchers per game, i.e. a partial chain, not a full
crossing. HOPS_MAX is the knob that decides how much we are willing to spend.
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


# Above route (8) so the trekker actually goes, below attack (9)/heal (9.5).
MAX_SCORE = 8.2
# How many builders become trekkers, claimed in spawn order.
SIEGE_BOTS = 1
# MEASURED: claiming the FIRST builder is catastrophic (-0.38 vs herbert11, Tyr
# collapsed +0.81 -> +0.02) because that builder IS the opening economy -- it lays
# the starting conveyor plan. So claim later, and only once a crew exists to spare
# one. CLAIM_AFTER_ROUND..CLAIM_BEFORE_ROUND is the window; MIN_CREW is the gate.
CLAIM_AFTER_ROUND = 30
CLAIM_BEFORE_ROUND = 80
MIN_CREW = 4
# Stop paying for hops after this many; walk the rest.
HOPS_MAX = 4
# MEASURED: attempts 1 and 2 built ZERO launchers and threw ZERO times across
# every game -- the trekker just walked at the enemy core and died. A launcher is
# LAUNCHER_BASE_COST 20 Ti plus ti_reserve() (~13), against a bank that sits at
# 3-4 Ti. So the affordability test never passed and we measured a suicide walk,
# not a launcher chain. SAVE_MODE makes the trekker HOLD STILL and wait for the
# bank rather than walking off without its transport.
SAVE_MODE = True
# Give up waiting after this many consecutive turns and just walk.
SAVE_PATIENCE = 40
# Consider ourselves arrived inside this Chebyshev distance of their core.
ARRIVE_D = 6

_trekkers = {}      # unit id -> hops used
_waited = {}        # unit id -> consecutive turns spent waiting for launcher money
_arrived = set()


def _their_core():
    return map_info._bm_their_core_area


def _dist_to_core(p) -> int:
    core = _their_core()
    if not core:
        return 999
    w = map_info._width
    best = 999
    m = core
    while m:
        b = m & -m
        n = b.bit_length() - 1
        m ^= b
        d = max(abs(n % w - p.x), abs(n // w - p.y))
        if d < best:
            best = d
    return best


def is_trekker(uid) -> bool:
    return uid in _trekkers and uid not in _arrived


def score(can_move=True):
    uid = rc.get_id()
    if uid in _arrived:
        return 0
    if uid not in _trekkers:
        r = rc.get_current_round()
        if (CLAIM_AFTER_ROUND <= r <= CLAIM_BEFORE_ROUND
                and len(_trekkers) < SIEGE_BOTS
                and map_info._bm_friendly_bots.bit_count() >= MIN_CREW):
            _trekkers[uid] = 0
        else:
            return 0
    if not _their_core():
        return 0                       # do not know where to go yet
    if _dist_to_core(map_info._my_pos) <= ARRIVE_D:
        _arrived.add(uid)              # hand over to siege/block/chip
        return 0
    return MAX_SCORE


def _adjacent_friendly_launcher() -> bool:
    my = map_info._my_pos
    w = map_info._width
    bit = 1 << (my.x + my.y * w)
    near = map_info.manhattan(bit, 2) & ~bit      # pickup reach is r2 <= 2
    mine = map_info._bm_team[map_info._my_team_idx]
    return bool(near & map_info._bm_et[map_info._IDX_LAUNCHER] & mine)


def run(can_move=True):
    uid = rc.get_id()
    log("TREK", uid, _trekkers.get(uid))
    # A launcher beside us will throw us on its own turn: hold still and let it.
    if _adjacent_friendly_launcher():
        return
    hops = _trekkers.get(uid, 0)
    if hops < HOPS_MAX:
        cost = rc.get_launcher_cost()
        bank = rc.get_global_resources()
        if bank < cost + map_info.ti_reserve():
            if SAVE_MODE:
                w = _waited.get(uid, 0) + 1
                _waited[uid] = w
                if w <= SAVE_PATIENCE:
                    return          # hold still and let the bank build
        if bank >= cost + map_info.ti_reserve():
            my = map_info._my_pos
            for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                p = Position(my.x + d[0], my.y + d[1])
                if not map_info.in_bounds(p):
                    continue
                if rc.can_build_launcher(p, Direction.NORTH):
                    rc.build_launcher(p, Direction.NORTH)
                    _trekkers[uid] = hops + 1
                    return
    # Out of hops or out of money: walk.
    core = _their_core()
    if core and can_move:
        w = map_info._width
        n = (core & -core).bit_length() - 1
        nav.move_to(Position(n % w, n // w), can_move=can_move)
