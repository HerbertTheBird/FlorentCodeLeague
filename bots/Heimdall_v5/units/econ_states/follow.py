"""Khaos-style pursuit of one visible, turret-uncovered enemy builder."""

from fcode import Controller, Position

import comms
import map_info
import units.builder
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 2.5
target: Position | None = None


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _owned_targets(enemies: int) -> int:
    """Assign each enemy to the nearest defense/economy builder.

    Attack builders are deliberately excluded from ownership. Previously an
    attacker that happened to be closer could win the distance comparison even
    though it never runs this state, leaving no defender following the enemy.
    Stable tile-index tie-breaking ensures exactly one defender owns each bot.
    """
    w = map_info._width
    my = map_info._my_pos
    my_n = my.x + my.y * w
    result = 0
    m = enemies
    while m:
        lsb = m & -m
        en = lsb.bit_length() - 1
        m ^= lsb
        ex, ey = en % w, en // w
        my_key = (abs(my.x - ex) + abs(my.y - ey), my_n)
        owner_key = my_key
        friends = map_info._bm_friendly_bots
        while friends:
            flsb = friends & -friends
            fn = flsb.bit_length() - 1
            friends ^= flsb
            friend_id = map_info._bot_at.get(fn)
            if friend_id is None or not comms.is_economy(friend_id):
                continue
            fx, fy = fn % w, fn // w
            owner_key = min(owner_key, (abs(fx - ex) + abs(fy - ey), fn))
        if my_key == owner_key:
            result |= lsb
    return result


def score() -> float:
    global target
    target = None
    if not units.builder._economy_builder:
        return 0
    enemies = map_info._bm_enemy_bots & map_info._bm_visible
    # Match anti_builder's definition of covered: an existing gunner that can
    # rotate onto the builder is enough, even if it currently faces elsewhere.
    from units.econ_states.anti_builder import _covered_by_any_gunner_rotation
    enemies &= ~_covered_by_any_gunner_rotation(enemies)
    enemies = _owned_targets(enemies)
    if not enemies:
        return 0
    target, distance = nav.closest_within(enemies, max_dist=8)
    return MAX_SCORE if target is not None and distance >= 0 else 0


def run() -> None:
    log("FOLLOW")
    if target is not None:
        nav.move_to(target)
