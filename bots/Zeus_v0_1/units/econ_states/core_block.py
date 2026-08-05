"""Emergency defender: shield our core when a gunner is unaffordable.

The two economy builders are Zeus's home defenders after establishing their
routes. When the team cannot afford a gunner, this state finds visible enemy
gunner rays that reach the 2x2 core and chooses the barrier tile shared by the
largest number of those rays. The defender stays cardinally adjacent so it can
replace the barrier immediately after it is destroyed.
"""

from fcode import Controller, EntityType, Position

import map_info
import units.builder
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 10
target: Position | None = None

_camp_target: Position | None = None
_last_threat_round = -1000
_CAMP_MEMORY = 5


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _bit(pos: Position) -> int:
    return 1 << (pos.x + pos.y * map_info._width)


def _can_afford_gunner() -> bool:
    import units.atk_states.attack as attack

    reserve = max(map_info.builder_ti_reserve(), attack.GUNNER_TI_FLOOR)
    return rc.get_global_resources() >= rc.get_gunner_cost() + reserve


def _visible_enemy_gunners() -> int:
    enemy_idx = 1 - map_info._my_team_idx
    return (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[enemy_idx]
        & map_info._bm_visible
    )


def _line_block_tiles(gunner_n: int, direction_index: int) -> set[int]:
    """Structurally viable barrier cells before this ray reaches our core.

    Friendly barriers are treated as removable: their own tile remains a
    candidate and scanning continues so the defender keeps camping after the
    barrier absorbs a shot. Other buildings and walls permanently stop the ray.
    Builder bots are temporary and do not make a gunner safe, but their current
    tiles are not buildable this turn.
    """
    w, h = map_info._width, map_info._height
    gx, gy = gunner_n % w, gunner_n // w
    walls = map_info._bm_env[map_info._IDX_ENV_WALL]
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    my_barriers = (
        map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    bots = map_info._bm_friendly_bots | map_info._bm_enemy_bots
    candidates: set[int] = set()

    for dx, dy in map_info._GUNNER_RAYS[direction_index]:
        x, y = gx + dx, gy + dy
        if not (0 <= x < w and 0 <= y < h):
            return set()
        n = x + y * w
        bit = 1 << n
        if walls & bit:
            return set()
        if map_info._bm_my_core_area & bit:
            return candidates
        if map_info._bm_any_building & bit:
            if my_barriers & bit:
                if not map_info._bm_my_gunner_claims & bit:
                    candidates.add(n)
                continue
            return set()
        if not (ore | bots | map_info._bm_my_gunner_claims) & bit:
            candidates.add(n)
    return set()


def _best_candidate(coverage: dict[int, int], already_covered: int = 0) -> int | None:
    """Greedy maximum-coverage tile after another defender's assignment."""
    if not coverage:
        return None
    my_barriers = (
        map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    useful = {
        n: rays & ~already_covered
        for n, rays in coverage.items()
        if rays & ~already_covered
    }
    if not useful:
        return None

    # A barrier cell is only useful if this defender can safely reach a
    # cardinal build/repair stance beside it. This prevents repeatedly camping
    # an unreachable target on the far side of the incoming gunner ray.
    avoid = (
        map_info.get_avoid(False, False, False)
        & ~map_info._bm_enemy_hard_threat
    )
    route_distance = {}
    w, h = map_info._width, map_info._height
    my_bit = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    other_bots = (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & ~my_bit
    for n in tuple(useful):
        x, y = n % w, n // w
        stances = 0
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            sx, sy = x + dx, y + dy
            if not (0 <= sx < w and 0 <= sy < h):
                continue
            pos = Position(sx, sy)
            bit = 1 << (sx + sy * w)
            if map_info.is_passable(pos) and not bit & other_bots:
                stances |= bit
        _stance, distance = nav.closest(stances, avoid=avoid) if stances else (None, -1)
        if distance < 0:
            useful.pop(n)
        else:
            route_distance[n] = distance
    if not useful:
        return None
    return max(
        useful,
        key=lambda n: (
            useful[n].bit_count(),
            bool(my_barriers & (1 << n)),
            -route_distance[n],
            -n,
        ),
    )


def _choose_target() -> Position | None:
    global _last_threat_round
    gunners = _visible_enemy_gunners()
    coverage: dict[int, int] = {}
    dirs = map_info._building_dir
    m = gunners
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        di = dirs[n]
        if not (0 <= di < 8):
            continue
        gunner_bit = 1 << n
        for tile_n in _line_block_tiles(n, di):
            coverage[tile_n] = coverage.get(tile_n, 0) | gunner_bit

    if coverage:
        _last_threat_round = rc.get_current_round()
        w = map_info._width
        # Defender 0 takes the global maximum. Defender 1 greedily covers only
        # gunner rays not already handled by defender 0, preventing both economy
        # bots from camping the same barrier or two cells on the same lone ray.
        first = _best_candidate(coverage)
        econ_index = units.builder._economy_index or 0
        best_n = first
        if econ_index > 0 and first is not None:
            best_n = _best_candidate(coverage, coverage[first])
        if best_n is None:
            return None
        return Position(best_n % w, best_n // w)

    # Do not abandon the repair stance the instant the barrier hides the gunner
    # or a shot temporarily removes local vision.
    if (
        _camp_target is not None
        and rc.get_current_round() <= _last_threat_round + _CAMP_MEMORY
        and not (_bit(_camp_target) & map_info._bm_my_gunner_claims)
    ):
        return _camp_target
    return None


def score() -> int:
    global _camp_target, target
    if not units.builder._economy_builder or _can_afford_gunner():
        target = None
        return 0
    chosen = _choose_target()
    if chosen is None:
        target = None
        return 0
    _camp_target = chosen
    target = chosen
    return MAX_SCORE


def run() -> None:
    global target
    log("CORE BLOCK")
    if target is None:
        return

    bit = _bit(target)
    my_barrier = bool(
        bit
        & map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    adjacent = map_info._my_pos.distance_squared(target) == 1
    if not adjacent:
        nav.move_adjacent(
            target,
            avoid_turret=False,
            allow_enemy_gunner=True,
        )
        return

    if my_barrier:
        if bit & map_info._bm_damaged and rc.can_heal(target):
            rc.heal(target)
        return

    # This is explicitly the low-titanium fallback. Spend the barrier cost as
    # soon as it is available rather than reserving funds for the unaffordable
    # gunner we are replacing.
    if rc.get_global_resources() >= rc.get_barrier_cost() and rc.can_build_barrier(target):
        rc.build_barrier(target)
        map_info.update_at(target)
