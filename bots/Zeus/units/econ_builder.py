"""Economy builder behaviour: harvest ore and lay conveyor routes only (the
[harvest, route] subset, selected because the _economy_builder flag is set). A
strict specialist — no defensive construction and no healing.
"""

import units.builder as builder


def run() -> None:
    best = builder.select_best_state()
    if best is not None:
        best.run()
