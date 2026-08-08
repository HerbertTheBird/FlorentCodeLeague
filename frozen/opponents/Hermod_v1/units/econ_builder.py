"""Hermod economy/defense builder: one assigned ore route, then base camp."""

import units.builder as builder


def run() -> None:
    best = builder.select_best_state()
    if best is not None:
        best.run()
