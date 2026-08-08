"""Hermod attacker: launcher insertion, two sentinels, then barrier guard."""

import pathing
import units.builder as builder


def run() -> None:
    pathing.rebuild_broken_barriers(builder.rc)
    best = builder.select_best_state()
    if best is not None:
        best.run()
    builder.heal_fallback()
