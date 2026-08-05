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

# Feature toggles (env-overridable for A/B ablation; defaults = current v0_2).
import os as _os
def _flag(name, default):
    return _os.environ.get(name, "1" if default else "0") == "1"

# killbox build + trap launchers + killbox landing + keep-clear zone.
# Default OFF: the killbox consumes one of our two econ bots, starving the early
# economy; on weak-economy maps the OTHER bot then deadlocks on a route it can't
# afford (stands idle). The pricier new-balance gunner (20 Ti) makes this worse.
# Enable with Z_KILLBOX=1 to develop it.
KILLBOX_ENABLED = _flag("Z_KILLBOX", False)
# friendly barriers block movement (no walking through / destroying your own)
BARRIER_WALKTHROUGH_ILLEGAL = _flag("Z_BARRIER", True)
# gunner-ray claims stop at buildings, not just walls
GUNNER_CLAIMS_STOP_AT_BUILDINGS = _flag("Z_CLAIMS", True)
# attack never places a gunner aimed at a friendly builder
ATTACK_AVOID_FRIENDLY_FACING = _flag("Z_ATTACK", True)
