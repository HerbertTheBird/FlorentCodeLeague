"""Attack / generalist builder behaviour.

Attack bots travel to the enemy fast by leapfrogging launchers: while still far
from their symmetry-predicted enemy core they build a launcher toward it and
hold, and a launcher (this one, or a defender's ring launcher) flings them ~5
tiles forward — landing them beside the next chain launcher when one exists.
Once close they fall back to the normal combat/scout states. Generalists (later
builders, _atk_bot False) just run the full state loop.
"""

from fcode import Direction

import map_info
import pathing
import units.builder as builder
import units.def_states.defense as defense

_CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)

# Only launch-travel while the target is at least this far (Manhattan); closer in
# we attack/explore normally.
_LAUNCH_MIN_MANHATTAN = 8
# Keep this much titanium beyond the launcher cost so the economy isn't starved.
_LAUNCH_RESERVE = 10
# Turns to hold beside a launcher before giving up and walking.
_MAX_LAUNCH_WAIT = 3

_launch_wait = 0
action = None    # "wait-launch" / "build-launcher" while leapfrogging, for status


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
    if _manhattan(my_pos, target) < _LAUNCH_MIN_MANHATTAN:
        _launch_wait = 0
        return False   # close to the enemy — attack/explore normally

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

    # Not beside a launcher and far from the target: get a launcher to be flung
    # from. Prefer building it where the defenders were going to put one anyway
    # (a planned ring site ahead of us) so the launcher does double duty; move to
    # that site if it isn't adjacent yet. Only once past the ring do we drop our
    # own chain launcher toward the target.
    if rc.get_global_resources() < rc.get_launcher_cost() + map_info.builder_ti_reserve() + _LAUNCH_RESERVE:
        return False
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
    spot = _launcher_spot(my_pos, target, launchers)
    if spot is not None and rc.can_build_launcher(spot):
        rc.build_launcher(spot)
        action = "build-launcher"
        return True
    return False


def _cardinal_adjacent(a, b) -> bool:
    return abs(a.x - b.x) + abs(a.y - b.y) == 1


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


def _launcher_spot(my_pos, target, launchers):
    """A cardinal neighbour to build a launcher on: buildable, and — to keep the
    chain vision-connected — within vision^2 <= 26 of an existing friendly
    launcher when there is one. Prefer the neighbour closest to the target."""
    rc = builder.rc
    candidates = []
    for d in _CARDINALS:
        p = map_info.pos_add(my_pos, d)
        if not map_info.in_bounds(p) or not rc.can_build_launcher(p):
            continue
        if launchers and not any(p.distance_squared(lp) <= 26 for lp in launchers):
            continue    # would break chain vision connectivity
        candidates.append(p)
    if not candidates:
        return None
    return min(candidates, key=lambda p: _manhattan(p, target))
