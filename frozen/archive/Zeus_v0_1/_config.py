# 1.0 for full cost, lower = discounted cost to more proactively start routes
CONVEYOR_COST_DISCOUNT = 0.65

# Opening composition: the first NUM_ATTACK builders spawned are attackers, the
# next NUM_ECON are economy builders. Single source of truth — change these and
# the split flows through spawn count, role assignment, and launcher targeting.
# (Defined here, with no imports, so both comms and spawn_plan can read it without
# a circular import.)
NUM_ATTACK = 2
NUM_ECON = 2
INITIAL_SPAWN_COUNT = NUM_ATTACK + NUM_ECON
