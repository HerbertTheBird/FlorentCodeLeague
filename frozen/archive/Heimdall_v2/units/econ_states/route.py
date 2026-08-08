import map_info
import pathing
from pathing import Pathing
from fcode import *
import units.builder
from log import log
rc: Controller = None
nav: Pathing = None
_cost_map: dict[int, tuple[int, int]] = {}  # tile index -> (min titanium cost, round recorded)
COST_MAP_TTL = 100

unpathable = 0


def _can_build_preferred_conveyor(pos: Position, direction: Direction) -> bool:
    return (
        rc.can_build_conveyor(pos, direction)
        and rc.get_global_resources() >= rc.get_conveyor_cost() + map_info.builder_ti_reserve()
    )


def _build_preferred_conveyor(pos: Position, direction: Direction) -> EntityType:
    rc.build_conveyor(pos, direction)
    return EntityType.CONVEYOR

def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav

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
        log("cost of", n%map_info._width, n//map_info._width, cost)
        if cost > ti:
            result |= 1 << n
    for n in stale:
        del _cost_map[n]
    return result

def _dead_end_conveyors():
    """Bitmask of routable conveyors whose output is not connected to my ore-accepting network."""
    return map_info._bm_dead_end & ~map_info._bm_enemy_turret_threat
def not_blocked():
    '''
    it is not blocked if
    it does not have a conveyor taking it
    and it does have a place to put a conveyor
    '''
    my_team_idx = map_info._my_team_idx
    my_connected = (
        map_info._bm_et[map_info._IDX_SPLITTER]
        | map_info._bm_et[map_info._IDX_CORE]
    ) & map_info._bm_team[my_team_idx]
    w = map_info._width
    conveyors = map_info._bm_et[map_info._IDX_CONVEYOR] & map_info._bm_team[my_team_idx]
    left_conveyors = ((conveyors&~map_info._bm_conv_by_dir[map_info._DIR_E])&map_info._not_right_col)<<1
    right_conveyors = ((conveyors&~map_info._bm_conv_by_dir[map_info._DIR_W])&map_info._not_left_col)>>1
    up_conveyors = ((conveyors&~map_info._bm_conv_by_dir[map_info._DIR_S]))<<w
    down_conveyors = ((conveyors&~map_info._bm_conv_by_dir[map_info._DIR_N]))>>w
    blocking = (
        (map_info._bm_team[1-my_team_idx])
        | ~map_info._bm_env[map_info._IDX_ENV_EMPTY]
        | (map_info._bm_team[my_team_idx]
        & ~map_info._bm_et[map_info._IDX_CONVEYOR])
    )
    already_routed = map_info.expand_manhattan(my_connected) | left_conveyors | right_conveyors | up_conveyors | down_conveyors
    bottom_row = ((1 << w) - 1) << (w * (map_info._height - 1))
    top_row = (1 << w) - 1
    blocked = (
        (((blocking & map_info._not_left_col) >> 1) | ~map_info._not_right_col)
        & (((blocking & map_info._not_right_col) << 1) | ~map_info._not_left_col)
        & ((blocking >> w) | bottom_row)
        & ((blocking << w) | top_row)
    )
    return map_info._board_mask & ~already_routed & ~blocked & ~map_info._bm_enemy_turret_threat

def _orphan_harvesters(not_blocked_mask: int):
    my_harvesters = map_info._bm_et[map_info._IDX_HARVESTER]
    if not my_harvesters:
        return 0
    return my_harvesters & not_blocked_mask
def cant_claim():
    w = map_info._width
    my_pos = map_info._my_pos
    my_bit = 1 << (my_pos.x + my_pos.y * w)
    cant = map_info._bm_others_3x3 & ~map_info.expand_manhattan(my_bit)
    return cant
def _my_claims():
    w = map_info._width
    my_mask = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    # Economy builders must finish an existing route even when the team cannot
    # currently afford its remaining conveyors. Keep the endpoint claimable so
    # route remains selected; run() will wait and retry until Ti is available.
    avoid = cant_claim() | unpathable
    if not units.builder._economy_builder:
        avoid |= _too_expensive()
    not_blocked_mask = not_blocked()
    candidates = (
        _dead_end_conveyors()
        | _orphan_harvesters(not_blocked_mask)
    ) & ~avoid
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, candidates, tie_self=True)

_cached_claims = 0
target = None  # tile we're routing, for status logging

MAX_SCORE = 7.75
def score():
    global _cached_claims
    units.builder.draw_mask(map_info._bm_dead_end, 0, 0, 255)
    _cached_claims = _my_claims()

    important = 0 if units.builder._economy_builder else (
        map_info.expand_manhattan(map_info._bm_enemy_bots, 5)
        & ~(map_info._bm_team[map_info._my_team_idx] & map_info._bm_et[map_info._IDX_HARVESTER])
    )
    if important&_cached_claims:
        log("IMPORTANT")
        _cached_claims &= important
        return 7.75
    return 5 if _cached_claims else 0

