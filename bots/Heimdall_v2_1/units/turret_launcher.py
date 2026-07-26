"""Launcher behavior for Heimdall's defensive ring."""

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
    builder_id, next_site = handoff

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
    for direction in _CARDINALS:
        target = map_info.pos_add(next_site, direction)
        if not map_info.in_bounds(target):
            continue
        if rc.can_launch(builder_pos, target):
            destinations.append(target)
    if not destinations:
        return False

    # All destinations are cardinally adjacent to the next build site. Prefer
    # the shortest legal throw, keeping later ring traversal predictable.
    target = min(
        destinations,
        key=lambda p: (p.distance_squared(my_pos), p.x + p.y * map_info._width),
    )
    rc.launch(builder_pos, target)
    return True


def _throw_lane_defender_toward_intruder(
    enemies: list[tuple[int, Position]],
) -> bool:
    """Claim one enemy per lane, then throw the adjacent claimed defender."""
    claimed = {lane: comms.claimed_enemy_id(lane) for lane in (0, 1)}
    reinforcement = comms.reinforcement_claim()
    reinforcement_enemy = reinforcement[0] if reinforcement is not None else 0
    candidates = []

    for enemy_id, enemy_pos in sorted(
        enemies,
        key=lambda item: (_distance_from_core(item[1]), item[0]),
    ):
        matching_lane = next(
            (lane for lane, claimed_id in claimed.items() if claimed_id == enemy_id),
            None,
        )
        if matching_lane is not None:
            comms.publish_lane_intruder(matching_lane, enemy_pos, rc.get_current_round())
            if not comms.defender_intercepting(matching_lane):
                candidates.append((enemy_id, enemy_pos, matching_lane))
            continue
        if enemy_id == reinforcement_enemy:
            comms.publish_reinforcement_enemy(enemy_id, enemy_pos)
            continue

        free_lanes = [lane for lane in (0, 1) if claimed[lane] == 0]
        if free_lanes:
            preferred = defense.intruder_lane(enemy_pos)
            launcher_pos = rc.get_position()
            lane = None
            # If the preferred lane appears free, only it may claim this enemy.
            # A later launcher in the same buffered-write round also sees it as
            # free, but its defender has already moved after the first launch;
            # refusing fallback prevents a duplicate claim in the other lane.
            lane_options = (
                [preferred]
                if preferred in free_lanes
                else free_lanes
            )
            for candidate_lane in lane_options:
                defender_id = comms.defender_id(candidate_lane)
                defender_pos = None
                for entity_id in rc.get_nearby_units():
                    if entity_id == defender_id:
                        defender_pos = rc.get_position(entity_id)
                        break
                if defender_pos is not None and max(
                    abs(defender_pos.x - launcher_pos.x),
                    abs(defender_pos.y - launcher_pos.y),
                ) <= 1:
                    lane = candidate_lane
                    break
            if lane is None:
                # Only a launcher that can currently pick up this defender may
                # create the claim. This prevents later same-round launchers
                # from overwriting a successful activation with stale state.
                continue
            claimed[lane] = enemy_id
            comms.claim_enemy_builder(lane, enemy_id)
            comms.publish_lane_intruder(lane, enemy_pos, rc.get_current_round())
            candidates.append((enemy_id, enemy_pos, lane))
        elif reinforcement is None:
            comms.publish_reinforcement_enemy(enemy_id, enemy_pos)
            reinforcement_enemy = enemy_id

    # A launcher without local vision may still service a pending lane claim
    # from another launcher's previous-round position report.
    for lane in (0, 1):
        enemy_id = claimed[lane]
        if not enemy_id or comms.defender_intercepting(lane):
            continue
        if any(candidate[2] == lane for candidate in candidates):
            continue
        enemy_pos = comms.lane_intruder(lane, rc.get_current_round())
        if enemy_pos is not None:
            candidates.append((enemy_id, enemy_pos, lane))

    for enemy_id, enemy_pos, lane in candidates:
        defender_id = comms.defender_id(lane)
        if not defender_id:
            continue

        defender_pos = None
        for entity_id in rc.get_nearby_units():
            if entity_id == defender_id:
                defender_pos = rc.get_position(entity_id)
                break
        if defender_pos is None:
            continue

        launcher_pos = rc.get_position()
        if max(
            abs(defender_pos.x - launcher_pos.x),
            abs(defender_pos.y - launcher_pos.y),
        ) > 1:
            continue

        block = defense.coreward_block_tile(enemy_pos)
        if defender_pos == block:
            comms.activate_defender_intercept(lane, enemy_id)
            return False
        if block not in rc.get_attackable_tiles() or not rc.can_launch(defender_pos, block):
            continue
        rc.launch(defender_pos, block)
        comms.activate_defender_intercept(lane, enemy_id)
        return True
    return False


def _throw_reinforcement() -> bool:
    claim = comms.reinforcement_claim()
    if claim is None:
        return False
    enemy_id, defender_id, enemy_pos, launched = claim
    if not defender_id or launched:
        return False
    defender_pos = None
    for entity_id in rc.get_nearby_units():
        if entity_id == defender_id:
            defender_pos = rc.get_position(entity_id)
            break
    if defender_pos is None:
        return False
    launcher_pos = rc.get_position()
    if max(abs(defender_pos.x - launcher_pos.x), abs(defender_pos.y - launcher_pos.y)) > 1:
        return False
    block = defense.coreward_block_tile(enemy_pos)
    if block not in rc.get_attackable_tiles() or not rc.can_launch(defender_pos, block):
        return False
    rc.launch(defender_pos, block)
    comms.activate_reinforcement()
    return True


def _publish_visible_intruders(enemies: list[tuple[int, Position]]) -> None:
    """Refresh positions for already-claimed enemies."""
    current_round = rc.get_current_round()
    lane_claims = {comms.claimed_enemy_id(lane): lane for lane in (0, 1)}
    reinforcement = comms.reinforcement_claim()
    reinforcement_enemy = reinforcement[0] if reinforcement is not None else 0
    for enemy_id, enemy_pos in enemies:
        lane = lane_claims.get(enemy_id)
        if lane is not None:
            comms.publish_lane_intruder(lane, enemy_pos, current_round)
        elif enemy_id == reinforcement_enemy:
            comms.publish_reinforcement_enemy(enemy_id, enemy_pos)


def run() -> None:
    global _handoff_pending
    # Launchers have radius²-26 vision, larger than builders. Feed those extra
    # wall/ore observations and symmetry eliminations into Loki's shared-board
    # protocol before deciding whom to throw.
    map_info.update(recompute=False)
    comms.update()
    map_info.recompute_derived()
    enemies = _visible_enemy_builders()
    # All v2.1 launchers belong to the base ring. A launcher that has not yet
    # learned the shared core position skips core-relative defensive behavior.
    has_core = map_info._my_core is not None
    if enemies and has_core:
        _publish_visible_intruders(enemies)
    launched_defender = has_core and _throw_lane_defender_toward_intruder(enemies)
    launched_reinforcement = (
        has_core and not launched_defender and _throw_reinforcement()
    )
    launched_enemy = (
        not launched_defender
        and not launched_reinforcement
        and bool(enemies)
        and _throw_enemy_away(enemies)
    )
    if (
        has_core
        and not launched_defender
        and not launched_reinforcement
        and not launched_enemy
        and _handoff_pending
        and _throw_assigned_builder()
    ):
        _handoff_pending = False
