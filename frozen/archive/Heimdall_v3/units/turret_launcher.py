"""Compact base-launcher, relay, and attack-launcher behavior for Heimdall v3."""

from fcode import Controller, Direction, EntityType, Position

import comms
import map_info
import units.def_states.defense as defense


rc: Controller = None
_handoff_pending = True

_CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)


def init(c: Controller) -> None:
    global rc, _handoff_pending
    rc = c
    # Store writes are buffered. A launcher can execute once on its construction
    # round before its builder's handoff is readable, so keep trying until the
    # assigned builder is actually launched.
    _handoff_pending = True


def _visible_enemy_builders() -> list[tuple[int, Position]]:
    enemies = []
    my_team = rc.get_team()
    for entity_id in rc.get_nearby_units():
        if rc.get_entity_type(entity_id) != EntityType.BUILDER_BOT:
            continue
        if rc.get_team(entity_id) == my_team:
            continue
        enemies.append((entity_id, rc.get_position(entity_id)))
    return enemies


def _distance_from_core(pos: Position) -> int:
    core = map_info._my_core
    if core is None:
        return 0
    return min(
        (pos.x - x) * (pos.x - x) + (pos.y - y) * (pos.y - y)
        for x in (core.x, core.x + 1)
        for y in (core.y, core.y + 1)
    )


def _manhattan(a: Position, b: Position) -> int:
    return abs(a.x - b.x) + abs(a.y - b.y)


