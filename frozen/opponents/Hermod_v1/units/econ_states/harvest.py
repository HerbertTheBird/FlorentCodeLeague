import map_info
import pathing
from pathing import Pathing
from fcode import *
import units.builder
from log import log
import sys
rc: Controller = None
nav: Pathing = None

def _my_claims():
    my_pos = map_info._my_pos
    w = map_info._width
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    if units.builder._economy_builder:
        assigned = units.builder.assigned_econ_ore()
        if assigned is None:
            return 0
        # Opening econ builders have a deterministic one-ore assignment.  Do not
        # run that ore through the generic "near side" heuristic: on narrow maps
        # (notably Showdown) it filtered econ #2's valid assignment and left the
        # builder idle forever.  Dynamic blockers and turret danger still come
        # from possible_ore(), and a completed harvester is excluded below.
        available = (possible_ore()
                     & ~map_info._bm_et[map_info._IDX_HARVESTER]
                     & ~cant_harvest
                     & (1 << (assigned.x + assigned.y * w)))
        # The assignment itself is the claim.  Running it through claim_subset()
        # lets an attack/guard builder that happens to be closer suppress this
        # econ builder even though that other builder will never harvest it.
        return available
    else:
        available = harvestable_ore()
        available &= ~_too_expensive()
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, available, tie_self=False)

def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav

cant_harvest = 0
_cost_map: dict[int, tuple[int, int]] = {}  # tile index -> (min titanium cost, round recorded)
COST_MAP_TTL = 100
def possible_ore():
    w = map_info._width
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    if (map_info._bm_team[map_info._my_team_idx] & map_info._bm_et[map_info._IDX_HARVESTER] & map_info._bm_env[map_info._IDX_ENV_ORE_TI]) and rc.get_current_round() >= 750:
        ore |= 0
    my_team_idx = map_info._my_team_idx
    enemy_idx = 1 - my_team_idx

    # Enemy buildings that block harvesting (not road/conveyor/bridge/splitter/marker)
    enemy_blocking = (
        map_info._bm_team[enemy_idx]
        & ~map_info._bm_et[map_info._IDX_HARVESTER]
    )
    # Friendly buildings that block harvesting (not road/barrier/marker)
    friendly_blocking = (
        map_info._bm_team[my_team_idx]
        & ~map_info._bm_et[map_info._IDX_CONVEYOR]
        & ~map_info._bm_et[map_info._IDX_BARRIER]
        & ~map_info._bm_et[map_info._IDX_HARVESTER]
    )
    # Ore tiles surrounded on all 4 cardinal sides by ore — unreachable by conveyor
    landlocking = ore | ~map_info._bm_seen&map_info._board_mask
    landlocked = landlocking & (landlocking >> 1 & map_info._not_right_col) & (landlocking << 1 & map_info._not_left_col) & (landlocking >> w) & (landlocking << w)

    enemy_blocked = map_info.expand_manhattan(enemy_blocking)

    return (ore
            & ~landlocked
            & ~enemy_blocked
            & ~friendly_blocking
            & ~map_info._bm_enemy_turret_threat)
def secured():
    my_team_idx = map_info._my_team_idx
    securing = ( map_info._bm_team[my_team_idx]
    | map_info._bm_env[map_info._IDX_ENV_WALL])
    w = map_info._width
    bottom_row = ((1<<w)-1)<<w*(map_info._height-1)
    top_row = ((1<<w)-1)
    secured = (((securing&map_info._not_left_col) >> 1)|~map_info._not_right_col) & (((securing&map_info._not_right_col) << 1)|~map_info._not_left_col) & ((securing>>w)|bottom_row) & ((securing<<w)|top_row)
    return secured
def _manh_to_core(x: int, y: int, core) -> int:
    """Raw Manhattan distance from (x, y) to the nearest tile of a 2x2 core."""
    cx = min(max(x, core.x), core.x + 1)
    cy = min(max(y, core.y), core.y + 1)
    return abs(x - cx) + abs(y - cy)


def _near_side_ore(ore: int) -> int:
    """Keep only ore tiles at least 2x closer (raw Manhattan) to our core than to
    the enemy core, so econ bots don't commit to contested / enemy-side ore. If
    either core is unknown yet, no filtering is applied."""
    my_core = map_info._my_core
    enemy_core = map_info._their_core or map_info._predicted_enemy_core
    if my_core is None or enemy_core is None:
        return ore
    w = map_info._width
    kept = 0
    m = ore
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        x, y = n % w, n // w
        if 2 * _manh_to_core(x, y, my_core) <= _manh_to_core(x, y, enemy_core):
            kept |= lsb
    return kept


