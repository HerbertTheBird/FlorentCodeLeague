"""Compact launcher-and-mirror defense for Heimdall v3.

One opening defender builds at most two launchers, all within distance three of
one of our 2x2 core tiles.  The chosen launcher directions cover the currently
possible enemy-core symmetries; while all three symmetries remain possible the
rotational, centre-facing option is omitted.  Once launched to an intruder, a
defender copies that intruder's cardinal movement exactly.  Losing the intruder
releases the claim and sends the defender back beside its launch launcher.
"""

from fcode import Controller, Direction, EntityType, Position

import comms
import map_info
from pathing import Pathing


rc: Controller = None
nav: Pathing = None
target: Position | None = None

_launchers_placed = 0
_first_launcher_site: Position | None = None
_first_launcher_id = 0
_wait_round = -1
_wait_position: Position | None = None
_return_launch_position: Position | None = None
_mirror_enemy_position: Position | None = None
_mirror_enemy_id = 0
_return_home: Position | None = None
_planned_sites: list[Position] | None = None

_CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)


def init(c: Controller, pathfinder: Pathing) -> None:
    global rc, nav, target, _launchers_placed
    global _first_launcher_site, _first_launcher_id
    global _wait_round, _wait_position, _return_launch_position
    global _mirror_enemy_position, _mirror_enemy_id, _return_home, _planned_sites
    rc = c
    nav = pathfinder
    target = None
    _launchers_placed = 0
    _first_launcher_site = None
    _first_launcher_id = 0
    _wait_round = -1
    _wait_position = None
    _return_launch_position = None
    _mirror_enemy_position = None
    _mirror_enemy_id = 0
    _return_home = None
    _planned_sites = None


def _bit(pos: Position) -> int:
    return 1 << (pos.x + pos.y * map_info._width)


def _core_distance_sq(pos: Position) -> int:
    core = map_info._my_core
    return min(
        (pos.x - x) * (pos.x - x) + (pos.y - y) * (pos.y - y)
        for x in (core.x, core.x + 1)
        for y in (core.y, core.y + 1)
    )


def _site_open(pos: Position) -> bool:
    if not map_info.in_bounds(pos):
        return False
    bit = _bit(pos)
    return not bool((
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
    ) & bit)


def _possible_symmetry_targets() -> list[tuple[str, Position]]:
    core = map_info._my_core
    if core is None:
        return []
    possible = []
    if map_info._hor_sym:
        possible.append(("horizontal", map_info.hor_flip_core(core)))
    if map_info._ver_sym:
        possible.append(("vertical", map_info.ver_flip_core(core)))
    if map_info._rot_sym:
        possible.append(("rotational", map_info.rot_flip_core(core)))
    # A symmetry that maps the core footprint onto itself cannot contain the
    # opposing core. Remove it before applying the three-way centre omission.
    possible = [
        item for item in possible
        if abs(item[1].x - core.x) > 1 or abs(item[1].y - core.y) > 1
    ]
    if len(possible) == 3:
        # Rotational symmetry points through the map centre.  The two axial
        # possibilities give wider launcher vision while all three remain live.
        possible = [item for item in possible if item[0] != "rotational"]
    unique = []
    seen_targets = set()
    for item in possible:
        key = (item[1].x, item[1].y)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        unique.append(item)
    return unique[:2]


def _has_core_spawn_stance(site: Position) -> bool:
    """Whether a cardinal build stance is in the core's legal spawn radius."""
    core = map_info._my_core
    for direction in _CARDINALS:
        stance = map_info.pos_add(site, direction)
        if not map_info.in_bounds(stance):
            continue
        if core.x <= stance.x <= core.x + 1 and core.y <= stance.y <= core.y + 1:
            continue
        if _core_distance_sq(stance) > 2:
            continue
        bit = _bit(stance)
        if (
            map_info._bm_env[map_info._IDX_ENV_WALL]
            | map_info._bm_any_building
        ) & bit:
            continue
        return True
    return False


