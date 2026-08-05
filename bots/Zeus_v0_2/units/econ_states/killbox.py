"""KILLBOX builder — one econ bot builds a protected gunner nest by our core
before it resumes economy.

It is the highest-priority economy behaviour so it happens first; once the nest
(a gunner facing inward, walled off by barriers / existing walls) is complete it
scores 0 and the bot goes back to harvesting/routing. The geometry is the
deterministic killbox_plan, shared with the launcher and the gunner.
"""

import map_info
import comms
import units.builder
import units.killbox_plan as killbox_plan
import units.atk_states.attack as attack
from fcode import Controller, Position
from log import log
from pathing import Pathing

rc: Controller = None
nav: Pathing = None

_CARDINALS = ((0, -1), (1, 0), (0, 1), (-1, 0))

MAX_SCORE = 12  # above core_block (10) / counter_mirror (11): build the nest first

target: Position | None = None


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _is_killbox_builder() -> bool:
    return units.builder._economy_builder and units.builder._economy_index == 0


def _has_mine(pos: Position, et_idx: int) -> bool:
    bit = 1 << (pos.x + pos.y * map_info._width)
    return bool(map_info._bm_et[et_idx] & map_info._bm_team[map_info._my_team_idx] & bit)


def _is_built(site: Position, kind: str) -> bool:
    if kind == "gunner":
        return _has_mine(site, map_info._IDX_GUNNER)
    n = site.x + site.y * map_info._width
    if map_info._bm_env[map_info._IDX_ENV_WALL] & (1 << n):
        return True  # a wall already seals this side
    return _has_mine(site, map_info._IDX_BARRIER)


def _next_build(route):
    """Next (site, kind) to build, in the route's park-ending order — or None
    when the killbox is complete."""
    for site, kind in route["order"]:
        if not _is_built(site, kind):
            return site, kind
    return None


# If a single killbox piece can't be built within this many rounds (unreachable
# stance, unaffordable under the pricier gunner, blocked by an enemy), give up on
# the killbox and return to economy — never deadlock this bot standing on a stance
# forever (which also starves the economy that would let it afford the build).
STUCK_TIMEOUT = 12

_abandoned = False
_stuck_site = None
_stuck_since = None


def score() -> int:
    global target, _abandoned, _stuck_site, _stuck_since
    target = None
    if _abandoned:
        return 0
    if not _is_killbox_builder():
        return 0
    if not killbox_plan.active():
        return 0  # only build the killbox on gunner-only (no-barrier) maps
    route = killbox_plan.build_route()
    if route is None:
        return 0
    nxt = _next_build(route)
    if nxt is None:
        return 0  # nest complete -> resume economy
    # Abandon if we've been stuck on the SAME piece too long (no progress).
    r = rc.get_current_round()
    key = (nxt[0].x, nxt[0].y)
    if key != _stuck_site:
        _stuck_site, _stuck_since = key, r
    elif r - _stuck_since > STUCK_TIMEOUT:
        _abandoned = True
        return 0
    target = nxt[0]
    return MAX_SCORE


def run() -> None:
    global target
    log("KILLBOX")
    p = killbox_plan.plan()
    route = killbox_plan.build_route()
    if p is None or route is None:
        return
    nxt = _next_build(route)
    if nxt is None:
        return
    site, kind = nxt
    target = site
    center = p["center"]

    # Build from the NEAREST REACHABLE outside stance (never the interior) so
    # sealing the last side can't trap us and a stance blocked by an enemy bot
    # doesn't stall the build. (The route's park optimisation lives in the build
    # ORDER and the launcher landing, not in fixating on one stance.)
    on_valid_stance = (
        map_info._my_pos.distance_squared(site) == 1
        and (map_info._my_pos.x, map_info._my_pos.y) != (center.x, center.y)
    )
    if not on_valid_stance:
        stances = _outside_stances(site, center)
        if stances:
            # Push through enemy turret threat to reach the stance (the killbox
            # is a core-side defensive build; often its only stance is exposed).
            nav.move_to(stances, avoid_turret=False, allow_enemy_gunner=True)
        return

    if kind == "gunner":
        reserve = max(map_info.builder_ti_reserve(), attack.GUNNER_TI_FLOOR)
        if (
            rc.get_global_resources() >= rc.get_gunner_cost() + reserve
            and rc.can_build_gunner(site, p["facing"])
        ):
            rc.build_gunner(site, p["facing"])
            comms.note_gunner_built()
            map_info.update_at(site)
        return

    if (
        rc.get_global_resources() >= rc.get_barrier_cost() + map_info.builder_ti_reserve()
        and rc.can_build_barrier(site)
    ):
        rc.build_barrier(site)
        map_info.update_at(site)


def _outside_stances(site: Position, center: Position) -> set:
    """Passable cardinal neighbours of `site` the bot may stand on to build it,
    EXCLUDING the killbox interior (center) — so it never walks inside and never
    gets sealed in when the last wall goes up."""
    stances = set()
    w, h = map_info._width, map_info._height
    for dx, dy in _CARDINALS:
        x, y = site.x + dx, site.y + dy
        if not (0 <= x < w and 0 <= y < h):
            continue
        if (x, y) == (center.x, center.y):
            continue
        pos = Position(x, y)
        if map_info.is_passable(pos):
            stances.add(pos)
    return stances
