"""Launchers.

Only the FIRST launcher built (the "speedup" launcher, claimed by spawn turn via
comms) flings the opening roster across the map — each opening builder once
toward its goal (attack bots -> enemy core, econ bots -> their ore, econ bot 0 ->
beside its killbox) — then self-destructs once the roster is away.

Every LATER launcher is a KILLBOX launcher (built by an econ bot beside an enemy
builder — see econ_states/trap_launcher). It NEVER launches friendly builders;
it only flings enemy builders in pickup range into the killbox centre, where the
killbox gunner kills them.
"""

from fcode import EntityType

import comms
import map_info
import units.launch_plan as plan
import units.killbox_plan as killbox_plan
from _config import INITIAL_SPAWN_COUNT, KILLBOX_ENABLED

rc = None

# builder_id -> assigned ore Position, and the set of ore tile indices handed out.
_econ_assignment = {}
_assigned_ore_ns = set()

# Ids of builders this (speedup) launcher has already flung — never fling the
# same builder twice (a short throw can land a bot back in pickup range).
_launched_ids = set()

_spawn_turn = 0
_role = None  # cached: True = speedup launcher, False = killbox launcher


def init(c) -> None:
    global rc, _spawn_turn
    rc = c
    _spawn_turn = c.get_current_round()


def _is_speedup() -> bool:
    """This launcher is the speedup launcher iff it was the first one built —
    claimed once (write-once via comms) and cached for the rest of its life."""
    global _role
    if _role is None:
        _role = comms.claim_speedup_launcher(_spawn_turn)
    return _role


def run() -> None:
    map_info.update(recompute=False)
    comms.update()
    map_info.recompute_derived()

    if _is_speedup():
        # Whole roster flung -> the speedup launcher's job is done.
        if len(_launched_ids) >= INITIAL_SPAWN_COUNT:
            comms.mark_launch_done()
            rc.self_destruct()
            return
        _throw_one()
        return

    # Killbox launcher: only fling enemy builders into the killbox (never friendly).
    _throw_enemy_to_killbox()


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
        # Econ bot 0 is the killbox builder: land it OUTSIDE, beside its first
        # build (never inside the box or on a build tile), so it can seal the
        # killbox without getting thrown in or stuck.
        if killbox_plan.active() and comms.economy_index(entity_id) == 0:
            land = killbox_plan.landing_tile()
            if land is not None:
                forbid = killbox_plan.no_land_tiles()
                w = map_info._width
                outside = [t for t in attackable if (t.x + t.y * w) not in forbid]
                dest = plan.econ_dest(outside, land) if outside else None
                if dest is not None:
                    return dest
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
    # Every friendly builder in pickup range (dist^2 <= 2) we haven't already
    # flung, lowest id first.
    candidates = []
    for entity_id in rc.get_nearby_units():
        if entity_id in _launched_ids:
            continue
        if rc.get_entity_type(entity_id) != EntityType.BUILDER_BOT:
            continue
        if rc.get_team(entity_id) != my_team:
            continue
        bot_pos = rc.get_position(entity_id)
        if max(abs(bot_pos.x - my_pos.x), abs(bot_pos.y - my_pos.y)) > 1:
            continue
        candidates.append((entity_id, bot_pos))
    candidates.sort(key=lambda c: c[0])
    for entity_id, bot_pos in candidates:
        # The killbox builder, if already standing on a valid build stance, must
        # NOT be flung — a launch can't target its own tile, so it would land a
        # tile away from the placement. Mark it handled (so the launcher still
        # counts it toward self-destruct) and leave it in place to build.
        if (comms.is_economy(entity_id) and comms.economy_index(entity_id) == 0
                and killbox_plan.active() and killbox_plan.is_good_landing(bot_pos)):
            _launched_ids.add(entity_id)
            return
        dest = _dest_for(entity_id, attackable)
        if dest is not None and rc.can_launch(bot_pos, dest):
            rc.launch(bot_pos, dest)
            _commit(entity_id, dest)
            _launched_ids.add(entity_id)
            return  # one launch per turn


def _throw_enemy_to_killbox() -> None:
    """Fling an enemy builder in pickup range into the killbox centre."""
    p = killbox_plan.plan()
    if p is None:
        return
    center = p["center"]
    my_pos = rc.get_position()
    my_team = rc.get_team()
    for entity_id in rc.get_nearby_units():
        if rc.get_entity_type(entity_id) != EntityType.BUILDER_BOT:
            continue
        if rc.get_team(entity_id) == my_team:
            continue
        bot_pos = rc.get_position(entity_id)
        if max(abs(bot_pos.x - my_pos.x), abs(bot_pos.y - my_pos.y)) > 1:
            continue  # not in pickup range
        if rc.can_launch(bot_pos, center):
            rc.launch(bot_pos, center)
            return
