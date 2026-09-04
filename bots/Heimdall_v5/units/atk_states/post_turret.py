"""Post-placement work for the dedicated attack builder.

After its first gunner, the builder dismantles enemy conveyors that feed
directly into the enemy core. A nearby, currently-unclaimed enemy builder
interrupts that job: place a gunner against it, preferring a firing lane that
will continue through a core-feed conveyor and then the core after the bot dies.
"""

from fcode import Controller, Direction, EntityType, Position

import comms
import map_info
import units.builder
from log import log
from pathing import Pathing
from units.econ_states.anti_builder import (
    _covered_by_any_gunner_rotation,
    _sites_for_enemy,
    _sites_within_core_radius,
)


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 9.5  # below urgent gunner shielding, above ordinary placements
ADVANCE_SCORE = 2  # above explore, below any useful ordinary gunner placement
target: Position | None = None
gunner_site: Position | None = None
gunner_facing: Direction | None = None
_approach: Position | None = None
_mode = ""


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _bit(pos: Position) -> int:
    return 1 << (pos.x + pos.y * map_info._width)


def _core_feed_conveyors() -> int:
    """Visible enemy conveyors cardinally adjacent and directed into the core."""
    core = map_info._bm_their_core_area
    if not core:
        return 0
    enemy_idx = 1 - map_info._my_team_idx
    conveyors = (
        map_info._bm_et[map_info._IDX_CONVEYOR]
        & map_info._bm_team[enemy_idx]
        & map_info._bm_visible
        & map_info.expand_manhattan(core)
        & ~core
    )
    result = 0
    mask = conveyors
    while mask:
        lsb = mask & -mask
        n = lsb.bit_length() - 1
        mask ^= lsb
        output_n = map_info._building_conv_target[n]
        if output_n >= 0 and (core & (1 << output_n)):
            result |= lsb
    return result


def _safe_cardinal_approach(pos: Position):
    """Closest reachable cardinal tile from which the builder can fire."""
    my = map_info._my_pos
    my_bit = _bit(my)
    blocked = map_info.get_avoid(False, False, False) & ~my_bit
    bots = (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & ~my_bit
    candidates = 0
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        tile = Position(pos.x + dx, pos.y + dy)
        if not map_info.in_bounds(tile) or not map_info.is_passable(tile):
            continue
        tile_bit = _bit(tile)
        if tile_bit & (blocked | bots):
            continue
        candidates |= tile_bit
    if not candidates:
        return None, -1
    if candidates & my_bit:
        return my, 0
    return nav.closest(candidates)


def _closest_feed_approach(feed: int):
    best_target = None
    best_approach = None
    best_key = None
    for conveyor in map_info.iter_mask(feed):
        approach, distance = _safe_cardinal_approach(conveyor)
        if approach is None or distance < 0:
            continue
        key = (distance, conveyor.x + conveyor.y * map_info._width)
        if best_key is None or key < best_key:
            best_key = key
            best_target = conveyor
            best_approach = approach
    return best_target, best_approach


def _nearby_unclaimed_builders() -> int:
    enemies = (
        map_info._bm_enemy_bots
        & map_info._bm_visible
    )
    enemies &= ~_covered_by_any_gunner_rotation(enemies)
    if not enemies:
        return 0
    result = 0
    my = map_info._my_pos
    for enemy in map_info.iter_mask(enemies):
        if my.distance_squared(enemy) <= 9:
            result |= _bit(enemy)
    return result


def _placement_has_safe_exit(site: Position) -> bool:
    """Do not box the builder in with the counter-gunner it is about to build."""
    my = map_info._my_pos
    occupied = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
        | _bit(site)
    )
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        escape = Position(my.x + dx, my.y + dy)
        if not map_info.in_bounds(escape):
            continue
        escape_bit = _bit(escape)
        if escape_bit & occupied:
            continue
        if escape_bit & map_info._bm_enemy_hard_threat:
            continue
        if map_info.is_passable(escape):
            return True
    return False


def _core_staging_tile() -> Position | None:
    """Stable open tile beside the visible enemy core, off allied gunner rays."""
    core = map_info._bm_their_core_area
    if not core:
        return None
    occupied = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
        | map_info._bm_enemy_hard_threat
        | map_info._bm_my_gunner_claims
    )
    candidates = map_info.expand_manhattan(core) & ~core & ~occupied
    if not candidates:
        return None
    my = map_info._my_pos
    if candidates & _bit(my):
        return my
    site, distance = nav.closest(candidates)
    return site if site is not None and distance >= 0 else None