def _best_site_toward(
    destination: Position,
    reserved: set[Position],
    require_spawn_stance: bool = False,
) -> Position | None:
    """Furthest legal core-local launcher site along ``destination``'s ray."""
    core = map_info._my_core
    if core is None:
        return None
    origin_x = core.x + 0.5
    origin_y = core.y + 0.5
    vx = destination.x + 0.5 - origin_x
    vy = destination.y + 0.5 - origin_y
    if vx == 0 and vy == 0:
        vx = map_info._width / 2 - origin_x
        vy = map_info._height / 2 - origin_y

    candidates = []
    for y in range(max(0, core.y - 3), min(map_info._height, core.y + 5)):
        for x in range(max(0, core.x - 3), min(map_info._width, core.x + 5)):
            pos = Position(x, y)
            if pos in reserved or _core_distance_sq(pos) > 9 or not _site_open(pos):
                continue
            if require_spawn_stance and not _has_core_spawn_stance(pos):
                continue
            dx = x - origin_x
            dy = y - origin_y
            projection = dx * vx + dy * vy
            if projection <= 0:
                continue
            # Maximize distance from the core first, then ray alignment and
            # forward projection.  Cross-product magnitude is alignment error.
            alignment_error = abs(dx * vy - dy * vx)
            candidates.append((
                _core_distance_sq(pos),
                -alignment_error,
                projection,
                -(x + y * map_info._width),
                pos,
            ))
    return max(candidates)[-1] if candidates else None


