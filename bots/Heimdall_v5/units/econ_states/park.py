"""PARK state — an econ bot's idle behaviour once its supply line is delivering
to the core: instead of wandering (explore), it holds a defensive slot near our
core. Priority sits below route/harvest (so routes always finish and are never
abandoned half-built) but above explore (so an idle econ bot parks rather than
roams).

Chosen slot: a passable tile within CORE_RANGE Manhattan of any of our core's
tiles, as close as possible to the enemy core, kept at least SPACING Manhattan
from other friendly builder bots so the parked bots fan out rather than stack.
"""

import map_info
import units.builder
from pathing import Pathing
from fcode import Controller, Position
from log import log

rc: Controller = None
nav: Pathing = None

MAX_SCORE = 2       # below route (7.75) / harvest (4), above explore (1)
target = None

CORE_RANGE = 3      # stay within this Manhattan distance of any core tile
SPACING = 3         # stay at least this Manhattan distance from other builders


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def score() -> int:
    if not units.builder._economy_builder:
        return 0
    # A bot that has finished at least one route may park between economy jobs;
    # harvest and route both outrank this state and can reactivate later.
    # Before that, park only activates once EVERY one of our conveyors
    # reaches the core (no route left dangling) — a structural check that, unlike
    # "no route claims right now", isn't fooled by the transient lull while a
    # frontier conveyor waits to load. That prevents parking (and locking) with a
    # half-built path.
    if units.builder._route_done or map_info.my_route_complete():
        return MAX_SCORE
    return 0


def _park_zone() -> int:
    """Passable tiles within CORE_RANGE Manhattan of our core (excluding the
    core footprint itself)."""
    core_area = map_info._bm_my_core_area
    if not core_area:
        return 0
    near = map_info.expand_manhattan(core_area, CORE_RANGE)
    return near & map_info._bm_passable_FFF & ~core_area


def _closest_to(mask: int, goal: Position) -> Position | None:
    """Tile in `mask` with the smallest Manhattan distance to `goal` (then a
    stable coordinate tiebreak)."""
    w = map_info._width
    best = None
    best_key = None
    m = mask
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        x, y = n % w, n // w
        key = (abs(x - goal.x) + abs(y - goal.y), x, y)
        if best_key is None or key < best_key:
            best_key = key
            best = Position(x, y)
    return best


def run():
    global target
    log("PARK")
    zone = _park_zone()
    if not zone:
        return

    # Keep at least SPACING Manhattan from other friendly builder bots: drop any
    # tile within (SPACING - 1) of another bot. Fall back to the full zone if
    # spacing leaves nothing reachable.
    w = map_info._width
    my_bit = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    others = map_info._bm_friendly_bots & ~my_bit
    spaced = zone & ~map_info.expand_manhattan(others, SPACING - 1) if others else zone
    pool = spaced if spaced else zone

    goal = map_info._predicted_enemy_core or map_info._their_core
    best = _closest_to(pool, goal) if goal is not None else _closest_to(pool, map_info._my_core)
    if best is None:
        return
    target = best
    if map_info._my_pos != best:
        nav.move_to(best)
