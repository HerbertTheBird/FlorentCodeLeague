"""Builder state: ride a launcher relay to the enemy core.

The transport half of Tyr_Jython's siege opening (`bots/Tyr_Jython/units/
states/siege.py` plus `jython.py`). One builder -- the first one our core spawns
-- builds a launcher on the tile beside it, holds still, is thrown ~5 tiles by
that launcher on the launcher's next turn, and does it again from where it
lands. Tyr's own trace of a 30x30 game: launchers on t1, t3, t5, t7, t9, t11 and
t13, arriving beside the enemy core on t13 against a walking builder's t40-odd.

This state ONLY does the transport. Tyr's siege state goes on to take the twelve
tiles of the enemy core's ring; herbert already has a state for that
(`units/states/siege.py`, barriers on the eight delivery tiles), so on arrival
this one stands down permanently and hands the builder back to the ordinary bot.

# Three things that are easy to get wrong

* `rc.can_build_launcher(pos)` / `rc.build_launcher(pos)` take NO direction,
  unlike the gunner and the sentinel. Passing one raises every turn and builds
  nothing, silently -- `main.py` swallows the exception into a stdout line.
* Every unit runs in its OWN interpreter, so nothing set here is visible to the
  launcher that has to do the throwing. The two ends are tied together by the
  builder's entity id (`relaygeom.RELAY_BOT_IDS`) and by nothing else.
* The turn spent standing on a launcher's pickup tile has to be spent on
  NOTHING. `builder.run` hands unspent turns to the healer and then to a
  second-choice state, either of which can move the builder off the tile and
  strand the whole chain -- so `holding()` tells it not to.
"""

import os

from main import has_op
from fcode import Position

import map_info
import relaygeom
import units.builder

# Above everything else this builder could be doing. While the relay is live it
# is the only unit on the board doing this job, and a turn spent elsewhere is a
# turn every remaining hop waits.
MAX_SCORE = 120

# The enemy core is inferred from symmetry, and symmetry is usually settled on
# the first turn. If it is not settled by here the map is one we cannot aim at,
# and the builder is worth more in the economy than standing about.
GIVE_UP_ROUND = 12

# Turns of no progress -- no build, no throw -- before the relay gives up. This
# is what stops a builder waiting forever beside a launcher that is never going
# to throw it (a defensive one that happened to be adjacent, say).
STALL_LIMIT = 12

# Turns to wait on an adjacent launcher we did not build before concluding it is
# not part of the relay and building our own next to it.
HOLD_PATIENCE = 3

TRACE = os.environ.get("RELAY_TRACE") == "1"

rc = None
nav = None

am_relay = False        # decided once, from our entity id
_checked = False
_dead = False           # stood down: arrived, unknown map, or stalled
_holding = False        # this turn was deliberately spent on nothing
_stall = 0
_hold_pos = None
_hold_turns = 0
_ignored = set()        # launcher tiles that have had their chance to throw us


def init(c):
    global rc, nav, am_relay, _checked, _dead, _holding, _stall
    global _hold_pos, _hold_turns, _ignored
    rc = c
    nav = units.builder.nav
    am_relay = False
    _checked = False
    _dead = False
    _holding = False
    _stall = 0
    _hold_pos = None
    _hold_turns = 0
    _ignored = set()
    relaygeom.reset()


def _trace(*a):
    if TRACE:
        print("RELAY", rc.get_current_round(), rc.get_id(), *a)


def _me():
    return (map_info._my_pos.x, map_info._my_pos.y)


def _launcher_in_pickup(me):
    """A friendly launcher standing where it could pick us up, or None.

    Pickup is r^2 <= 2, i.e. the eight neighbours. Anything already there can
    throw us, so building another one beside it would be 20+ titanium spent to
    duplicate a machine we are already standing next to.
    """
    lm = (map_info._bm_et[map_info._IDX_LAUNCHER]
          & map_info._bm_team[map_info._my_team_idx])
    if not lm:
        return None
    w = map_info._width
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            p = (me[0] + dx, me[1] + dy)
            if not relaygeom.in_bounds(p) or p in _ignored:
                continue
            if (lm >> (p[0] + p[1] * w)) & 1:
                return p
    return None


def _buildable(site) -> bool:
    return rc.can_build_launcher(Position(site[0], site[1]))


def score(can_move=True):
    global am_relay, _checked, _dead
    if not _checked:
        _checked = True
        am_relay = rc.get_id() in relaygeom.RELAY_BOT_IDS
        if am_relay:
            _trace("claimed the relay role")
    if not am_relay or _dead:
        return 0
    if relaygeom.their_core() is None:
        if rc.get_current_round() > GIVE_UP_ROUND:
            _dead = True
            _trace("stood down: no symmetry, enemy core unknown")
        return 0
    if relaygeom.dist_at(_me()) <= relaygeom.ARRIVE_DIST:
        _dead = True
        _trace("ARRIVED at", _me(), "core", relaygeom.their_core())
        return 0
    return MAX_SCORE


def run(can_move=True):
    global _holding, _dead, _stall, _hold_pos, _hold_turns
    _holding = False
    me = _me()

    if me != _hold_pos:
        _hold_pos = me
        _hold_turns = 0

    # Never a redundant launcher: anything already inside our pickup radius can
    # throw us, so stand still and let it. `_ignored` is the escape hatch -- a
    # launcher that has had HOLD_PATIENCE turns to throw us and has not is not
    # part of this relay, and we build our own rather than wait on it forever.
    near = _launcher_in_pickup(me)
    if near is not None:
        _hold_turns += 1
        if _hold_turns <= HOLD_PATIENCE:
            _holding = True
            _stall += 1
            if _stall > STALL_LIMIT:
                _dead = True
                _trace("stood down: stalled beside", near)
            _trace("holding beside launcher", near)
            return
        _ignored.add(near)
        _trace("giving up on launcher", near, "- building my own")

    # Asked before the geometry, because `can_build_launcher` is False when we
    # cannot afford one -- which would make `best_hop` report "no route" and
    # retire a relay that is merely one turn short of the money.
    if not has_op() or rc.get_global_resources() < rc.get_launcher_cost():
        # 20-odd titanium, and the opening bank is hundreds. If it is ever
        # short, waiting one turn beats walking away from a chain half built.
        _holding = True
        _stall += 1
        _trace("waiting: op", has_op(), "ti", rc.get_global_resources(),
               "cost", rc.get_launcher_cost())
        if _stall > STALL_LIMIT:
            _dead = True
            _trace("stood down: never able to buy a launcher")
        return

    hop = relaygeom.best_hop(me, site_ok=_buildable)
    if hop is None:
        _stall += 1
        _trace("no hop from", me, "dist", relaygeom.dist_at(me))
        if _stall > STALL_LIMIT:
            _dead = True
            _trace("stood down: no hop available")
        return
    site, landing = hop
    p = Position(site[0], site[1])
    rc.build_launcher(p)                  # site_ok already asked can_build
    map_info.update_at(p)
    _holding = True
    _stall = 0
    _hold_turns = 0
    _trace("built launcher", site, "aiming at", landing,
           "dist", relaygeom.dist_at(me), "->", relaygeom.dist_at(landing))


def holding() -> bool:
    """True when this builder is deliberately spending its turn on nothing.

    Standing on a launcher's pickup tile waiting to be thrown is the whole
    protocol; `builder.run` offers unspent turns to the healer and to a
    second-choice state, either of which would move or spend the action and
    strand the throw.
    """
    return am_relay and not _dead and _holding
