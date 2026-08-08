"""Attack / generalist builder behaviour.

Zeus has no launcher ring and no launch-based fast-travel: attack bots walk the
whole way. They just run their normal state loop — explore toward the
symmetry-predicted enemy core, and attack when in range. Generalists (later
builders, _atk_bot False) run the same full state loop.
"""

import pathing
import units.builder as builder


def run() -> None:
    pathing.rebuild_broken_barriers(builder.rc)
    best = builder.select_best_state()
    if best is not None:
        best.run()
    builder.heal_fallback()
