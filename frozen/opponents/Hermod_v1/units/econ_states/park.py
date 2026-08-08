"""PARK state — completed econ routes camp near and defend the core."""

import map_info
import units.builder
import units.launch_plan as launch_plan
from pathing import Pathing
from fcode import Controller, Position
from log import log

rc: Controller = None
nav: Pathing = None

MAX_SCORE = 2       # below route (7.75) / harvest (4), above explore (1)
target = None

CORE_RANGE = 3      # fallback when the opening launcher has been destroyed
SPACING = 2         # enough separation for two defenders around one launcher
ENABLE_LAUNCHER_CAMP = True


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def score() -> int:
    if not units.builder._economy_builder:
        return 0
    # A bot that has finished its route (locked out of route/harvest) always
    # parks. Before locking, park only activates once EVERY one of our conveyors
    # reaches the core (no route left dangling) — a structural check that, unlike
    # "no route claims right now", isn't fooled by the transient lull while a
    # frontier conveyor waits to load. That prevents parking (and locking) with a
    # half-built path.
    if units.builder._route_done or map_info.my_route_complete():
        return MAX_SCORE
    return 0


def _park_zone() -> int:
    """Opening-launcher pickup zone, falling back to the near-core zone."""
    launcher = launch_plan.launcher_position() if ENABLE_LAUNCHER_CAMP else None
    if launcher is not None:
        launcher_bit = 1 << (launcher.x + launcher.y * map_info._width)
        allied_launcher = bool(
            launcher_bit
            & map_info._bm_et[map_info._IDX_LAUNCHER]
            & map_info._bm_team[map_info._my_team_idx]
        )
        if allied_launcher:
            return (
                map_info.expand_chebyshev(launcher_bit)
                & map_info._bm_passable_FFF
                & ~launcher_bit
            )
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

    launcher = launch_plan.launcher_position() if ENABLE_LAUNCHER_CAMP else None
    goal = launcher or map_info._my_core
    best = _closest_to(pool, goal)
    if best is None:
        return
    target = best
    if map_info._my_pos != best:
        nav.move_to(best)
