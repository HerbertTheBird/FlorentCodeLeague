"""After the siege is placed, shield its sentinels from enemy gunners."""

from fcode import Controller, EntityType, Position

import map_info
import units.builder
import units.atk_states.sentinel_siege as siege
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 10
target: Position | None = None
_threatened = False
_camp_target: Position | None = None
_last_threat_round = -1000
_CAMP_MEMORY = 20
_mode = ""
CLOSE_BUILDER_RADIUS_SQ = 16


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _barrier_coverage() -> dict[int, int]:
    my_idx = map_info._my_team_idx
    enemy_idx = 1 - my_idx
    my_sentinels = (
        map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[my_idx]
    )
    enemy_gunners = (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[enemy_idx]
        & map_info._bm_visible
    )
    if not my_sentinels or not enemy_gunners:
        return {}

    walls = map_info._bm_env[map_info._IDX_ENV_WALL]
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    bots = map_info._bm_friendly_bots | map_info._bm_enemy_bots
    my_barriers = (
        map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[my_idx]
    )
    dirs = map_info._building_dir
    coverage: dict[int, int] = {}
    w, h = map_info._width, map_info._height

    m = enemy_gunners
    while m:
        lsb = m & -m
        gn = lsb.bit_length() - 1
        m ^= lsb
        di = dirs[gn]
        if not (0 <= di < 8):
            continue
        gx, gy = gn % w, gn // w
        candidates = []
        reaches_sentinel = False
        for dx, dy in map_info._GUNNER_RAYS[di]:
            x, y = gx + dx, gy + dy
            if not (0 <= x < w and 0 <= y < h):
                break
            n = x + y * w
            bit = 1 << n
            if walls & bit:
                break
            if my_sentinels & bit:
                reaches_sentinel = True
                break
            if map_info._bm_any_building & bit:
                if my_barriers & bit:
                    candidates.append(n)
                    continue
                break
            if not (ore | bots | map_info._bm_my_gunner_claims) & bit:
                candidates.append(n)
        if reaches_sentinel:
            for n in candidates:
                coverage[n] = coverage.get(n, 0) | lsb
    return coverage


