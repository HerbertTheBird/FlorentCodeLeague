# 1.0 for full cost, lower = discounted cost to more proactively start routes
CONVEYOR_COST_DISCOUNT = 0.65

# Opening composition: the first builder attacks immediately and the remaining
# three run economy/defense.
# (Defined here, with no imports, so both comms and spawn_plan can read it without
# a circular import.)
NUM_ATTACK = 1
NUM_ECON = 3
INITIAL_SPAWN_COUNT = NUM_ATTACK + NUM_ECON

# Legacy chip-offense timing knobs remain available if an attacker is restored.
OFFENSE_EARLIEST_ROUND = 45
OFFENSE_FALLBACK_ROUND = 120
ECON_READY_TO_ATTACK = 2
CHIP_STEP_INTERVAL = 7
