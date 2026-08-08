"""Economy defender: interpose barriers in gunner lines aimed at our core.

Barrier coverage is maximized across all visible threatening gunners. If a ray
reaches the core but contains no legal barrier tile, one economy builder stages
a gunner that can shoot the aggressor instead.
"""

from fcode import Controller, Direction, EntityType, Position

import comms
import map_info
import units.builder
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 12
target: Position | None = None
_mode = ""
_gunner_facing: Direction | None = None
_camp_target: Position | None = None
_last_threat_round = -1000
_CAMP_MEMORY = 20


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _bit(pos: Position) -> int:
    return 1 << (pos.x + pos.y * map_info._width)


def _visible_enemy_gunners() -> int:
    enemy_idx = 1 - map_info._my_team_idx
    return (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[enemy_idx]
        & map_info._bm_visible
    )


def _ray_barrier_tiles(gunner_n: int, direction_index: int) -> tuple[bool, set[int]]:
    """Whether this ray reaches our core, plus legal interposition cells."""
    w, h = map_info._width, map_info._height
    gx, gy = gunner_n % w, gunner_n // w
    walls = map_info._bm_env[map_info._IDX_ENV_WALL]
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    bots = map_info._bm_friendly_bots | map_info._bm_enemy_bots
    my_barriers = (
        map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    candidates: set[int] = set()

    for dx, dy in map_info._GUNNER_RAYS[direction_index]:
        x, y = gx + dx, gy + dy
        if not (0 <= x < w and 0 <= y < h):
            return False, set()
        n = x + y * w
        bit = 1 << n
        if walls & bit:
            return False, set()
        if map_info._bm_my_core_area & bit:
            return True, candidates
        if map_info._bm_any_building & bit:
            if my_barriers & bit:
                if not map_info._bm_my_gunner_claims & bit:
                    candidates.add(n)
                continue
            return False, set()
        if not (ore | bots | map_info._bm_my_gunner_claims) & bit:
            candidates.add(n)
    return False, set()


def _threat_data() -> tuple[dict[int, int], list[int]]:
    coverage: dict[int, int] = {}
    unbarrierable = []
    dirs = map_info._building_dir
    m = _visible_enemy_gunners()
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        di = dirs[n]
        if not (0 <= di < 8):
            continue
        reaches_core, candidates = _ray_barrier_tiles(n, di)
        if not reaches_core:
            continue
        if not candidates:
            unbarrierable.append(n)
            continue
        for tile_n in candidates:
            coverage[tile_n] = coverage.get(tile_n, 0) | lsb
    return coverage, unbarrierable


def _reachable_barrier_candidates(coverage: dict[int, int]) -> dict[int, int]:
    if not coverage:
        return {}
    w, h = map_info._width, map_info._height
    my_bit = _bit(map_info._my_pos)
    other_bots = (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & ~my_bit
    reachable = {}
    for n, rays in coverage.items():
        x, y = n % w, n // w
        stances = 0
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            sx, sy = x + dx, y + dy
            if not (0 <= sx < w and 0 <= sy < h):
                continue
            pos = Position(sx, sy)
            bit = 1 << (sx + sy * w)
            if map_info.is_passable(pos) and not (other_bots & bit):
                stances |= bit
        if stances and nav.closest(
            stances,
            avoid=map_info.get_avoid(False, False, False) & ~map_info._bm_enemy_hard_threat,
        )[1] >= 0:
            reachable[n] = rays
    return reachable


def _best_barrier(
    coverage: dict[int, int],
    already_covered: int = 0,
    preferred: Position | None = None,
) -> int | None:
    useful = {
        n: rays & ~already_covered
        for n, rays in _reachable_barrier_candidates(coverage).items()
        if rays & ~already_covered
    }
    if not useful:
        return None
    if preferred is not None:
        preferred_n = preferred.x + preferred.y * map_info._width
        # Persist the assignment across the destruction frame. Otherwise the
        # "existing barrier" tiebreak disappears with the structure and the
        # defender walks to another equally good ray cell instead of rebuilding.
        if preferred_n in useful:
            return preferred_n
    my_barriers = (
        map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    return max(
        useful,
        key=lambda n: (
            useful[n].bit_count(),
            bool(my_barriers & (1 << n)),
            -n,
        ),
    )


def _facing_to_enemy(site: Position, enemy: Position) -> Direction | None:
    for di, direction in enumerate(map_info._DIRECTIONS):
        for dx, dy in map_info._GUNNER_RAYS[di]:
            pos = Position(site.x + dx, site.y + dy)
            if not map_info.in_bounds(pos):
                break
            if pos == enemy:
                return direction
            bit = _bit(pos)
            if (
                map_info._bm_env[map_info._IDX_ENV_WALL] & bit
                or map_info._bm_any_building & bit
            ):
                break
    return None


def _gunner_counter(gunner_n: int) -> tuple[Position | None, Direction | None]:
    w, h = map_info._width, map_info._height
    enemy = Position(gunner_n % w, gunner_n // w)
    occupied = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
        | map_info._bm_my_gunner_claims
        | _bit(map_info._my_pos)
    )
    sites = 0
    facings = {}
    for y in range(max(0, enemy.y - 3), min(h, enemy.y + 4)):
        for x in range(max(0, enemy.x - 3), min(w, enemy.x + 4)):
            pos = Position(x, y)
            bit = _bit(pos)
            if occupied & bit:
                continue
            facing = _facing_to_enemy(pos, enemy)
            if facing is not None:
                sites |= bit
                facings[x + y * w] = facing
    pos, _distance = nav.closest(sites) if sites else (None, -1)
    if pos is None:
        return None, None
    return pos, facings[pos.x + pos.y * w]


def score() -> int:
    global target, _mode, _gunner_facing, _camp_target, _last_threat_round
    target = None
    _mode = ""
    _gunner_facing = None
    if not units.builder._economy_builder:
        return 0

    coverage, unbarrierable = _threat_data()
    if coverage or unbarrierable:
        _last_threat_round = rc.get_current_round()

    first = _best_barrier(coverage)
    econ_index = units.builder._economy_index or 0
    if econ_index == 0:
        chosen = _best_barrier(coverage, preferred=_camp_target)
    elif first is not None:
        chosen = _best_barrier(
            coverage, coverage[first], preferred=_camp_target
        )
    else:
        chosen = None
    if chosen is not None:
        target = Position(chosen % map_info._width, chosen // map_info._width)
        _camp_target = target
        _mode = "barrier"
        return MAX_SCORE

    # Assign unbarrierable rays by defender index so both builders do not place
    # redundant gunners against the same aggressor.
    counter_slot = max(0, econ_index - (1 if first is not None else 0))
    if counter_slot < len(unbarrierable):
        target, _gunner_facing = _gunner_counter(unbarrierable[counter_slot])
        if target is not None:
            _mode = "gunner"
            return MAX_SCORE

    if (
        _camp_target is not None
        and rc.get_current_round() <= _last_threat_round + _CAMP_MEMORY
        and not (_bit(_camp_target) & map_info._bm_my_gunner_claims)
    ):
        target = _camp_target
        _mode = "barrier"
        return MAX_SCORE
    return 0


def run() -> None:
    log("BASE GUARD")
    if target is None:
        return
    if map_info._my_pos.distance_squared(target) != 1:
        nav.move_adjacent(target, avoid_turret=False, allow_enemy_gunner=True)
        return

    if _mode == "gunner":
        if (
            _gunner_facing is not None
            and rc.get_global_resources() >= rc.get_gunner_cost()
            and rc.can_build_gunner(target, _gunner_facing)
        ):
            rc.build_gunner(target, _gunner_facing)
            comms.note_gunner_built()
            map_info.update_at(target)
        return

    bit = _bit(target)
    my_barrier = bool(
        bit
        & map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    if my_barrier:
        if bit & map_info._bm_damaged and rc.can_heal(target):
            rc.heal(target)
        return
    if rc.get_global_resources() >= rc.get_barrier_cost() and rc.can_build_barrier(target):
        rc.build_barrier(target)
        map_info.update_at(target)
