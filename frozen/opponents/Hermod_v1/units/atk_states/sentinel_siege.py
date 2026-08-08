"""Use up to two launches, then place one safe enemy-core sentinel.

The core launcher supplies the opening throw. If that landing is not already
close enough, the attacker walks until one forward launcher can insert it into
an empty cardinal build stance beside the sentinel site. Once inserted, it
finishes on foot and never spends on another transport launcher.
"""

from fcode import Controller, Direction, EntityType, Position

import comms
import map_info
import units.builder
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 9
SENTINEL_LIMIT = 1

target: Position | None = None
target_facing: Direction | None = None
last_positions: list[Position] = []
_placed_locally = 0
_mode = ""
_blocking_builders = 0
_pending_launcher: Position | None = None
_pending_source: Position | None = None
_pending_round = -1
_insertion_complete = False
_committed_site: Position | None = None
_committed_facing: Direction | None = None
_sentinel_destroyed_latched = False

CARDINAL_DELTAS = ((0, -1), (1, 0), (0, 1), (-1, 0))


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def placed_count() -> int:
    return max(_placed_locally, comms.sentinel_count())


def complete() -> bool:
    return placed_count() >= SENTINEL_LIMIT


def sentinel_destroyed() -> bool:
    """True once our placed sentinel's tile is visibly empty of that sentinel."""
    global _sentinel_destroyed_latched
    if _sentinel_destroyed_latched:
        return True
    if not last_positions:
        return False
    my_sentinels = (
        map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[map_info._my_team_idx]
    )
    saw_recorded_tile = False
    for pos in last_positions:
        bit = _bit(pos.x, pos.y)
        if my_sentinels & bit:
            return False
        if map_info._bm_visible & bit:
            saw_recorded_tile = True
    if saw_recorded_tile:
        _sentinel_destroyed_latched = True
    return _sentinel_destroyed_latched


def _core_area() -> int:
    core = map_info._bm_their_core_area
    if core:
        return core
    origin = map_info._their_core or map_info._predicted_enemy_core
    if origin is None:
        return 0
    result = 0
    for x in (origin.x, origin.x + 1):
        for y in (origin.y, origin.y + 1):
            if 0 <= x < map_info._width and 0 <= y < map_info._height:
                result |= 1 << (x + y * map_info._width)
    return result


def _bit(x: int, y: int) -> int:
    return 1 << (x + y * map_info._width)


def _gunner_site_hits(gunner_x: int, gunner_y: int, sentinel_x: int, sentinel_y: int) -> bool:
    """Whether a newly placed gunner could face and shoot the sentinel site.

    Builder bots are deliberately ignored as line blockers: either builder can
    move on its next turn, so relying on a temporary body block would not make
    the sentinel defensible. Walls and existing structures remain real blockers.
    """
    w, h = map_info._width, map_info._height
    for di in range(8):
        for dx, dy in map_info._GUNNER_RAYS[di]:
            x, y = gunner_x + dx, gunner_y + dy
            if not (0 <= x < w and 0 <= y < h):
                break
            if x == sentinel_x and y == sentinel_y:
                return True
            bit = _bit(x, y)
            if (
                map_info._bm_env[map_info._IDX_ENV_WALL] & bit
                or map_info._bm_any_building & bit
            ):
                break
    return False


def _immediate_builder_counters(site_n: int) -> int:
    """Visible enemy builders able to place a gunner that attacks ``site_n``."""
    w, h = map_info._width, map_info._height
    sx, sy = site_n % w, site_n // w
    builders = map_info._bm_enemy_bots & map_info._bm_visible
    occupied = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
    )
    result = 0
    m = builders
    while m:
        builder_bit = m & -m
        bn = builder_bit.bit_length() - 1
        m ^= builder_bit
        bx, by = bn % w, bn // w
        for dx, dy in CARDINAL_DELTAS:
            gx, gy = bx + dx, by + dy
            if not (0 <= gx < w and 0 <= gy < h):
                continue
            gunner_bit = _bit(gx, gy)
            if occupied & gunner_bit or gunner_bit == (1 << site_n):
                continue
            if _gunner_site_hits(gx, gy, sx, sy):
                result |= builder_bit
                break
    return result


