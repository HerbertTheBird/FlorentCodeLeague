"""Attack / generalist builder behaviour.

Repairs any barrier it broke while pathing, runs the best available combat/
scout state (builder.select_best_state routes rush bots to [explore, attack]
and generalists to the full state list via the role flags), then falls back to
healing. Economy and reinforcement builders do neither of those bookends.
"""

import pathing
import units.builder as builder


def run() -> None:
    pathing.rebuild_broken_barriers(builder.rc)
    best = builder.select_best_state()
    if best is not None:
        best.run()
    builder.heal_fallback()
