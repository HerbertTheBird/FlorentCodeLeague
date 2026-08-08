"""Opening transport/defense launcher and optional forward siege launcher.

The opening launcher deploys all three builders, then dispatches the two
returned economy defenders to mirror claimed intruders. A forward launcher can
supply the attacker's optional second throw and afterward launches nearby enemy
builders away from the sentinel.
"""

from fcode import EntityType, Position

import comms
import map_info
import units.launch_plan as plan
from _config import INITIAL_SPAWN_COUNT
from log import status

rc = None

# builder_id -> assigned ore Position, and the set of ore tile indices handed out.
_econ_assignment = {}
_assigned_ore_ns = set()

# Count of builders this launcher has flung through the opening transport phase.
# It remains alive afterward as the two economy defenders' pickup/home launcher.
_launched = 0
_siege_launch_used = False


def init(c) -> None:
    global rc
    rc = c


def run() -> None:
    map_info.update(recompute=False)
    comms.update()
    map_info.recompute_derived()
    if _is_opening_launcher():
        if _launched < INITIAL_SPAWN_COUNT:
            _throw_one()
            return
        comms.mark_launch_done()
        if _throw_siege_attacker(require_request=True):
            return
        _run_base_defense()
        return
    if _throw_siege_attacker():
        return
    _throw_enemy()


def _throw_siege_attacker(require_request: bool = False) -> bool:
    """Insert our attacker beside the best currently valid sentinel site."""
    global _siege_launch_used
    if comms.attack_launch_count() >= 2:
        return False
    # Lazy import avoids initializing builder-only pathing state for launchers.
    from units.atk_states import sentinel_siege

    local_sentinels = (
        map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[map_info._my_team_idx]
    ).bit_count()
    # The shared counter can be one buffered write behind, so also trust the
    # launcher's local board view. This prevents a retained launcher from
    # repeatedly flinging the attacker after the two-sentinel phase is done.
    siege_complete = (
        max(comms.sentinel_count(), local_sentinels)
        >= sentinel_siege.SENTINEL_LIMIT
    )

    my_pos = rc.get_position()
    insertion = sentinel_siege.launch_plan_from(my_pos, rc)
    if insertion is None:
        return False
    destination, sentinel_site, sentinel_facing = insertion
    candidates = []
    my_team = rc.get_team()
    for entity_id in rc.get_nearby_units():
        if (
            rc.get_entity_type(entity_id) != EntityType.BUILDER_BOT
            or rc.get_team(entity_id) != my_team
            or comms.atk_index(entity_id) is None
        ):
            continue
        bot_pos = rc.get_position(entity_id)
        if max(abs(bot_pos.x - my_pos.x), abs(bot_pos.y - my_pos.y)) <= 1:
            candidates.append((entity_id, bot_pos))
    for entity_id, bot_pos in sorted(candidates):
        requested = comms.siege_relaunch_requested(entity_id)
        if (require_request or _siege_launch_used or siege_complete) and not requested:
            continue
        if rc.can_launch(bot_pos, destination):
            comms.set_siege_insert(entity_id, sentinel_site, sentinel_facing)
            rc.launch(bot_pos, destination)
            comms.note_attack_launch()
            _siege_launch_used = True
            status("siege launcher id=%d inserted attacker %d at %s" % (
                rc.get_id(), entity_id, destination))
            return True
    return False


def _is_opening_launcher() -> bool:
    opening = plan.launcher_position()
    return opening is not None and rc.get_position() == opening


