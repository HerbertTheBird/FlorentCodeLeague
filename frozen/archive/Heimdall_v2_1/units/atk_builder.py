"""Attack/generalist builder behavior and the coordinated two-bot siege.

v2.1 restores v0's ring-first opening: dedicated defenders build launchers and
attackers walk from the core. Attackers never build or wait for launchers.
"""

import map_info
import pathing
import units.builder as builder
import units.atk_states.siege as siege

action = None


def run() -> None:
    global action
    action = None
    if builder._atk_bot and builder._atk_index in (0, 1):
        action_taken = siege.run(builder._atk_index)
        if action_taken:
            action = siege.phase
            builder.heal_fallback()
            return
    pathing.rebuild_broken_barriers(builder.rc)
    best = builder.select_best_state()
    if best is not None:
        best.run()
    builder.heal_fallback()