def _bot_passable(p: Position) -> bool:
    """A launch destination must be an in-bounds, non-wall, unoccupied tile."""
    if not map_info.in_bounds(p):
        return False
    bit = 1 << (p.x + p.y * map_info._width)
    if map_info._bm_env[map_info._IDX_ENV_WALL] & bit:
        return False
    if map_info._bm_any_building & bit:
        return False
    if (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & bit:
        return False
    return True


def _friendly_launchers() -> list[Position]:
    mine = (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    return list(map_info.iter_mask(mine))


def _attacker_launch_dest(target: Position) -> Position | None:
    """Where to fling an attack bot: adjacent to the closest-to-target friendly
    launcher we can reach (so it leapfrogs down the chain), else the reachable
    tile nearest to the target by Manhattan distance."""
    my_pos = rc.get_position()
    attackable = [t for t in rc.get_attackable_tiles() if _bot_passable(t)]
    if not attackable:
        return None

    # A visible friendly launcher strictly closer to the target than we are —
    # if we can drop the bot right next to it, do so.
    best_launcher = None
    best_manh = _manhattan(my_pos, target)
    for lp in _friendly_launchers():
        if lp == my_pos:
            continue
        m = _manhattan(lp, target)
        if m < best_manh:
            best_manh = m
            best_launcher = lp
    if best_launcher is not None:
        adjacent = [
            t for t in attackable
            if max(abs(t.x - best_launcher.x), abs(t.y - best_launcher.y)) <= 1
        ]
        if adjacent:
            return min(adjacent, key=lambda t: _manhattan(t, target))

    return min(attackable, key=lambda t: _manhattan(t, target))


def _throw_attacker() -> bool:
    """Fling an adjacent attack builder toward its symmetry-predicted enemy core.
    The launcher reads the bot's id, recovers its attack index from comms, and
    computes the target itself (it knows the same symmetry and map)."""
    my_pos = rc.get_position()
    my_team = rc.get_team()
    for entity_id in rc.get_nearby_units():
        if rc.get_entity_type(entity_id) != EntityType.BUILDER_BOT:
            continue
        if rc.get_team(entity_id) != my_team:
            continue
        bot_pos = rc.get_position(entity_id)
        if max(abs(bot_pos.x - my_pos.x), abs(bot_pos.y - my_pos.y)) > 1:
            continue  # outside pickup range (dist^2 <= 2)
        idx = comms.atk_index(entity_id)
        if idx is None:
            continue  # not an attack builder
        target = map_info.atk_symmetry_target(idx)
        if target is None:
            continue
        dest = _attacker_launch_dest(target)
        if dest is not None and rc.can_launch(bot_pos, dest):
            rc.launch(bot_pos, dest)
            return True
    return False


def _throw_enemy_away(enemies: list[tuple[int, Position]]) -> bool:
    """Launch an adjacent enemy to the legal tile farthest from our core."""
    my_pos = rc.get_position()
    best = None
    best_key = None
    attackable = rc.get_attackable_tiles()
    for enemy_id, bot_pos in enemies:
        if max(abs(bot_pos.x - my_pos.x), abs(bot_pos.y - my_pos.y)) > 1:
            continue
        for target in attackable:
            if not rc.can_launch(bot_pos, target):
                continue
            # Primary objective is exactly the defensive requirement. Stable
            # ties prefer a longer throw and then a deterministic tile index.
            key = (
                _distance_from_core(target),
                target.distance_squared(my_pos),
                target.x + target.y * map_info._width,
                enemy_id,
            )
            if best_key is None or key > best_key:
                best_key = key
                best = (bot_pos, target)
    if best is None:
        return False
    rc.launch(best[0], best[1])
    return True


def _throw_assigned_builder() -> bool:
    handoff = comms.launcher_handoff(rc.get_id())
    if handoff is None:
        return False
    builder_id, next_site, return_home = handoff

    builder_pos = None
    for entity_id in rc.get_nearby_units():
        if entity_id == builder_id:
            builder_pos = rc.get_position(entity_id)
            break
    if builder_pos is None:
        return False

    my_pos = rc.get_position()
    if max(abs(builder_pos.x - my_pos.x), abs(builder_pos.y - my_pos.y)) > 1:
        return False

    destinations = []
    directions = map_info._DIRECTIONS if return_home else _CARDINALS
    for direction in directions:
        target = map_info.pos_add(next_site, direction)
        if not map_info.in_bounds(target):
            continue
        if rc.can_launch(builder_pos, target):
            destinations.append(target)
    if not destinations:
        return False

    # Construction landings must be cardinal to the next build site. A final
    # return may use any of the eight pickup tiles surrounding the home launcher.
    target = min(
        destinations,
        key=lambda p: (p.distance_squared(my_pos), p.x + p.y * map_info._width),
    )
    rc.launch(builder_pos, target)
    return True


def _nearby_friendly_builders() -> dict[int, Position]:
    result = {}
    my_team = rc.get_team()
    for entity_id in rc.get_nearby_units():
        if (
            rc.get_entity_type(entity_id) == EntityType.BUILDER_BOT
            and rc.get_team(entity_id) == my_team
        ):
            result[entity_id] = rc.get_position(entity_id)
    return result


def _adjacent_to(a: Position, b: Position) -> bool:
    return max(abs(a.x - b.x), abs(a.y - b.y)) <= 1


def _relay_destination(builder_pos: Position, observer: Position) -> Position | None:
    attackable = rc.get_attackable_tiles()
    choices = []
    for direction in map_info._DIRECTIONS:
        pos = map_info.pos_add(observer, direction)
        if pos in attackable and _bot_passable(pos) and rc.can_launch(builder_pos, pos):
            choices.append(pos)
    if not choices:
        return None
    return min(
        choices,
        key=lambda pos: (pos.distance_squared(rc.get_position()), pos.x + pos.y * map_info._width),
    )


def _intercept_destination(builder_pos: Position, enemy_pos: Position) -> Position | None:
    """Best open landing beside an intruder, preferring its coreward square."""
    preferred = defense.coreward_block_tile(enemy_pos)
    attackable = rc.get_attackable_tiles()
    choices = []
    for direction in map_info._DIRECTIONS:
        pos = map_info.pos_add(enemy_pos, direction)
        if pos == builder_pos:
            choices.append((
                0 if pos == preferred else 1,
                _distance_from_core(pos),
                pos.x + pos.y * map_info._width,
                pos,
            ))
            continue
        if pos not in attackable or not _bot_passable(pos):
            continue
        if not rc.can_launch(builder_pos, pos):
            continue
        choices.append((
            0 if pos == preferred else 1,
            _distance_from_core(pos),
            pos.x + pos.y * map_info._width,
            pos,
        ))
    return min(choices)[-1] if choices else None


def _launch_lane(lane: int, local_enemies: dict[int, Position]) -> bool:
    """Direct-launch a pending defender, or relay it to the observing launcher."""
    if not comms.defender_claim_pending(lane):
        return False
    defender_id = comms.defender_id(lane)
    defender_pos = _nearby_friendly_builders().get(defender_id)
    my_pos = rc.get_position()
    if defender_pos is None or not _adjacent_to(defender_pos, my_pos):
        return False
    claim = comms.defender_claim(lane, rc.get_current_round())
    if claim is None:
        return False
    enemy_id, reported, observer, _active = claim
    enemy_pos = local_enemies.get(enemy_id)
    if enemy_pos is not None:
        destination = _intercept_destination(defender_pos, enemy_pos)
        if destination is None:
            return False
        if defender_pos == destination:
            comms.activate_defender_intercept(lane, enemy_id)
            return False
        rc.launch(defender_pos, destination)
        comms.activate_defender_intercept(lane, enemy_id)
        return True

    # This launcher cannot see the target, but the payload names the launcher
    # that can. If that launcher is visible, leapfrog the defender beside it.
    observer_bit = 1 << (observer.x + observer.y * map_info._width)
    friendly_launchers = (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
        & map_info._bm_visible
    )
    if observer != my_pos and friendly_launchers & observer_bit:
        destination = _relay_destination(defender_pos, observer)
        if destination is not None:
            rc.launch(defender_pos, destination)
            return True
    return False


def _new_spawned_reinforcement(request, friendly: dict[int, Position]) -> tuple[int, Position] | None:
    """Find the just-spawned, still-unassigned builder beside this launcher."""
    enemy_id, assigned_id, observer, lane = request
    if observer != rc.get_position():
        return None
    if assigned_id and assigned_id in friendly:
        return assigned_id, friendly[assigned_id]
    reserved = {
        entity_id
        for entity_id in friendly
        if comms.atk_index(entity_id) is not None
        or comms.is_economy(entity_id)
        or comms.defender_lane(entity_id) is not None
    }
    candidates = [
        (entity_id, pos)
        for entity_id, pos in friendly.items()
        if entity_id not in reserved and _adjacent_to(pos, observer)
    ]
    return max(candidates, default=None, key=lambda item: item[0])


def _process_reinforcement(local_enemies: dict[int, Position]) -> bool:
    request = comms.reinforcement_claim()
    if request is None:
        return False
    enemy_id, _assigned_id, observer, lane = request
    if observer != rc.get_position():
        return False
    friendly = _nearby_friendly_builders()
    reinforcement = _new_spawned_reinforcement(request, friendly)
    if reinforcement is None:
        return False
    defender_id, defender_pos = reinforcement
    if not _adjacent_to(defender_pos, observer):
        return False
    if enemy_id not in local_enemies:
        comms.assign_lane_defender(lane, defender_id)
        comms.set_defender_home(lane, observer)
        comms.clear_reinforcement()
        return False
    enemy_pos = local_enemies[enemy_id]
    comms.assign_defender_claim(lane, defender_id, enemy_id, enemy_pos, observer)
    destination = _intercept_destination(defender_pos, enemy_pos)
    if destination is None:
        return False
    rc.launch(defender_pos, destination)
    comms.activate_defender_intercept(lane, enemy_id, defender_id)
    comms.clear_reinforcement()
    return True


def _claim_visible_enemies(enemies: list[tuple[int, Position]]) -> None:
    """Refresh existing claims, then claim or request one unguarded intruder."""
    current_round = rc.get_current_round()
    my_pos = rc.get_position()
    claimed = comms.claimed_enemy_ids()
    for enemy_id, enemy_pos in enemies:
        for lane in (0, 1):
            if comms.claimed_enemy_id(lane) == enemy_id:
                claim = comms.defender_claim(lane, current_round)
                home = claim[2] if claim is not None else my_pos
                comms.publish_lane_intruder(lane, enemy_pos, current_round, home)

    if comms.reinforcement_claim() is not None:
        return
    friendly = _nearby_friendly_builders()
    for enemy_id, enemy_pos in sorted(
        enemies, key=lambda item: (_distance_from_core(item[1]), item[0])
    ):
        if enemy_id in claimed:
            continue
        free_lanes = [lane for lane in (0, 1) if not comms.claimed_enemy_id(lane)]
        if not free_lanes:
            return
        # Prefer a defender this launcher can throw now, then any globally idle
        # lane (the other home launcher will relay it here).
        local_lane = next((
            lane for lane in free_lanes
            if comms.defender_id(lane)
            and _adjacent_to(friendly.get(comms.defender_id(lane), Position(-99, -99)), my_pos)
        ), None)
        if local_lane is not None:
            comms.claim_enemy_builder(local_lane, enemy_id, enemy_pos, my_pos)
            return
        idle_lane = next((lane for lane in free_lanes if comms.defender_id(lane)), None)
        if idle_lane is not None:
            comms.claim_enemy_builder(idle_lane, enemy_id, enemy_pos, my_pos)
            return
        # Only lane 1 is dynamically spawned; lane 0 is the opening defender.
        if 1 in free_lanes:
            comms.request_reinforcement(enemy_id, my_pos, 1)
        return


def run() -> None:
    global _handoff_pending
    # Launchers have radius²-26 vision, larger than builders. Feed those extra
    # wall/ore observations and symmetry eliminations into Loki's shared-board
    # protocol before deciding whom to throw.
    map_info.update(recompute=False)
    comms.update()
    map_info.recompute_derived()
    enemies = _visible_enemy_builders()
    # Base-defense launches only make sense for launchers that actually know
    # where our base is. Attack-chain launchers are built far out and never see
    # the core locally (_my_core stays None), so they skip all lane / base /
    # intruder logic — which reads _my_core — and only fling attack bots.
    has_core = map_info._my_core is not None
    local_enemies = dict(enemies)
    if enemies and has_core:
        _claim_visible_enemies(enemies)
    launched_reinforcement = has_core and _process_reinforcement(local_enemies)
    launched_defender = False
    if has_core and not launched_reinforcement:
        for lane in (0, 1):
            if _launch_lane(lane, local_enemies):
                launched_defender = True
                break
    # Fast-travel: fling an adjacent attack bot toward the enemy core (after the
    # defensive launches, which use different builders and protect the base).
    launched_attacker = (
        not launched_defender
        and not launched_reinforcement
        and _throw_attacker()
    )
    launched_enemy = (
        not launched_defender
        and not launched_reinforcement
        and not launched_attacker
        and bool(enemies)
        and _throw_enemy_away(enemies)
    )
    if (
        has_core
        and not launched_defender
        and not launched_reinforcement
        and not launched_attacker
        and not launched_enemy
        and _handoff_pending
        and _throw_assigned_builder()
    ):
        _handoff_pending = False
