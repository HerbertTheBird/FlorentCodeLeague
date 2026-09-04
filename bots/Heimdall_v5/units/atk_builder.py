"""Economy-gated chip attacker and emergency front-line defender."""

import pathing
import units.builder as builder


def run() -> None:
    pathing.rebuild_broken_barriers(builder.rc)
    best = builder.select_best_state()
    if best is not None:
        best.run()
    builder.heal_fallback()