def _candidate_sites() -> tuple[int, dict[int, Direction], dict[int, int]]:
    """Return build sites, their best facing, and core tiles covered.

    Sentinel offsets already encode the engine's directional radius-squared-32
    footprint. No line-of-sight scan is performed because sentinels shoot over
    walls, bots, and buildings.
    """
    global _blocking_builders
    _blocking_builders = 0
    core = _core_area()
    if not core:
        return 0, {}, {}
    w, h = map_info._width, map_info._height
    occupied = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
        | map_info._bm_enemy_hard_threat
        | (1 << (map_info._my_pos.x + map_info._my_pos.y * w))
    )
    per_site: dict[int, tuple[int, Direction]] = {}
    m = core
    while m:
        lsb = m & -m
        core_n = lsb.bit_length() - 1
        m ^= lsb
        cx, cy = core_n % w, core_n // w
        for di, direction in enumerate(map_info._DIRECTIONS):
            for dx, dy in map_info._SENTINEL_OFFSETS[di]:
                sx, sy = cx - dx, cy - dy
                if not (0 <= sx < w and 0 <= sy < h):
                    continue
                n = sx + sy * w
                if occupied & (1 << n):
                    continue
                hits = 0
                for ox, oy in map_info._SENTINEL_OFFSETS[di]:
                    tx, ty = sx + ox, sy + oy
                    if 0 <= tx < w and 0 <= ty < h:
                        hits |= core & (1 << (tx + ty * w))
                old = per_site.get(n)
                if hits and (old is None or hits.bit_count() > old[0].bit_count()):
                    per_site[n] = (hits, direction)

    # Safety is evaluated before core-coverage and standoff optimization. A
    # closer two-tile shot is preferable to an outer shot the defender can
    # immediately counter with a gunner.
    safe_sites = {}
    for n, value in per_site.items():
        blockers = _immediate_builder_counters(n)
        _blocking_builders |= blockers
        if not blockers:
            safe_sites[n] = value
    per_site = safe_sites

    if not per_site:
        return 0, {}, {}
    core_tiles = []
    cm = core
    while cm:
        lsb = cm & -cm
        n = lsb.bit_length() - 1
        cm ^= lsb
        core_tiles.append((n % w, n // w))
    enemy_positions = list(map_info.iter_mask(map_info._bm_enemy_bots))
    other_bots = (
        map_info._bm_friendly_bots | map_info._bm_enemy_bots
    ) & ~_bit(map_info._my_pos.x, map_info._my_pos.y)

    def site_priority(n: int, hit_count: int) -> tuple[int, int, int, int]:
        sx, sy = n % w, n // w
        standoff = min(
            (sx - cx) ** 2 + (sy - cy) ** 2 for cx, cy in core_tiles
        )
        # Walls and map edges deny surrounding enemy-gunner placements.
        wall_cover = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                x, y = sx + dx, sy + dy
                if not (0 <= x < w and 0 <= y < h):
                    wall_cover += 1
                elif map_info._bm_env[map_info._IDX_ENV_WALL] & _bit(x, y):
                    wall_cover += 1
        enemy_clearance = min(
            ((sx - bot.x) ** 2 + (sy - bot.y) ** 2 for bot in enemy_positions),
            default=w * w + h * h,
        )
        return hit_count, standoff, wall_cover, enemy_clearance

    # Discard sites that have no cardinal build stance. The current builder's
    # own tile remains eligible, while all other occupied stances are rejected.
    usable = {}
    for n, value in per_site.items():
        sx, sy = n % w, n // w
        for dx, dy in CARDINAL_DELTAS:
            x, y = sx + dx, sy + dy
            if not (0 <= x < w and 0 <= y < h):
                continue
            bit = _bit(x, y)
            if (
                bit & map_info._bm_env[map_info._IDX_ENV_WALL]
                or bit & map_info._bm_any_building
                or bit & other_bots
            ):
                continue
            usable[n] = value
            break
    per_site = usable
    if not per_site:
        return 0, {}, {}

    # First preserve maximum core coverage, then prefer greater standoff, more
    # wall cover, and finally more clearance from visible opponent builders.
    best_priority = max(
        site_priority(n, hits.bit_count())
        for n, (hits, _direction) in per_site.items()
    )
    facings = {}
    coverage = {}
    sites = 0
    for n, (hits, direction) in per_site.items():
        if site_priority(n, hits.bit_count()) != best_priority:
            continue
        sites |= 1 << n
        facings[n] = direction
        coverage[n] = hits
    return sites, facings, coverage


def _site_pool() -> tuple[int, dict[int, Direction]]:
    """Best safe sites, spread away from the first allied sentinel."""
    sites, facings, _coverage = _candidate_sites()
    if not sites:
        return 0, facings
    my_sentinels = (
        map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[map_info._my_team_idx]
    )
    if my_sentinels:
        spread = sites & ~map_info.expand_chebyshev(my_sentinels, 2)
        if spread:
            sites = spread
    return sites, facings


def _landing_open(pos: Position) -> bool:
    """A builder may be launched here and then build cardinally from it."""
    if not map_info.in_bounds(pos):
        return False
    bit = _bit(pos.x, pos.y)
    return not bool(
        bit
        & (
            map_info._bm_env[map_info._IDX_ENV_WALL]
            | map_info._bm_any_building
            | map_info._bm_friendly_bots
            | map_info._bm_enemy_bots
        )
    )


def _launcher_tile_open(pos: Position) -> bool:
    """Static legality for a launcher site; can_build checks the final details."""
    if not map_info.in_bounds(pos):
        return False
    bit = _bit(pos.x, pos.y)
    return not bool(
        bit
        & (
            map_info._bm_env[map_info._IDX_ENV_WALL]
            | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
            | map_info._bm_any_building
            | map_info._bm_friendly_bots
            | map_info._bm_enemy_bots
        )
    )


def _launch_choices(
    launcher: Position,
    sites: int,
    controller: Controller | None = None,
) -> list[tuple[int, int, Position]]:
    """Legal empty landings beside a site and in this launcher's radius."""
    if not sites:
        return []
    c = controller or rc
    raw = {
        p.x + p.y * map_info._width
        for p in c.get_attackable_tiles_from(
            launcher, Direction.NORTH, EntityType.LAUNCHER
        )
    }
    launcher_n = launcher.x + launcher.y * map_info._width
    choices = []
    m = sites & ~(1 << launcher_n)
    while m:
        lsb = m & -m
        site_n = lsb.bit_length() - 1
        m ^= lsb
        sx, sy = site_n % map_info._width, site_n // map_info._width
        for dx, dy in CARDINAL_DELTAS:
            landing = Position(sx + dx, sy + dy)
            if not map_info.in_bounds(landing):
                continue
            landing_n = landing.x + landing.y * map_info._width
            if landing_n not in raw or not _landing_open(landing):
                continue
            # Prefer the shortest throw, then a stable site/landing ordering.
            choices.append((launcher.distance_squared(landing), site_n, landing))
    choices.sort(key=lambda item: (item[0], item[1], item[2].x, item[2].y))
    return choices


def launch_destination_from(
    launcher: Position,
    controller: Controller | None = None,
) -> Position | None:
    """Shared insertion geometry used by the builder and the real launcher."""
    sites, _facings = _site_pool()
    choices = _launch_choices(launcher, sites, controller)
    return choices[0][2] if choices else None


def launch_plan_from(
    launcher: Position,
    controller: Controller | None = None,
) -> tuple[Position, Position, Direction] | None:
    """Landing, sentinel site, and facing for a real forward launcher."""
    sites, facings = _site_pool()
    choices = _launch_choices(launcher, sites, controller)
    if not choices:
        return None
    _distance, site_n, landing = choices[0]
    site = Position(site_n % map_info._width, site_n // map_info._width)
    return landing, site, facings[site_n]


def _adjacent_sentinel_target(
    sites: int, facings: dict[int, Direction]
) -> tuple[Position | None, Direction | None]:
    my_pos = map_info._my_pos
    candidates = []
    m = sites
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        pos = Position(n % map_info._width, n // map_info._width)
        if my_pos.distance_squared(pos) == 1:
            candidates.append((n, pos))
    if not candidates:
        return None, None
    n, pos = min(candidates)
    return pos, facings[n]


def adjacent_sentinel_site() -> tuple[Position | None, Direction | None]:
    """Best currently buildable cardinal site for mirror-recovery fallback."""
    sites, facings = _site_pool()
    return _adjacent_sentinel_target(sites, facings)


def _adjacent_ready_launcher(sites: int) -> bool:
    launchers = (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    for pos in map_info.iter_mask(launchers):
        if max(
            abs(pos.x - map_info._my_pos.x),
            abs(pos.y - map_info._my_pos.y),
        ) <= 1 and _launch_choices(pos, sites):
            return True
    return False


def _staging_launcher(sites: int) -> Position | None:
    """Adjacent launcher site that can insert us beside a valid sentinel site."""
    choices = []
    for dx, dy in CARDINAL_DELTAS:
        launcher = Position(map_info._my_pos.x + dx, map_info._my_pos.y + dy)
        if not _launcher_tile_open(launcher):
            continue
        landings = _launch_choices(launcher, sites)
        if not landings:
            continue
        distance, site_n, landing = landings[0]
        choices.append((distance, site_n, launcher.x, launcher.y, launcher, landing))
    if not choices:
        return None
    return min(choices, key=lambda item: item[:4])[4]


def _launcher_pickup_mask() -> int:
    launchers = (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    return map_info.expand_chebyshev(launchers) if launchers else 0


def _choose_launcher_target(builders: int) -> Position | None:
    """Nearest buildable launcher tile in pickup range of a blocking builder."""
    if not builders:
        return None
    w, h = map_info._width, map_info._height
    builders &= ~_launcher_pickup_mask()  # already assigned to a live launcher
    if not builders:
        return None
    occupied = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
    )
    other_bots = (
        map_info._bm_friendly_bots | map_info._bm_enemy_bots
    ) & ~_bit(map_info._my_pos.x, map_info._my_pos.y)
    best = None
    best_key = None
    m = builders
    while m:
        lsb = m & -m
        bn = lsb.bit_length() - 1
        m ^= lsb
        bx, by = bn % w, bn // w
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                lx, ly = bx + dx, by + dy
                if not (0 <= lx < w and 0 <= ly < h):
                    continue
                launcher_bit = _bit(lx, ly)
                if occupied & launcher_bit:
                    continue
                stances = 0
                for sdx, sdy in CARDINAL_DELTAS:
                    x, y = lx + sdx, ly + sdy
                    if not (0 <= x < w and 0 <= y < h):
                        continue
                    pos = Position(x, y)
                    bit = _bit(x, y)
                    if map_info.is_passable(pos) and not (other_bots & bit):
                        stances |= bit
                if not stances:
                    continue
                _stance, distance = nav.closest(stances)
                if distance < 0:
                    continue
                key = (distance, max(abs(dx), abs(dy)), launcher_bit.bit_length())
                if best_key is None or key < best_key:
                    best_key = key
                    best = Position(lx, ly)
    return best


def _choose_target() -> tuple[Position | None, Direction | None]:
    sites, facings = _site_pool()
    if not sites:
        return None, None
    adjacent, facing = _adjacent_sentinel_target(sites, facings)
    if adjacent is not None:
        return adjacent, facing
    pos, _distance = nav.closest(sites)
    if pos is None:
        return None, None
    return pos, facings[pos.x + pos.y * map_info._width]


def score() -> int:
    global target, target_facing, _mode
    global _pending_launcher, _pending_source, _pending_round
    global _insertion_complete, _committed_site, _committed_facing
    target = None
    target_facing = None
    _mode = ""
    if not units.builder._atk_bot or complete():
        return 0

    # The launcher chose this site while evaluating the throw. Preserve that
    # one-turn decision even if a defender moves and changes the safety ranking
    # before the launched builder acts; otherwise retained launchers can bounce
    # the attacker forever between newly-ranked sites.
    handoff = comms.siege_insert(rc.get_id())
    if handoff is not None:
        handed_site, handed_facing = handoff
        if map_info._my_pos.distance_squared(handed_site) == 1:
            # Reaching a core-attacking build stance permanently retires
            # launcher construction. Any remaining sentinel is approached on
            # foot, even if this handed site becomes occupied before we build.
            _insertion_complete = True
            _pending_launcher = None
            _pending_source = None
            if _launcher_tile_open(handed_site):
                target = handed_site
                target_facing = handed_facing
                _mode = "sentinel"
                return MAX_SCORE

    # Once walking to the post-insertion sentinel, keep that strategic choice.
    # Re-ranking enemy clearance every turn otherwise makes a mirroring bot
    # pull the destination back and forth without ever letting us arrive.
    if _insertion_complete and _committed_site is not None:
        committed_bit = _bit(_committed_site.x, _committed_site.y)
        permanently_blocked = bool(
            committed_bit
            & (
                map_info._bm_env[map_info._IDX_ENV_WALL]
                | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
                | map_info._bm_any_building
            )
        )
        if not permanently_blocked:
            target = _committed_site
            target_facing = _committed_facing
            _mode = "sentinel"
            return MAX_SCORE
        _committed_site = None
        _committed_facing = None
    sites, facings = _site_pool()
    if not sites:
        return 0

    # A launcher has already inserted us when a valid sentinel tile is now
    # cardinally adjacent. Build immediately (or wait here for titanium).
    target, target_facing = _adjacent_sentinel_target(sites, facings)
    if target is not None:
        _mode = "sentinel"
        return MAX_SCORE

    # One successful insertion is enough. Walk to the closest remaining safe
    # core-attacking site instead of spending on a second launcher.
    if _insertion_complete:
        target, target_facing = _choose_target()
        if target is not None:
            _committed_site = target
            _committed_facing = target_facing
            _mode = "sentinel"
            return MAX_SCORE

    # The opening launcher counts as launch one and a forward insertion as
    # launch two. Once both are spent, finish the approach on foot.
    if comms.attack_launch_count() >= 2:
        target, target_facing = _choose_target()
        if target is not None:
            _mode = "sentinel"
            return MAX_SCORE

    # Wait only for the launcher this builder just constructed. Old one-shot
    # insertion launchers may remain nearby, but must never pin the builder.
    if _pending_launcher is not None:
        launcher_bit = _bit(_pending_launcher.x, _pending_launcher.y)
        launcher_alive = bool(
            launcher_bit
            & map_info._bm_et[map_info._IDX_LAUNCHER]
            & map_info._bm_team[map_info._my_team_idx]
        )
        if (
            map_info._my_pos == _pending_source
            and launcher_alive
            and rc.get_current_round() <= _pending_round + 2
        ):
            target = map_info._my_pos
            _mode = "launcher_wait"
            return MAX_SCORE
        _pending_launcher = None
        _pending_source = None

    # Spend on a launcher only when its real radius already contains an open
    # cardinal stance beside one of the currently valid sentinel sites.
    target = _staging_launcher(sites)
    if target is not None:
        _mode = "launcher"
        return MAX_SCORE

    # Until insertion is possible, simply advance toward the opponent core.
    target = (
        map_info._their_core
        or map_info._predicted_enemy_core
        or units.builder.atk_symmetry_target()
    )
    if target is not None:
        _mode = "walk"
        return MAX_SCORE
    return 0


def run() -> None:
    global _placed_locally, _pending_launcher, _pending_source, _pending_round
    log("SENTINEL SIEGE")
    if target is None or _mode == "launcher_wait":
        return
    if _mode == "walk":
        nav.move_to(target, avoid_turret=False, allow_enemy_gunner=True)
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
            _pending_launcher = target
            _pending_source = map_info._my_pos
            _pending_round = rc.get_current_round()
        return
    if target_facing is None:
        return
    if map_info._my_pos.distance_squared(target) != 1:
        nav.move_adjacent(target, avoid_turret=False)
        return
    if (
        rc.get_global_resources() >= rc.get_sentinel_cost()
        and rc.can_build_sentinel(target, target_facing)
    ):
        rc.build_sentinel(target, target_facing)
        _placed_locally += 1
        last_positions.append(target)
        comms.note_sentinel_built()
        map_info.update_at(target)
