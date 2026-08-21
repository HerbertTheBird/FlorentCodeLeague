"""Is our core in trouble? One answer, computed once by the core.

Every unit needs this and only the core can see it reliably: a builder ten tiles
away has no vision of its own core, and `map_info` remembers a stale HP from
whenever it last looked. So the CORE evaluates its own health each turn and
broadcasts the verdict in its slot-0 word (see comms.core_distress), and
everyone else just reads it. Two bits on the wire, no polling, no staleness.

# A band, not a line

A single threshold makes builders flap: the core takes a hit, three of them walk
over, one heal puts it back above the line, they all go back to work, and the
next shot starts it again -- so the team pays the walk both ways every couple of
rounds and never actually heals it. Nothing responds until ENGAGE_HP, and once
engaged the healers stay until it is nearly whole again at RELEASE_HP. The gap
is what stops the flapping.

# Why these numbers

A sentinel does {SENTINEL_DAMAGE} damage on a 2-round cooldown, i.e. 9 HP a
round, so a core at ENGAGE_HP is about 33 rounds from dead -- far enough out that
builders can finish what they are holding, close enough that the walk is worth
it. A builder restores {HEAL_AMOUNT} HP for 1 titanium, so it takes 2.25 of them
to hold one sentinel off and three to gain on it; below CRITICAL_HP the core is
roughly 22 rounds from dead and nothing else on the board matters.
"""

from fcode import GameConstants

import map_info

FINE = 0
ENGAGED = 1
CRITICAL = 2

MAX_HP = GameConstants.CORE_MAX_HP           # 500
ENGAGE_HP = 300
RELEASE_HP = 450
CRITICAL_HP = 200

# 2.25 builders hold one sentinel off; three gain on it. More than that is
# builders queueing for tiles around a 2x2 core while the economy pays for it.
WANT_HEALERS = 3

_engaged = False


def reset() -> None:
    global _engaged
    _engaged = False


def core_hp() -> int:
    """Our core's HP, or -1 if we cannot see it. All four cells share one pool,
    so max() over the 2x2 just skips cells we have no reading for."""
    area = map_info._bm_my_core_area
    hp = -1
    while area:
        bit = area & -area
        area ^= bit
        value = map_info._building_hp[bit.bit_length() - 1]
        if value > hp:
            hp = value
    return hp


def evaluate() -> int:
    """FINE / ENGAGED / CRITICAL, with hysteresis. Core-side only -- everyone
    else reads the broadcast rather than recomputing from a stale HP."""
    global _engaged
    hp = core_hp()
    if hp < 0:
        return FINE
    if _engaged:
        if hp >= RELEASE_HP:
            _engaged = False
    elif hp <= ENGAGE_HP:
        _engaged = True
    if not _engaged:
        return FINE
    return CRITICAL if hp <= CRITICAL_HP else ENGAGED