def launcher_plan() -> list[Position]:
    """Return one or two distinct symmetry-facing launcher sites."""
    global _planned_sites
    if _planned_sites is not None:
        return list(_planned_sites)
    core = map_info._my_core
    if core is None:
        return []
    targets = _possible_symmetry_targets()
    if not targets:
        targets = [("centre", Position(map_info._width // 2, map_info._height // 2))]

    sites: list[Position] = []
    reserved: set[Position] = set()
    for index, (_name, destination) in enumerate(targets):
        site = _best_site_toward(
            destination, reserved, require_spawn_stance=(index == 0)
        )
        if site is None and index == 0:
            # Some wall/edge layouts have no spawn-seeded ray site; retain the
            # general legal fallback rather than failing the minimum launcher.
            site = _best_site_toward(destination, reserved)
        if site is None:
            continue
        sites.append(site)
        reserved.add(site)

    # Map edges or walls can invalidate every symmetry ray.  Keep the promised
    # minimum of one launcher by searching toward centre, then any legal tile.
    if not sites:
        centre = Position(map_info._width // 2, map_info._height // 2)
        fallback = _best_site_toward(centre, reserved)
        if fallback is None:
            legal = []
            for y in range(max(0, core.y - 3), min(map_info._height, core.y + 5)):
                for x in range(max(0, core.x - 3), min(map_info._width, core.x + 5)):
                    pos = Position(x, y)
                    if _core_distance_sq(pos) <= 9 and _site_open(pos):
                        legal.append((_core_distance_sq(pos), -(x + y * map_info._width), pos))
            fallback = max(legal)[-1] if legal else None
        if fallback is not None:
            sites.append(fallback)
    _planned_sites = sites[:2]
    return list(_planned_sites)


def next_launcher_site(lane: int = 0) -> Position | None:
    global target
    if lane != 0:
        target = None
        return None
    plan = launcher_plan()
    target = plan[_launchers_placed] if _launchers_placed < len(plan) else None
    return target


def setup_complete() -> bool:
    return _launchers_placed >= len(launcher_plan()) and _launchers_placed > 0


def _move_into_build_range(site: Position) -> bool:
    destinations = set()
    for direction in _CARDINALS:
        stance = map_info.pos_add(site, direction)
        if stance == map_info._my_pos:
            return False
        if not map_info.in_bounds(stance) or not map_info.is_passable(stance):
            continue
        if rc.is_in_vision(stance) and rc.get_tile_builder_bot_id(stance):
            continue
        destinations.add(stance)
    return bool(destinations) and nav.move_to(destinations, avoid_turret=False)


def _build_launcher(site: Position) -> bool:
    global _launchers_placed, _first_launcher_site, _first_launcher_id
    global _wait_round, _wait_position, _return_launch_position
    if abs(site.x - map_info._my_pos.x) + abs(site.y - map_info._my_pos.y) != 1:
        return False
    if not _site_open(site) or not rc.can_build_launcher(site):
        return False
    if rc.get_global_resources() < rc.get_launcher_cost() + map_info.builder_ti_reserve():
        return False

    launcher_id = rc.build(EntityType.LAUNCHER, site)
    _launchers_placed += 1
    if _first_launcher_id == 0:
        _first_launcher_id = launcher_id
        _first_launcher_site = site
    map_info.update_at(site)
    next_site = next_launcher_site(0)
    return_home = next_site is None
    handoff_site = _first_launcher_site if return_home else next_site
    comms.publish_launcher_handoff(
        0, rc.get_id(), launcher_id, handoff_site, return_home=return_home
    )
    if handoff_site is not None:
        _wait_round = rc.get_current_round() + 1
        _wait_position = map_info._my_pos
    if return_home:
        _return_launch_position = map_info._my_pos
    return True


def coreward_block_tile(enemy: Position) -> Position:
    """Block the enemy's dominant cardinal offset toward our 2x2 core."""
    core = map_info._my_core
    nearest_x = min(max(enemy.x, core.x), core.x + 1)
    nearest_y = min(max(enemy.y, core.y), core.y + 1)
    dx = enemy.x - nearest_x
    dy = enemy.y - nearest_y
    if abs(dx) >= abs(dy):
        step = 0 if dx == 0 else (1 if dx > 0 else -1)
        return Position(enemy.x - step, enemy.y)
    step = 0 if dy == 0 else (1 if dy > 0 else -1)
    return Position(enemy.x, enemy.y - step)


def _hold_launcher(home: Position | None) -> None:
    if home is None:
        return
    if max(abs(map_info._my_pos.x - home.x), abs(map_info._my_pos.y - home.y)) <= 1:
        return
    destinations = set()
    for direction in map_info._DIRECTIONS:
        pos = map_info.pos_add(home, direction)
        if not map_info.in_bounds(pos) or not map_info.is_passable(pos):
            continue
        if rc.is_in_vision(pos) and rc.get_tile_builder_bot_id(pos):
            continue
        destinations.add(pos)
    if destinations:
        nav.move_to(destinations, avoid_turret=False)


def _visible_enemy(enemy_id: int) -> Position | None:
    for entity_id in rc.get_nearby_units():
        if entity_id == enemy_id:
            return rc.get_position(entity_id)
    return None


def _run_claim(lane: int) -> bool:
    """Mirror an active claim; return home immediately when its target vanishes."""
    global _mirror_enemy_position, _mirror_enemy_id, _return_home
    claim = comms.defender_claim(lane, rc.get_current_round())
    if claim is None:
        if _return_home is not None:
            _hold_launcher(_return_home)
            if max(
                abs(map_info._my_pos.x - _return_home.x),
                abs(map_info._my_pos.y - _return_home.y),
            ) <= 1:
                _return_home = None
        return False
    enemy_id, reported, home, active = claim
    if not active:
        # A pending claim means some launcher is arranging the relay. Stay in
        # pickup range rather than walking away.
        return True

    enemy = _visible_enemy(enemy_id)
    if enemy is None:
        _return_home = home
        _mirror_enemy_position = None
        _mirror_enemy_id = 0
        comms.release_enemy_claim(lane)
        _hold_launcher(home)
        return True

    comms.publish_lane_intruder(lane, enemy, rc.get_current_round(), home)
    if _mirror_enemy_id != enemy_id:
        _mirror_enemy_id = enemy_id
        _mirror_enemy_position = reported if reported is not None else enemy
    previous = _mirror_enemy_position
    _mirror_enemy_position = enemy
    if previous is None:
        return True
    dx = enemy.x - previous.x
    dy = enemy.y - previous.y
    if abs(dx) + abs(dy) != 1:
        return True
    direction = map_info.direction_to(previous, enemy)
    if rc.can_move(direction):
        rc.move(direction)
    return True


def counter_battery() -> bool:
    """v3 reserves defense builders for launcher pickup and mirroring."""
    return False


def run(lane: int) -> bool:
    """Run the opening launcher builder or an on-demand mirror defender."""
    global _return_launch_position, _wait_round, _wait_position
    if _run_claim(lane):
        return False

    if lane != 0:
        home = comms.defender_home(lane)
        _hold_launcher(home)
        return False

    if _return_launch_position is not None:
        if map_info._my_pos == _return_launch_position:
            # The last launcher owns a return-home handoff. Do not begin walking
            # and leave its pickup square if another action delayed the throw.
            return False
        _return_launch_position = None

    if _wait_round == rc.get_current_round() and map_info._my_pos == _wait_position:
        return False
    if _wait_round <= rc.get_current_round():
        _wait_round = -1
        _wait_position = None

    site = next_launcher_site(0)
    if site is not None:
        if abs(site.x - map_info._my_pos.x) + abs(site.y - map_info._my_pos.y) != 1:
            _move_into_build_range(site)
        _build_launcher(site)
        return False

    if _first_launcher_id:
        comms.publish_defender_home(0, rc.get_id(), _first_launcher_id, _first_launcher_site)
    _hold_launcher(_first_launcher_site)
    return False


def run_reinforcement(*_args) -> None:
    """Compatibility entry point; reinforcement builders now receive lane 1."""
    run(1)