def _ray_lane_quality(
    site: Position,
    facing: Direction,
    enemy: Position,
    feed: int,
) -> int:
    """2 for enemy -> feed -> core in one ray, 1 if rotation offers feed -> core."""
    core = map_info._bm_their_core_area
    if not core or not feed:
        return 0
    try:
        facing_idx = map_info._DIRECTIONS.index(facing)
    except ValueError:
        return 0

    ray = map_info._GUNNER_RAYS[facing_idx]
    enemy_step = None
    for i, (dx, dy) in enumerate(ray):
        if site.x + dx == enemy.x and site.y + dy == enemy.y:
            enemy_step = i
            break
    if enemy_step is not None:
        saw_feed = False
        for dx, dy in ray[enemy_step + 1:]:
            tile = Position(site.x + dx, site.y + dy)
            if not map_info.in_bounds(tile):
                break
            tile_bit = _bit(tile)
            if tile_bit & feed:
                saw_feed = True
                continue
            if saw_feed and tile_bit & core:
                return 2
            if map_info._bm_env[map_info._IDX_ENV_WALL] & tile_bit:
                break
            if map_info._bm_any_building & tile_bit and not (tile_bit & core):
                break

    # Secondary preference: the gunner can rotate after killing the builder and
    # acquire a clean conveyor-then-core lane from the same placement tile.
    for di, ray in enumerate(map_info._GUNNER_RAYS):
        if di == facing_idx:
            continue
        saw_feed = False
        for dx, dy in ray:
            tile = Position(site.x + dx, site.y + dy)
            if not map_info.in_bounds(tile):
                break
            tile_bit = _bit(tile)
            if map_info._bm_env[map_info._IDX_ENV_WALL] & tile_bit:
                break
            if tile_bit & feed:
                saw_feed = True
                continue
            if saw_feed and tile_bit & core:
                return 1
            if map_info._bm_any_building & tile_bit:
                break
    return 0


def _gunner_plan(enemies: int, feed: int):
    """Pick a reachable gunner plan, maximizing future core-feed utility."""
    best = None
    best_quality = -1
    best_distance = None
    w = map_info._width
    for enemy in map_info.iter_mask(enemies):
        sites, facings = _sites_for_enemy(enemy)
        # Counter-builder gunners are home defense only. The attack bot used to
        # inherit this helper and place them beside the enemy core (for example
        # Duel's (8, 7)); enforce the same radius as anti_builder here too.
        sites = _sites_within_core_radius(sites, radius_sq=4)
        while sites:
            site, distance = nav.closest(sites)
            if site is None or distance < 0:
                break
            site_bit = _bit(site)
            sites &= ~site_bit
            if not _placement_has_safe_exit(site):
                continue
            facing = facings[site.x + site.y * w]
            quality = _ray_lane_quality(site, facing, enemy, feed)
            if (
                quality > best_quality
                or (
                    quality == best_quality
                    and (best_distance is None or distance < best_distance)
                )
            ):
                best_quality = quality
                best_distance = distance
                best = (enemy, site, facing)
    return best


def score() -> float:
    global target, gunner_site, gunner_facing, _approach, _mode
    target = None
    gunner_site = None
    gunner_facing = None
    _approach = None
    _mode = ""

    if not units.builder._atk_bot:
        return 0
    from units.atk_states import attack
    if not attack.has_placed_turret():
        return 0

    feed = _core_feed_conveyors()
    close_enemies = _nearby_unclaimed_builders()
    if close_enemies:
        plan = _gunner_plan(close_enemies, feed)
        if plan is not None:
            target, gunner_site, gunner_facing = plan
            _mode = "gunner"
            return MAX_SCORE
        return 0

    if feed:
        target, _approach = _closest_feed_approach(feed)
        if target is not None:
            _mode = "conveyor"
            return MAX_SCORE

    # A turret may be placed before the enemy core and its supply line enter
    # vision. Keep advancing toward the core instead of falling back to explore,
    # whose changing target set prevented stuck recovery and caused Runestone's
    # mid-map jitter. Ordinary useful gunner placement still outranks this low
    # score; enemy gunner rays remain forbidden during movement.
    if not units.builder.enemy_core_visible():
        target = units.builder.chip_target()
        if target is not None and target != map_info._my_pos:
            _mode = "advance"
            return ADVANCE_SCORE

    # No feed exists right now. Hold one stable cardinal tile beside the core so
    # a newly-built feed is immediately visible/attackable, and so we do not
    # bounce around explore's larger target ring beside our own turret.
    target = _core_staging_tile()
    if target is not None:
        _mode = "camp"
        return ADVANCE_SCORE
    return 0


def run() -> None:
    if target is None:
        return
    if _mode == "gunner":
        log("POST TURRET: COUNTER BUILDER", target, "via", gunner_site)
        if gunner_site is None or gunner_facing is None:
            return
        if map_info._my_pos.distance_squared(gunner_site) != 1:
            nav.move_adjacent(gunner_site, avoid_own_gunner=False)
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

    if _mode == "conveyor":
        log("POST TURRET: BREAK CORE FEED", target)
        if _approach is None:
            return
        if map_info._my_pos != _approach:
            nav.move_to(_approach, avoid_own_gunner=False)
            return
        if rc.can_fire(target):
            rc.fire(target)
            map_info.update_at(target)
        return

    if _mode == "advance":
        log("POST TURRET: ADVANCE", target)
        nav.move_to(target, avoid_own_gunner=False)
        return

    if _mode == "camp" and map_info._my_pos != target:
        log("POST TURRET: CORE STAGING", target)
        nav.move_to(target, avoid_own_gunner=False)
