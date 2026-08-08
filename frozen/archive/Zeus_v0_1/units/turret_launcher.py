"""The single central launcher.

It flings each friendly builder that stands within pickup range toward its goal:
attack / generalist bots toward the enemy core, econ bots toward their assigned
titanium ore (closest-by-BFS-from-core ore not yet handed out). Ore assignments
are tracked locally — there is only ever one launcher, so its module state is the
single source of truth (1st econ bot -> closest ore, 2nd -> 2nd closest, ...).
"""

from fcode import EntityType

import comms
import map_info
import units.launch_plan as plan
from _config import INITIAL_SPAWN_COUNT

rc = None

# builder_id -> assigned ore Position, and the set of ore tile indices handed out.
_econ_assignment = {}
_assigned_ore_ns = set()

# Count of builders this launcher has flung. Its only job is the opening roster,
# so once it has launched all of them it self-destructs to clear the tile.
_launched = 0


def init(c) -> None:
    global rc
    rc = c


def run() -> None:
    map_info.update(recompute=False)
    comms.update()
    map_info.recompute_derived()
    # Whole roster launched — the launcher has done its job; get out of the way.
    if _launched >= INITIAL_SPAWN_COUNT:
        comms.mark_launch_done()   # tell builders to stop waiting to be flung
        rc.self_destruct()
        return
    _throw_one()


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
            _commit(entity_id, dest)
            _launched += 1
            return  # one launch per turn
