"""Launcher-camping and exact-mirroring builders for Heimdall v3."""

import units.builder as builder
import units.def_states.defense as defense


def run() -> None:
    lane = builder._defense_lane
    defense.run(lane)
    builder.heal_fallback()


def run_reinforcement() -> None:
    defense.run(1)
