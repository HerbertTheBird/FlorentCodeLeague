"""Defensive builder behaviour: hold one launcher-ring lane, or run a claimed
reinforcement intercept. Lane 0 permanently converts to an economy builder once
its half of the ring is complete.
"""

import units.builder as builder
import units.def_states.defense as defense


def run() -> None:
    lane = builder._defense_lane
    # Counter-battery first: if an enemy gunner/sentinel is in view and we can
    # shoot it with a gunner built from where we stand (no moving), do that.
    if defense.counter_battery():
        builder.heal_fallback()
        return
    if defense.run(lane):
        # Ring complete for this lane -> permanently an economy builder.
        builder._defense_lane = None
        builder._economy_builder = True
        import units.econ_builder as econ_builder
        econ_builder.run()
        return
    builder.heal_fallback()


def run_reinforcement() -> None:
    # A reinforcement builder mirrors a claimed enemy exactly; it neither builds
    # nor heals.
    defense.run_reinforcement(
        builder._reinforcement_enemy_id,
        builder._reinforcement_position,
        builder._reinforcement_launched,
    )