def harvestable_ore():
    ore = possible_ore()
    # units.builder.draw_mask(ore, 255, 0, 0)
    # units.builder.draw_mask(secured(), 0, 255, 0)
    # units.builder.draw_mask(cant_harvest, 0, 0, 255)
    # Loki: no `secured()` requirement. Cambridge/Khaos needed ore boxed in before
    # harvesting so the turret feeding it couldn't be tapped by the enemy. Titan's
    # global ammo pool removes turret feeding entirely, so any reachable ore is fair
    # game — harvest it as soon as we can route to it.
    result = (ore
              & ~map_info._bm_et[map_info._IDX_HARVESTER]
              & ~cant_harvest)
    # Econ bots only harvest ore that is at least 2x closer to us than to the
    # enemy (raw Manhattan). Generalists that fall back to harvesting are not
    # restricted this way.
    if units.builder._economy_builder:
        result = _near_side_ore(result)
    return result

def _too_expensive():
    """Bitmask of tiles we know we can't afford right now."""
    ti = rc.get_global_resources()
    current = rc.get_current_round()
    result = 0
    stale = []
    for n, (cost, turn) in _cost_map.items():
        if turn + COST_MAP_TTL < current:
            stale.append(n)
            continue
        if cost > ti:
            result |= 1 << n
    for n in stale:
        del _cost_map[n]
    return result

MAX_SCORE = 4
_cached_claims = 0
target = None  # ore we're harvesting, for status logging
def score():
    global _cached_claims
    _cached_claims = _my_claims()
    return 4 if _cached_claims else 0

CARD = [Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST]


def run():
    global cant_harvest
    log("HARVEST")

    available = _cached_claims
    if not available:
        return

    w = map_info._width
    my_team_idx = map_info._my_team_idx

    best_ore = None
    path = None
    while available:
        candidate, _ = nav.closest(available)
        log("harvesting", candidate)
        if candidate is None:
            cant_harvest |= available
            return
        cand_n = candidate.x + candidate.y * w
        cand_bit = 1 << cand_n
        cand_path = None
        for dir in CARD:
            pos = map_info.pos_add(candidate, dir)
            if not map_info.in_bounds(pos):
                continue
            pn = pos.x + pos.y * w
            pbit = 1 << pn
            if not (map_info._bm_et[map_info._IDX_CONVEYOR] & pbit):
                continue
            d_idx = map_info._building_dir[pn]
            if d_idx < 0:
                continue
            conv_dir = map_info._INT_DIR[d_idx]
            if conv_dir != dir.opposite() and not (map_info._bm_conv_into_open_ore & pbit):
                cand_path = nav.calculate_conveyor_path(map_info.pos_add(pos, conv_dir), update=True)
                if cand_path is not None:
                    break
        if not cand_path:
            cand_path = nav.calculate_conveyor_path(candidate)
        if cand_path is None:
            cant_harvest |= cand_bit
            available &= ~cand_bit
            log("cant route", candidate, "— retrying")
            continue
        cost = rc.get_harvester_cost() + nav.conveyor_cost(cand_path[2], rc.get_scale_percent()/100+0.05)
        _cost_map[cand_n] = (cost, rc.get_current_round())
        if cost > rc.get_global_resources() and not units.builder._economy_builder:
            available &= ~cand_bit
            log("too expensive", candidate, "— retrying")
            continue
        best_ore = candidate
        path = cand_path
        break

    global target
    target = best_ore
    if best_ore is None:
        return

    ore_n = best_ore.x + best_ore.y * w
    ore_bit = 1 << ore_n
    ore_id = map_info._building_id[ore_n]

    if ore_id:
        is_mine = bool(map_info._bm_team[my_team_idx] & ore_bit)
        can_replace_harvester = (
            rc.get_global_resources()
            >= rc.get_harvester_cost() + map_info.builder_ti_reserve()
        )
        if is_mine and can_replace_harvester and rc.can_destroy(best_ore) and rc.get_action_cooldown() == 0 and (map_info._my_pos != best_ore or rc.get_move_cooldown() == 0):
            rc.destroy(best_ore)
            map_info.update_at(best_ore)
    # Titan 2.3.x: builds only reach CARDINAL neighbours (radius² 1), so stand on a
    # cardinal neighbour of the ore. Avoid path[0] (the first conveyor goes there,
    # and you can't build on your own tile) unless it's the only option.
    targets = set()
    log(path[0])
    for d in CARD:
        p = map_info.pos_add(best_ore, d)
        if not map_info.in_bounds(p):
            continue
        if map_info.is_passable(p):
            targets.add(p)
    if len(targets) > 1:
        targets.discard(path[0])
    if targets:
        nav.move_to(targets)
    log("targets", targets, path[0])
    # Move to any adjacent tile and build harvester
    if rc.can_build_harvester(best_ore) and rc.get_global_resources() >= rc.get_harvester_cost() + map_info.builder_ti_reserve():
        rc.build_harvester(best_ore)
        map_info.update_at(best_ore)
