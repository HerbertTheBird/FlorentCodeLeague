from fcode import Controller, Direction, Position
import comms
import map_info
import units.launch_plan as plan
from units.spawn_plan import choose_spawn_plan, draw_spawn_plan, INITIAL_SPAWN_COUNT, INITIAL_EXPLORE_MAX_STEPS

rc: Controller

_spawn_plan: list[Direction] | None = None
_num_spawned = 0
_core_area: tuple[Position, ...] = ()
# Opening builder ids by spawn index (first NUM_ATTACK are attackers, rest are
# economy — see spawn_plan). Broadcast to comms so each builder learns its role.
_opening_ids = [0] * INITIAL_SPAWN_COUNT


def _core_area_positions(pos: Position, width: int, height: int) -> tuple[Position, ...]:
    # Titan core is 2x2 with top-left = pos (get_position()). Builders may spawn
    # up to two tiles from any core tile. Offer that full clipped region to
    # can_spawn(); the controller remains the authority on exact legality.
    footprint = {(pos.x + dx, pos.y + dy) for dx in (0, 1) for dy in (0, 1)}
    return tuple(
        Position(x, y)
        for x in range(pos.x - 2, pos.x + 4)
        for y in range(pos.y - 2, pos.y + 4)
        if 0 <= x < width and 0 <= y < height and (x, y) not in footprint
    )


def init(c: Controller):
    global rc, _core_area
    rc = c
    _core_area = _core_area_positions(
        rc.get_position(), rc.get_map_width(), rc.get_map_height()
    )


def _record_opening_spawn(spawn_index: int, builder_id: int) -> None:
    """Record an opening builder's id by spawn index (role is derived from the
    index in comms). The broadcast happens once per round in run()."""
    global _num_spawned
    if 0 <= spawn_index < INITIAL_SPAWN_COUNT:
        _opening_ids[spawn_index] = builder_id
    _num_spawned += 1


def _launcher_spawn_tile():
    """A spawnable core tile beside the launcher position. Before the launcher is
    built we spawn cardinally adjacent to it (so the first builder can build it);
    once it's up we spawn on any of the 8 surrounding tiles (all within pickup
    range) so builders queue to be flung. None if the launcher tile isn't known
    yet or no legal tile is free."""
    L = plan.launcher_position()
    if L is None:
        return None
    w = map_info._width
    have_launcher = bool(
        (map_info._bm_et[map_info._IDX_LAUNCHER]
         & map_info._bm_team[map_info._my_team_idx]) & (1 << (L.x + L.y * w))
    )
    def ok(p):
        cheb = max(abs(p.x - L.x), abs(p.y - L.y))
        manh = abs(p.x - L.x) + abs(p.y - L.y)
        return (cheb == 1) if have_launcher else (manh == 1)
    cands = [p for p in _core_area if ok(p) and rc.can_spawn(p)]
    if not cands:
        cands = [p for p in _core_area if rc.can_spawn(p)]
    if not cands:
        return None
    return min(cands, key=lambda p: (p.distance_squared(L), p.x, p.y))


def _spawn_toward_plan(core_pos: Position) -> bool:
    if _spawn_plan is None or _num_spawned >= INITIAL_SPAWN_COUNT:
        return False
    spawn_index = _num_spawned
    tile = _launcher_spawn_tile()
    if tile is None:
        return False
    builder_id = rc.spawn_builder(tile)
    _record_opening_spawn(spawn_index, builder_id)
    return True


def run():
    global _spawn_plan
    # if rc.get_current_round() == 200:
        
    #     rc.resign()
    # Sync round info
    map_info.update()
    # Identify the map from our own core origin and publish its id, then load
    # the whole board (walls, ore, both cores, symmetry) locally. Every other
    # unit reads the id and loads the same board — no per-tile pooling needed.
    comms.publish_identified_map()
    titanium = rc.get_global_resources()




    core_pos = map_info._my_pos
    
    # Initialize spawn plan
    if _spawn_plan is None:
        _spawn_plan = choose_spawn_plan(rc, core_pos, INITIAL_SPAWN_COUNT)
    if rc.get_current_round() <= INITIAL_SPAWN_COUNT + INITIAL_EXPLORE_MAX_STEPS:
        draw_spawn_plan(rc, core_pos, _spawn_plan, rc.get_map_width(), rc.get_map_height())

    # Fixed roster: spawn exactly the opening 4 builders (2 attack + 2 econ) and
    # never grow past it — no reactive or economy-gated growth spawns.
    if _num_spawned < INITIAL_SPAWN_COUNT:
        _spawn_toward_plan(core_pos)

    # Rebroadcast the 2-attack / 2-economy opening role ids (buffered writes need
    # repeating until every builder has recognized its assignment).
    if any(_opening_ids):
        comms.rebroadcast_opening(_opening_ids)
                
    # Global ammo (Titan 2.3.x): turrets fire from a team-wide pool, filled only
    # by the core converting titanium 1:1 (at most once per turn). Slot 15 now
    # carries emergency defender claims, so count locally-known allied gunners
    # and keep the pool topped up to 2 * (gunners + 1).
    ammo = rc.get_global_ammo()
    # The core's vision is local, so it can't see gunners built out on the map;
    # counting only locally-visible ones capped the target at 2 (one gunner) and
    # starved the rest. Use the team-wide counter builders maintain, floored by
    # whatever the core can see itself.
    local_gunners = (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[map_info._my_team_idx]
    ).bit_count()
    allied_gunners = max(comms.gunner_count(), local_gunners)
    ammo_target = 2 * (allied_gunners + 1)
    if ammo < ammo_target:
        amt = min(ammo_target - ammo, titanium)
        if amt > 0 and rc.can_convert_ammo(amt):
            rc.convert_ammo(amt)
