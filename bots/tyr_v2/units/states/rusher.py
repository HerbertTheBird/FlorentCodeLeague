"""Direct sentinel rush performed by exactly one designated builder."""

import map_info
import units.builder
from fcode import Controller, EntityType, Position
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

# Only an active siege at our core (defend.SIEGE_SCORE == 20) may interrupt the
# rush. This otherwise outranks cut and every economic or ordinary attack job.
MAX_SCORE = 14

# Set by builder.py on the first turn of the builder that exists in round 1.
am_rusher = False

_finished = False
_cached_tiers: tuple = ()


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _core_mask() -> int:
    """Known enemy footprint, falling back to the current symmetry prediction."""
    if map_info._bm_their_core_area:
        return map_info._bm_their_core_area
    core = map_info._their_core or map_info._predicted_enemy_core
    if core is None:
        return 0
    mask = 0
    w = map_info._width
    for y in range(core.y, core.y + 2):
        for x in range(core.x, core.x + 2):
            if map_info.in_bounds_coords(x, y):
                mask |= 1 << (x + y * w)
    return mask


def _fires_on_core(site: Position, facing_idx: int, core: int) -> bool:
    """Whether this sentinel ray contains at least one enemy-core tile."""
    w = map_info._width
    for dx, dy in map_info._SENTINEL_OFFSETS[facing_idx]:
        x = site.x + dx
        y = site.y + dy
        if map_info.in_bounds_coords(x, y) and core & (1 << (x + y * w)):
            return True
    return False


def _observed_planted_sentinel(core: int) -> bool:
    mine = (map_info._bm_et[map_info._IDX_SENTINEL]
            & map_info._bm_team[map_info._my_team_idx])
    w = map_info._width
    while mine:
        bit = mine & -mine
        mine ^= bit
        n = bit.bit_length() - 1
        facing_idx = map_info._building_dir[n]
        if 0 <= facing_idx < 8 and _fires_on_core(
                Position(n % w, n // w), facing_idx, core):
            return True
    return False


def _distance_from_core(site: Position, core_positions: tuple[Position, ...]) -> int:
    return min(site.distance_squared(tile) for tile in core_positions)


def _candidate_tiers(core: int) -> tuple:
    """Firing sites grouped farthest-first, with deterministic site/facing order."""
    w = map_info._width
    core_positions = tuple(map_info.iter_mask(core))
    blocked = map_info._bm_any_building | map_info._bm_env[map_info._IDX_ENV_WALL]
    by_site: dict[int, tuple[int, int]] = {}

    # Work backwards from each core tile along every legal sentinel ray. This
    # includes diagonal facings (four steps) as well as cardinal ones (five).
    for facing_idx, offsets in enumerate(map_info._SENTINEL_OFFSETS):
        for dx, dy in offsets:
            for target in core_positions:
                x = target.x - dx
                y = target.y - dy
                if not map_info.in_bounds_coords(x, y):
                    continue
                n = x + y * w
                bit = 1 << n
                if (core | blocked) & bit:
                    continue
                site = Position(x, y)
                distance = _distance_from_core(site, core_positions)
                prior = by_site.get(n)
                # One site can hit multiple footprint tiles/facings. Preserve
                # the lowest facing index as a stable build-direction tiebreak.
                if prior is None or facing_idx < prior[1]:
                    by_site[n] = (distance, facing_idx)

    by_distance: dict[int, list[tuple[Position, int]]] = {}
    for n, (distance, facing_idx) in by_site.items():
        by_distance.setdefault(distance, []).append(
            (Position(n % w, n // w), facing_idx))

    tiers = []
    for distance in sorted(by_distance, reverse=True):
        candidates = tuple(sorted(
            by_distance[distance],
            key=lambda item: (item[0].x + item[0].y * w, item[1]),
        ))
        tiers.append((distance, candidates))
    return tuple(tiers)


def score(can_move=True):
    if not can_move:
        return 0
    global _cached_tiers, _finished
    _cached_tiers = ()
    if not am_rusher or _finished:
        return 0

    core = _core_mask()
    if not core:
        return 0
    if _observed_planted_sentinel(core):
        _finished = True
        units.builder._econ_only = True
        return 0

    _cached_tiers = _candidate_tiers(core)
    return MAX_SCORE if _cached_tiers else 0


def _stand_tiles(candidates: tuple[tuple[Position, int], ...]) -> set[Position]:
    """Passable cardinal neighbours from which a candidate can be built."""
    result = set()
    for site, _facing_idx in candidates:
        for direction in map_info._CARDINAL:
            stand = map_info.pos_add(site, direction)
            if map_info.in_bounds(stand) and map_info.is_passable(stand):
                result.add(stand)
    return result


def _engine_confirms_ray(site: Position, facing, core: int) -> bool:
    """Final engine-level geometry check before committing titanium."""
    for target in map_info.iter_mask(core):
        if rc.can_fire_from(site, facing, EntityType.SENTINEL, target):
            return True
    return False


def run(can_move=True):
    if not can_move:
        return
    global _finished
    if not _cached_tiers:
        return

    my_pos = map_info._my_pos
    core = _core_mask()
    sentinel_cost = rc.get_sentinel_cost()
    can_afford = rc.get_global_resources() >= sentinel_cost

    # "As soon as possible" wins over the distance preference: if a legal site
    # is buildable this turn, plant the sentinel now, choosing the farthest such
    # site. Only when nothing can be built immediately do we walk toward the
    # farthest viable tier.
    if can_afford and rc.get_action_cooldown() == 0:
        for distance, candidates in _cached_tiers:
            adjacent = [item for item in candidates
                        if abs(item[0].x - my_pos.x) + abs(item[0].y - my_pos.y) == 1]
            for site, facing_idx in adjacent:
                facing = map_info._DIRECTIONS[facing_idx]
                if (_engine_confirms_ray(site, facing, core)
                        and rc.can_build_sentinel(site, facing)):
                    log(f"RUSHER sentinel d2={distance} at {site} facing {facing}")
                    rc.build_sentinel(site, facing)
                    map_info.update_at(site)
                    _finished = True
                    units.builder._econ_only = True
                    return

    for distance, candidates in _cached_tiers:
        adjacent = [item for item in candidates
                    if abs(item[0].x - my_pos.x) + abs(item[0].y - my_pos.y) == 1]
        if adjacent:
            # Hold a legal build stance while saving titanium or waiting for an
            # action cooldown; leaving would only delay the earliest build.
            if not can_afford or rc.get_action_cooldown() != 0:
                return

        stands = _stand_tiles(candidates)
        # If an occupied candidate beside us cannot be built, continue toward
        # another stance in the same preferred tier instead of idling forever.
        if adjacent:
            stands.discard(my_pos)
        if stands and nav.move_to(stands):
            return
