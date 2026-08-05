"""Walk cardinally adjacent to visible enemy turrets and dismantle them."""

from fcode import Controller, Direction, Position

import comms
import map_info
import units.builder
from log import log
from pathing import Pathing
from units.econ_states.anti_builder import _sites_for_enemy
from units.econ_states.follow import _owned_targets


rc: Controller = None
nav: Pathing = None

# Combat turrets take precedence over core repair. Launchers remain actionable,
# but do not interrupt an urgent repair on their own.
MAX_SCORE = 16
LAUNCHER_SCORE = 14
target: Position | None = None
approach: Position | None = None
gunner_site: Position | None = None
gunner_facing: Direction | None = None
gunner_approach: Position | None = None


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _reachable_approach(pos: Position):
    """Closest safe, reachable cardinal tile from which we can act on pos."""
    w = map_info._width
    my = map_info._my_pos
    my_bit = 1 << (my.x + my.y * w)
    blocked = map_info.get_avoid(False, False, False) & ~my_bit
    occupied = (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & ~my_bit
    candidates = 0
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        p = Position(pos.x + dx, pos.y + dy)
        if not map_info.in_bounds(p) or not map_info.is_passable(p):
            continue
        bit = 1 << (p.x + p.y * w)
        if bit & (blocked | occupied):
            continue
        candidates |= bit
    if not candidates:
        return None, -1
    # Pathing.closest() normally treats the builder's own tile as distance 2 so
    # build-site selection will not choose an unbuildable tile under our feet.
    # Here these are *approach* tiles: already standing on one is ideal and must
    # trigger the attack/build now instead of making us circle the target.
    if candidates & my_bit:
        return my, 0
    return nav.closest(candidates)


def _best_reachable(turrets: int):
    best = None
    best_approach = None
    best_key = None
    w = map_info._width
    for turret in map_info.iter_mask(turrets):
        adjacent, distance = _reachable_approach(turret)
        if adjacent is None or distance < 0:
            continue
        key = (distance, turret.x + turret.y * w)
        if best_key is None or key < best_key:
            best_key = key
            best = turret
            best_approach = adjacent
    return best, best_approach


def _has_adjacent_enemy_builder(turret: Position) -> bool:
    w = map_info._width
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        x, y = turret.x + dx, turret.y + dy
        if not (0 <= x < map_info._width and 0 <= y < map_info._height):
            continue
        bit = 1 << (x + y * w)
        if bit & map_info._bm_enemy_bots & map_info._bm_visible:
            return True
    return False


def _gunner_plan(turret: Position):
    """Return a safe allied-gunner build plan against an enemy gunner."""
    w = map_info._width
    turret_bit = 1 << (turret.x + turret.y * w)
    enemy_idx = 1 - map_info._my_team_idx
    if not (
        turret_bit
        & map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[enemy_idx]
    ):
        return None, None, None
    if turret_bit & map_info._bm_my_gunner_claims:
        return None, None, None

    sites, facings = _sites_for_enemy(turret)
    from units.econ_states import route
    sites &= ~route.planned_route_mask()
    if not sites:
        return None, None, None
    best_site = None
    best_approach = None
    best_key = None
    for site in map_info.iter_mask(sites):
        adjacent, distance = _reachable_approach(site)
        if adjacent is None or distance < 0:
            continue
        n = site.x + site.y * w
        key = (distance, n)
        if best_key is None or key < best_key:
            best_key = key
            best_site = site
            best_approach = adjacent
    if best_site is None:
        return None, None, None
    return best_site, facings[best_site.x + best_site.y * w], best_approach


def score() -> int:
    global target, approach, gunner_site, gunner_facing, gunner_approach
    target = None
    approach = None
    gunner_site = None
    gunner_facing = None
    gunner_approach = None
    if not units.builder._economy_builder:
        return 0
    enemy_idx = 1 - map_info._my_team_idx
    combat_turrets = (
        map_info._bm_et[map_info._IDX_GUNNER]
        | map_info._bm_et[map_info._IDX_SENTINEL]
    ) & map_info._bm_team[enemy_idx] & map_info._bm_visible
    # Do not assign combat turrets to only one builder. A previous ownership
    # filter made every non-owner fall back to healing the core while the gunner
    # continued shooting it. All builders that see a combat turret may converge
    # on it; launchers below remain singly assigned.
    if combat_turrets:
        # Prefer a counter-gunner whenever direct builder contact is unsafe, or
        # when an adjacent enemy builder can repair the target. Previously only
        # the guarded case reached this planner, so safely-unreachable gunners
        # received neither a builder attack nor a counter-turret.
        visible_gunners = combat_turrets & map_info._bm_et[map_info._IDX_GUNNER]
        best_plan = None
        best_key = None
        for gunner in map_info.iter_mask(visible_gunners):
            direct_approach, _ = _reachable_approach(gunner)
            if direct_approach is not None and not _has_adjacent_enemy_builder(gunner):
                continue
            site, facing, build_approach = _gunner_plan(gunner)
            if site is None:
                continue
            key = (
                abs(build_approach.x - map_info._my_pos.x)
                + abs(build_approach.y - map_info._my_pos.y),
                gunner.x + gunner.y * map_info._width,
            )
            if best_key is None or key < best_key:
                best_key = key
                best_plan = (gunner, site, facing, build_approach)
        if best_plan is not None:
            target, gunner_site, gunner_facing, gunner_approach = best_plan
            approach = None
            return MAX_SCORE

        target, approach = _best_reachable(combat_turrets)
        if target is not None:
            return MAX_SCORE

    launchers = (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[enemy_idx]
        & map_info._bm_visible
    )
    launchers = _owned_targets(launchers)
    if launchers:
        target, approach = _best_reachable(launchers)
        if target is not None:
            return LAUNCHER_SCORE
    return 0


def run() -> None:
    log("BREAK TURRET")
    if target is None:
        return
    if gunner_site is not None and gunner_facing is not None and gunner_approach is not None:
        log("COUNTER GUNNER", target, "via", gunner_site)
        if map_info._my_pos != gunner_approach:
            nav.move_to(gunner_approach)
            return
        if (
            rc.get_global_resources()
            >= rc.get_gunner_cost() + map_info.builder_ti_reserve()
            and rc.can_build_gunner(gunner_site, gunner_facing)
        ):
            rc.build_gunner(gunner_site, gunner_facing)
            comms.note_gunner_built()
            map_info.update_at(gunner_site)
        return
    if approach is None:
        return
    if map_info._my_pos != approach:
        nav.move_to(approach)
        return
    if rc.can_fire(target):
        rc.fire(target)
        map_info.update_at(target)
