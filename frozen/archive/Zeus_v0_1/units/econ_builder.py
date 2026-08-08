"""Economy/defense builder behavior.

These builders finish harvesting and conveyor work, but emergency core-line
barriers and confirmed anti-mirror gunners outrank routine economy work.
"""

import units.builder as builder


def run() -> None:
    best = builder.select_best_state()
    if best is not None:
        best.run()