def _best_barrier(
    coverage: dict[int, int], preferred: Position | None = None
) -> Position | None:
    if not coverage:
        return None
    if preferred is not None:
        preferred_n = preferred.x + preferred.y * map_info._width
        # Do not abandon the repair stance merely because the barrier vanished
        # and another cell now wins an arbitrary tie. If this cell still blocks
        # a sentinel shot, rebuilding it is the highest-priority action.
        if preferred_n in coverage:
            return preferred
    my_barriers = (
        map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    n = max(
        coverage,
        key=lambda tile_n: (
            coverage[tile_n].bit_count(),
            bool(my_barriers & (1 << tile_n)),
            -tile_n,
        ),
    )
    return Position(n % map_info._width, n // map_info._width)


def _camp_tile_available(pos: Position) -> bool:
    """Whether a remembered barrier can still exist on this tile."""
    if not map_info.in_bounds(pos):
        return False
    bit = 1 << (pos.x + pos.y * map_info._width)
    if (
        map_info._bm_env[map_info._IDX_ENV_WALL] & bit
        or map_info._bm_env[map_info._IDX_ENV_ORE_TI] & bit
    ):
        return False
    if not (map_info._bm_any_building & bit):
        return True
    return bool(
        bit
        & map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )


def _standby_target() -> Position | None:
    my_sentinels = (
        map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[map_info._my_team_idx]
    )
    if my_sentinels:
        zone = map_info.expand_manhattan(my_sentinels) & map_info._bm_passable_FFF
        my_bit = 1 << (
            map_info._my_pos.x + map_info._my_pos.y * map_info._width
        )
        other_bots = (
            map_info._bm_friendly_bots | map_info._bm_enemy_bots
        ) & ~my_bit
        zone &= ~other_bots
        pos, _distance = nav.closest(zone)
        if pos is not None:
            return pos
    for pos in reversed(siege.last_positions):
        if map_info.in_bounds(pos):
            return pos
    return None


def _close_enemy_builders() -> int:
    my_sentinels = (
        map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[map_info._my_team_idx]
    )
    if not my_sentinels:
        return 0
    result = 0
    my_team = rc.get_team()
    sentinel_positions = list(map_info.iter_mask(my_sentinels))
    for entity_id in rc.get_nearby_units():
        if (
            rc.get_entity_type(entity_id) != EntityType.BUILDER_BOT
            or rc.get_team(entity_id) == my_team
        ):
            continue
        enemy = rc.get_position(entity_id)
        if min(enemy.distance_squared(s) for s in sentinel_positions) <= CLOSE_BUILDER_RADIUS_SQ:
            result |= 1 << (enemy.x + enemy.y * map_info._width)
    return result


def _proactive_barrier() -> Position | None:
    """Closest empty tile in the 3x3 ring around our sentinel."""
    sentinels = (
        map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[map_info._my_team_idx]
    )
    if not sentinels:
        return None
    candidates = map_info.expand_chebyshev(sentinels) & ~sentinels
    candidates &= ~(
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
        | map_info._bm_my_gunner_claims
    )
    if not candidates:
        return None
    pos, _distance = nav.closest(candidates)
    return pos


def score() -> int:
    global target, _threatened, _camp_target, _last_threat_round, _mode
    target = None
    _threatened = False
    _mode = ""
    if (
        not units.builder._atk_bot
        or not siege.complete()
        or siege.sentinel_destroyed()
    ):
        return 0

    close_builders = _close_enemy_builders()
    if close_builders:
        target = siege._choose_launcher_target(close_builders)
        if target is not None:
            _mode = "launcher"
            return MAX_SCORE
    coverage = _barrier_coverage()
    if coverage:
        _last_threat_round = rc.get_current_round()
    target = _best_barrier(coverage, _camp_target)
    if target is not None:
        _camp_target = target
        _threatened = True
        _mode = "barrier"
        return MAX_SCORE
    # A gunner can temporarily disappear from vision or have its shot hidden by
    # an update boundary on the same round it destroys the barrier. Hold the
    # remembered tile long enough to replace/repair it instead of walking back
    # to generic sentinel standby immediately.
    if (
        _camp_target is not None
        and rc.get_current_round() <= _last_threat_round + _CAMP_MEMORY
        and _camp_tile_available(_camp_target)
    ):
        target = _camp_target
        _threatened = True
        _mode = "barrier"
        return MAX_SCORE
    if rc.get_current_round() > _last_threat_round + _CAMP_MEMORY:
        _camp_target = None
    target = _proactive_barrier()
    if target is not None:
        _mode = "barrier"
        return MAX_SCORE
    target = _standby_target()
    _mode = "standby"
    return 3 if target is not None else 0


def run() -> None:
    log("SENTINEL GUARD")
    if target is None:
        return
    if _mode == "launcher":
        if map_info._my_pos.distance_squared(target) != 1:
            nav.move_adjacent(target, avoid_turret=False, allow_enemy_gunner=True)
            return
        if (
            rc.get_global_resources() >= rc.get_launcher_cost()
            and rc.can_build_launcher(target)
        ):
            rc.build_launcher(target)
            map_info.update_at(target)
        return
    if not _threatened:
        if _mode == "standby":
            if map_info._my_pos != target:
                nav.move_to(target)
            return

    bit = 1 << (target.x + target.y * map_info._width)
    my_barrier = bool(
        bit
        & map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    if map_info._my_pos.distance_squared(target) != 1:
        nav.move_adjacent(target, avoid_turret=False, allow_enemy_gunner=True)
        return
    if my_barrier:
        if bit & map_info._bm_damaged and rc.can_heal(target):
            rc.heal(target)
        return
    if rc.get_global_resources() >= rc.get_barrier_cost() and rc.can_build_barrier(target):
        rc.build_barrier(target)
        map_info.update_at(target)