def run():

    global unpathable
    log("ROUTE")
    candidates = _cached_claims
    high_priority = False
    important = 0 if units.builder._economy_builder else (
        map_info.expand_manhattan(map_info._bm_enemy_bots, 5)
        & ~(map_info._bm_team[map_info._my_team_idx] & map_info._bm_et[map_info._IDX_HARVESTER])
    )

    if important & candidates:
        high_priority = True
        candidates &= important
    if not candidates:
        log("no candidates?")
        return
    width = map_info._width

    def _try_barrier_output(target_n):
        output_n = map_info._building_conv_target[target_n]
        if output_n < 0:
            return False
        output_bit = 1 << output_n
        if output_bit & (map_info._bm_friendly_bots | map_info._bm_enemy_bots):
            return False
        output = Position(output_n % width, output_n // width)
        output_type = map_info.type_at(output.x, output.y)
        if output_type is not None:
            return False

        my_pos = map_info._my_pos
        if abs(my_pos.x - output.x) + abs(my_pos.y - output.y) > 1:  # Titan: cardinal build reach
            if not nav.move_to_adjacent(output):
                return False

        if rc.can_build_barrier(output) and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.builder_ti_reserve():
            rc.build_barrier(output)
            map_info.update_at(output)
        return True

    def fallback_barrier(target):
        log("barrier fallback at", target)
        target_n = target.x + target.y * width
        barrier_ready = (
            rc.get_action_cooldown() == 0
            and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.builder_ti_reserve()
        )

        if barrier_ready and _try_barrier_output(target_n):
            return

        nav.move_adjacent(target)
        existing = map_info.type_at(target.x, target.y)
        # Only destroy the routed tile for free when it immediately cuts an
        # enemy feed, or when we can still turn it into a barrier this turn.
        if existing == EntityType.CONVEYOR and rc.can_destroy(target) and barrier_ready:
            rc.destroy(target)
            map_info.update_at(target)
        if rc.can_build_barrier(target) and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.builder_ti_reserve():
            rc.build_barrier(target)
            map_info.update_at(target)

    best = None
    path = None
    target_conveyor = [None]*2
    is_harvester = False

    while candidates:
        candidate, _ = nav.closest(candidates)
        if candidate is None:
            log("no closest???")
            unpathable |= candidates
            return
        cand_n = candidate.x + candidate.y * width
        cand_bit = 1 << cand_n
        cand_is_harvester = bool(map_info._bm_et[map_info._IDX_HARVESTER] & cand_bit)
        cand_path = nav.calculate_conveyor_path(candidate, update=not cand_is_harvester)
        if not cand_is_harvester:
            log("PATH", cand_path)
        if cand_path is None:
            if high_priority:
                fallback_barrier(candidate)
                return
            unpathable |= cand_bit
            candidates &= ~cand_bit
            continue
        cost = nav.conveyor_cost(cand_path[2])
        _cost_map[cand_n] = (cost, rc.get_current_round())
        if rc.get_global_resources() < cost:
            log("can't afford", cost)
            if high_priority:
                fallback_barrier(candidate)
                return
            candidates &= ~cand_bit
            continue
        best = candidate
        path = cand_path
        target_conveyor = [path[0], path[1]]
        is_harvester = cand_is_harvester
        break

    global target
    target = best
    if best is None:
        return

    best_n = best.x + best.y * width
    best_bit = 1 << best_n
    near_enemy = False
    if target_conveyor[0].distance_squared(target_conveyor[1]) == 1:
        tc1_zone = 1 << (target_conveyor[1].x + target_conveyor[1].y * width)
        for _ in range(4):
            tc1_zone = map_info.expand_manhattan(tc1_zone)  # enemy walk reach
        if tc1_zone & map_info._bm_enemy_bots:
            near_enemy = True
    if (not units.builder._economy_builder and near_enemy
            and map_info.team_at(target_conveyor[1].x, target_conveyor[1].y) != rc.get_team()
            and map_info.team_at(target_conveyor[0].x, target_conveyor[0].y) == rc.get_team()):
        nav.move_to(target_conveyor[1])
        if map_info._my_pos == target_conveyor[1]:
            if map_info.team_at(target_conveyor[1].x, target_conveyor[1].y) != map_info._my_team and rc.can_fire(target_conveyor[1]):
                rc.fire(target_conveyor[1])
                map_info.update_at(target_conveyor[0])
        return
    def attempt_build():
        destroy = target_conveyor[0]
        next = target_conveyor[1]
        # Loki: no bridges in Titan — the route search is cardinal-only, so every
        # step must be cardinally adjacent. If a non-cardinal step ever slips
        # through, bail loudly instead of silently stalling the chain.
        if destroy.distance_squared(next) > 1:
            log("route: non-cardinal step", destroy, "->", next, "— skipping (no bridges)")
            return
        cost = rc.get_conveyor_cost() + map_info.builder_ti_reserve()
        if rc.can_destroy(destroy) and rc.get_action_cooldown() == 0 and rc.get_global_resources() >= cost:
            rc.destroy(destroy)
            map_info.update_at(destroy)
        direction = map_info.direction_to(destroy, next)
        if _can_build_preferred_conveyor(destroy, direction):
            _build_preferred_conveyor(destroy, direction)
            map_info.update_at(destroy)
    # Titan 2.3.x: move and action share ONE cooldown (engine-verified: build
    # after moving and move after building are both rejected), so a conveyor
    # takes a move-turn then a build-turn. Build first when already adjacent;
    # otherwise spend the turn approaching a cardinal neighbour of the site
    # (own-tile and diagonal builds are illegal in 2.3.x).
    attempt_build()
    nav.move_adjacent(target_conveyor[0])