def _landable(t) -> bool:
    n = t.x + t.y * map_info._width
    bit = 1 << n
    if map_info._bm_env[map_info._IDX_ENV_WALL] & bit:
        return False
    if map_info._bm_any_building & bit:
        return False
    if (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & bit:
        return False
    return True


def _next_unassigned_ore():
    for ore in plan.ranked_ore_from_core():
        if (ore.x + ore.y * map_info._width) not in _assigned_ore_ns:
            return ore
    return None


def _dest_for(entity_id, attackable):
    """Throw destination for this builder given its comms role."""
    if comms.is_economy(entity_id):
        ore = _econ_assignment.get(entity_id) or _next_unassigned_ore()
        if ore is not None:
            dest = plan.econ_dest(attackable, ore)
            if dest is not None:
                return dest
        # No ore left / unreachable: fall back to flinging it forward.
    return plan.attack_dest(attackable)


def _commit(entity_id, ore_hint) -> None:
    """Record an econ bot's ore assignment once it is actually launched."""
    if not comms.is_economy(entity_id) or entity_id in _econ_assignment:
        return
    ore = _next_unassigned_ore()
    if ore is not None:
        _econ_assignment[entity_id] = ore
        _assigned_ore_ns.add(ore.x + ore.y * map_info._width)


def _throw_one() -> None:
    my_pos = rc.get_position()
    my_team = rc.get_team()
    attackable = [t for t in rc.get_attackable_tiles() if _landable(t)]
    if not attackable:
        return
    # Every friendly builder in pickup range (dist^2 <= 2), lowest id first — we
    # always launch the lowest-id builder available.
    candidates = []
    for entity_id in rc.get_nearby_units():
        if rc.get_entity_type(entity_id) != EntityType.BUILDER_BOT:
            continue
        if rc.get_team(entity_id) != my_team:
            continue
        bot_pos = rc.get_position(entity_id)
        if max(abs(bot_pos.x - my_pos.x), abs(bot_pos.y - my_pos.y)) > 1:
            continue
        candidates.append((entity_id, bot_pos))
    candidates.sort(key=lambda c: c[0])
    global _launched
    for entity_id, bot_pos in candidates:
        dest = _dest_for(entity_id, attackable)
        if dest is not None and rc.can_launch(bot_pos, dest):
            rc.launch(bot_pos, dest)
            if _launched == 0 or comms.atk_index(entity_id) is not None:
                comms.note_attack_launch()
            _commit(entity_id, dest)
            _launched += 1
            return  # one launch per turn


def _visible_enemy_builders() -> list[tuple[int, Position]]:
    result = []
    my_team = rc.get_team()
    for entity_id in rc.get_nearby_units():
        if (
            rc.get_entity_type(entity_id) == EntityType.BUILDER_BOT
            and rc.get_team(entity_id) != my_team
        ):
            result.append((entity_id, rc.get_position(entity_id)))
    return result


def _nearby_defenders() -> dict[int, tuple[int, Position]]:
    """Economy lane -> (builder id, position) for builders in pickup range."""
    result = {}
    my_pos = rc.get_position()
    my_team = rc.get_team()
    for entity_id in rc.get_nearby_units():
        if (
            rc.get_entity_type(entity_id) != EntityType.BUILDER_BOT
            or rc.get_team(entity_id) != my_team
        ):
            continue
        lane = comms.economy_index(entity_id)
        if lane not in (0, 1) or not comms.defender_ready(lane):
            continue
        pos = rc.get_position(entity_id)
        if max(abs(pos.x - my_pos.x), abs(pos.y - my_pos.y)) <= 1:
            result[lane] = (entity_id, pos)
    return result


def _distance_from_our_core(pos: Position) -> int:
    core = map_info._my_core
    if core is None:
        return 10 ** 9
    return min(
        pos.distance_squared(Position(core.x + dx, core.y + dy))
        for dx in (0, 1) for dy in (0, 1)
    )


def _coreward_block_tile(enemy: Position) -> Position:
    """Enemy-adjacent tile on its dominant cardinal offset toward our core."""
    core = map_info._my_core
    if core is None:
        return enemy
    nearest_x = min(max(enemy.x, core.x), core.x + 1)
    nearest_y = min(max(enemy.y, core.y), core.y + 1)
    dx, dy = enemy.x - nearest_x, enemy.y - nearest_y
    if abs(dx) >= abs(dy):
        step = 0 if dx == 0 else (1 if dx > 0 else -1)
        return Position(enemy.x - step, enemy.y)
    step = 0 if dy == 0 else (1 if dy > 0 else -1)
    return Position(enemy.x, enemy.y - step)


def _intercept_destination(defender_pos: Position, enemy_pos: Position) -> Position | None:
    preferred = _coreward_block_tile(enemy_pos)
    attackable = rc.get_attackable_tiles()
    choices = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            pos = Position(enemy_pos.x + dx, enemy_pos.y + dy)
            if not map_info.in_bounds(pos):
                continue
            if pos == defender_pos:
                choices.append((
                    0 if pos == preferred else 1,
                    _distance_from_our_core(pos),
                    pos.x + pos.y * map_info._width,
                    pos,
                ))
                continue
            if pos not in attackable or not _landable(pos):
                continue
            if not rc.can_launch(defender_pos, pos):
                continue
            choices.append((
                0 if pos == preferred else 1,
                _distance_from_our_core(pos),
                pos.x + pos.y * map_info._width,
                pos,
            ))
    if not choices:
        return None
    return min(choices)[-1]


def _launch_defender(
    lane: int,
    defender: tuple[int, Position],
    enemy_id: int,
    enemy_pos: Position,
) -> bool:
    _defender_id, defender_pos = defender
    destination = _intercept_destination(defender_pos, enemy_pos)
    if destination is None:
        comms.set_defense_claim(lane, enemy_id, enemy_pos, active=False)
        return False
    if destination != defender_pos:
        if not rc.can_launch(defender_pos, destination):
            comms.set_defense_claim(lane, enemy_id, enemy_pos, active=False)
            return False
        rc.launch(defender_pos, destination)
    comms.set_defense_claim(lane, enemy_id, enemy_pos, active=True)
    status("base launcher id=%d dispatched defense%d against enemy %d" % (
        rc.get_id(), lane, enemy_id))
    return True


def _run_base_defense() -> None:
    """Claim visible intruders and dispatch one camping defender per enemy."""
    comms.clear_stale_defense_claims()
    enemies = _visible_enemy_builders()
    local = dict(enemies)
    defenders = _nearby_defenders()

    # Keep reports fresh and finish any pending dispatch before taking a new
    # claim. Only one launch action is available per launcher turn.
    for lane in (0, 1):
        claim = comms.defense_claim(lane)
        if claim is None:
            continue
        enemy_id, reported, active = claim
        enemy_pos = local.get(enemy_id)
        if enemy_pos is not None:
            comms.set_defense_claim(lane, enemy_id, enemy_pos, active)
        if not active and lane in defenders:
            if _launch_defender(
                lane, defenders[lane], enemy_id, enemy_pos or reported
            ):
                return

    claimed = comms.claimed_defense_enemy_ids()
    for enemy_id, enemy_pos in sorted(
        enemies, key=lambda item: (_distance_from_our_core(item[1]), item[0])
    ):
        if enemy_id in claimed:
            continue
        lane = next((i for i in (0, 1) if comms.defense_claim(i) is None and i in defenders), None)
        if lane is None:
            return
        _launch_defender(lane, defenders[lane], enemy_id, enemy_pos)
        return


def _core_distance(pos, core) -> int:
    if core is None:
        return pos.distance_squared(rc.get_position())
    return min(
        pos.distance_squared(Position(core.x + dx, core.y + dy))
        for dx in (0, 1) for dy in (0, 1)
    )


def _enemy_core_distance(pos) -> int:
    return _core_distance(pos, map_info._their_core or map_info._predicted_enemy_core)


def _own_core_distance(pos) -> int:
    return _core_distance(pos, map_info._my_core)


def _sentinel_distance(pos) -> int:
    sentinels = (
        map_info._bm_et[map_info._IDX_SENTINEL]
        & map_info._bm_team[map_info._my_team_idx]
    )
    if not sentinels:
        return 0
    return min(pos.distance_squared(s) for s in map_info.iter_mask(sentinels))


def _throw_enemy() -> None:
    """Throw one adjacent enemy builder away from the siege area."""
    my_pos = rc.get_position()
    my_team = rc.get_team()
    destinations = [t for t in rc.get_attackable_tiles() if _landable(t)]
    if not destinations:
        return
    # Keep the defender away from both bases: blindly maximizing distance from
    # its own core can throw it straight toward ours on linear maps.  The
    # max-min term strongly prefers a lateral / far-edge landing instead.
    destinations.sort(
        key=lambda p: (
            min(_enemy_core_distance(p), _own_core_distance(p)),
            _enemy_core_distance(p),
            _own_core_distance(p),
            _sentinel_distance(p),
            p.distance_squared(my_pos),
            p.x,
            p.y,
        ),
        reverse=True,
    )
    enemies = []
    for entity_id in rc.get_nearby_units():
        if rc.get_entity_type(entity_id) != EntityType.BUILDER_BOT:
            continue
        if rc.get_team(entity_id) == my_team:
            continue
        bot_pos = rc.get_position(entity_id)
        if max(abs(bot_pos.x - my_pos.x), abs(bot_pos.y - my_pos.y)) <= 1:
            enemies.append((entity_id, bot_pos))
    enemies.sort(key=lambda item: (_enemy_core_distance(item[1]), item[0]))
    for _entity_id, bot_pos in enemies:
        for dest in destinations:
            if rc.can_launch(bot_pos, dest):
                rc.launch(bot_pos, dest)
                status("forward launcher id=%d launched enemy %s to %s" % (
                    rc.get_id(), bot_pos, dest))
                return
