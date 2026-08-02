"""Attack / generalist builder behaviour.

Attack bots use launchers only at the two ends of their trip, never across the
middle: near base they build a launcher at a planned defender ring site (double
duty — it defends and flings them outward), then they WALK across the map, and
once the enemy core is confirmed and close enough that a launcher they build
could throw them to a tile adjacent to it, they build that final-jump launcher
and are flung right next to the core. A launcher beside them (theirs or a
defender's) picks them up and throws them toward the core. Once adjacent they
fall back to the normal combat/scout states. Generalists (later builders,
_atk_bot False) just run the full state loop.
"""

from fcode import Direction, Position

import map_info
import pathing
import units.builder as builder
import units.def_states.defense as defense

_CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)

# Below this Manhattan distance to the target we attack/explore normally (unless
# a final jump to the core is available).
_LAUNCH_MIN_MANHATTAN = 8
# Keep this much titanium beyond the launcher cost so the economy isn't starved.
_LAUNCH_RESERVE = 10
# Turns to hold beside a launcher before giving up and walking.
_MAX_LAUNCH_WAIT = 3
# Launcher throw range (dist^2).
_THROW_RANGE_SQ = 26

_launch_wait = 0
action = None    # "wait-launch" / "build-launcher" / "goto-ring" for status


def run() -> None:
    if _travel_by_launcher():
        return
    pathing.rebuild_broken_barriers(builder.rc)
    best = builder.select_best_state()
    if best is not None:
        best.run()
    builder.heal_fallback()


def _manhattan(a, b) -> int:
    return abs(a.x - b.x) + abs(a.y - b.y)


def _friendly_launchers():
    mine = (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    return list(map_info.iter_mask(mine))


def _travel_by_launcher() -> bool:
    """Returns True if this turn was spent traveling by launcher (built one, or
    held beside one to be flung) — the caller then skips the normal states."""
    global _launch_wait, action
    action = None
    if not builder._atk_bot:
        return False
    rc = builder.rc
    target = builder.atk_symmetry_target()
    if target is None:
        return False
    my_pos = map_info._my_pos

    launchers = _friendly_launchers()
    # Beside a friendly launcher: hold still so it can pick us up and fling us.
    if any(max(abs(lp.x - my_pos.x), abs(lp.y - my_pos.y)) <= 1 for lp in launchers):
        _launch_wait += 1
        if _launch_wait <= _MAX_LAUNCH_WAIT:
            action = "wait-launch"
            return True     # wait to be launched (do not move away)
        _launch_wait = 0    # never launched — give up and walk this turn
        return False
    _launch_wait = 0

    if rc.get_global_resources() < rc.get_launcher_cost() + map_info.builder_ti_reserve() + _LAUNCH_RESERVE:
        return False

    # FINAL JUMP: once the core is confirmed and a launcher we build could fling
    # us to a tile adjacent to it, build that launcher. Checked before the close
    # cutoff so it fires right at the end of the approach.
    if _core_confident():
        spot = _final_jump_spot(my_pos, target)
        if spot is not None:
            rc.build_launcher(spot)
            action = "build-launcher"
            return True

    if _manhattan(my_pos, target) < _LAUNCH_MIN_MANHATTAN:
        return False   # close, no final jump available — attack/explore normally

    # DEFENSE: build a launcher where the defenders were going to put one anyway
    # (a planned ring site ahead of us, near base), moving to it if not adjacent.
    # Nothing is built across the middle of the map — we just walk it.
    site = _nearest_ring_site(my_pos, target)
    if site is not None:
        if _cardinal_adjacent(my_pos, site):
            if rc.can_build_launcher(site):
                rc.build_launcher(site)
                action = "build-launcher"
                return True
        elif builder.nav.move_to(site):
            action = "goto-ring"
            return True
    return False   # walk the middle — no chain launchers


def _cardinal_adjacent(a, b) -> bool:
    return abs(a.x - b.x) + abs(a.y - b.y) == 1


def _core_confident() -> bool:
    """We only commit the final-jump launcher once we actually know where the
    enemy core is (symmetry confirmed or the core seen), not on a guess."""
    return map_info._solved_sym or map_info._their_core is not None


def _bot_passable(p) -> bool:
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


def _enemy_core_ring(core_origin):
    """Bot-passable tiles cardinally/diagonally around the enemy 2x2 core."""
    tiles = []
    for x in range(core_origin.x - 1, core_origin.x + 3):
        for y in range(core_origin.y - 1, core_origin.y + 3):
            if core_origin.x <= x <= core_origin.x + 1 and core_origin.y <= y <= core_origin.y + 1:
                continue  # the core footprint itself
            p = Position(x, y)
            if _bot_passable(p):
                tiles.append(p)
    return tiles


def _final_jump_spot(my_pos, core_origin):
    """A cardinal neighbour to build a launcher on such that it could throw us to
    a tile adjacent to the enemy core (within throw range). None if not close
    enough yet."""
    ring = _enemy_core_ring(core_origin)
    if not ring:
        return None
    for d in _CARDINALS:
        launcher = map_info.pos_add(my_pos, d)
        if not map_info.in_bounds(launcher) or not builder.rc.can_build_launcher(launcher):
            continue
        if any(launcher.distance_squared(t) <= _THROW_RANGE_SQ for t in ring):
            return launcher
    return None


def _nearest_ring_site(my_pos, target):
    """Nearest planned defender ring launcher site that is ahead of us (no
    farther from the target than we are) and close by. None once the ring is
    covered or we've moved past it."""
    try:
        sites = defense._calculate_tiling()
    except Exception:
        return None
    if not sites:
        return None
    my_to_target = _manhattan(my_pos, target)
    ahead = [
        s for s in sites
        if _manhattan(s, target) <= my_to_target and _manhattan(my_pos, s) <= 12
    ]
    if not ahead:
        return None
    return min(ahead, key=lambda s: _manhattan(my_pos, s))
