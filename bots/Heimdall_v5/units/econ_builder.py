"""Economy-first builder that interrupts routes only for local defense."""

import units.builder as builder


def run() -> None:
    best = builder.select_best_state()
    if best is not None:
        best.run()
