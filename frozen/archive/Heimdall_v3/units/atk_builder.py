"""Attack / generalist builder behaviour.

Attack bots may use the defender's compact base launcher setup, but never build
launchers themselves. After the base throw they walk to the enemy core and use
the normal combat/scout states. Generalists (later builders, ``_atk_bot False``)
just run the full state loop.
"""

import map_info
import pathing
import units.builder as builder

# Turns to hold beside a launcher before giving up and walking.
_MAX_LAUNCH_WAIT = 3

_launch_wait = 0
action = None    # "wait-launch" for status


def run() -> None:
    if _travel_by_launcher():
        return
    pathing.rebuild_broken_barriers(builder.rc)
    best = builder.select_best_state()
    if best is not None:
        best.run()
    builder.heal_fallback()


def _friendly_launchers():
    mine = (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    return list(map_info.iter_mask(mine))


def _travel_by_launcher() -> bool:
    """Return True while holding beside a launcher to be flung."""
    global _launch_wait, action
    action = None
    if not builder._atk_bot:
        return False
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

    return False   # walk from here; only the defender builds launchers in v3
