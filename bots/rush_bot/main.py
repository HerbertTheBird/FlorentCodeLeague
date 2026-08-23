"""rushdown -- a copy of not adgato v25's sentinel rush, plus a small-economy
arm for big maps.

# What the reference bot does

Read off five replays of not adgato v25 beating Pantheon v105 5-0, cores
destroyed on turns 59, 68, 65, 58 and 70:

    t   0  spawn ONE builder
    t 34-52  walk it to the enemy core   (no conveyor, no harvester, ever)
    t  +0  build a launcher beside it     (3 of the 5 games)
    t  +1..+4  build 4-5 SENTINELS on tiles whose fixed line of fire crosses
               the enemy core, on the OUTSIDE face of it
    t +14..+20  core dead

That is the entire bot: six units, no economy, 91% on the ladder over 56
matches. Four sentinels are 36 damage a round into a 500 HP core, which is
fourteen rounds, and the defender's builders are all out in the middle of the
map building conveyors when it lands.

# What this copy changes

Above config.ECON_MIN_AREA the core may spawn up to two extra builders, but only
for ore it can already see within a short route home, and those builders never
defend -- they harvest, route, and then heal. The reasoning is in config.py: the
walk is eighteen rounds longer on a 30x30 map than on a 20x20 one, which is
another 200 ammunition against only 25 more titanium of passive income, so the
big-map version of this rush is short of AMMUNITION rather than short of
defenders. Defending would spend builder turns on the thing that is not scarce.

Roles need no comms: the core spawns the rusher on round 0 and nothing else on
round 0, so a builder is the rusher exactly when it first ran on round 0.

Every turn is wrapped, because an exception escaping run() does not merely lose
the turn -- the engine destroys that unit permanently, and this bot has one
builder.
"""

from fcode import Controller, EntityType

import config
import core as core_role
import roles


class Player:
    def __init__(self):
        self.st = None
        self.kind = None

    def run(self, ct: Controller) -> None:
        try:
            if self.st is None:
                self.st = roles.State(ct)
                self.kind = ct.get_entity_type()
                if self.kind == EntityType.BUILDER_BOT:
                    # The core published the rusher's id on the turn it spawned
                    # it, one round before this unit's first run, so the value
                    # is already visible. Falling back on the spawn round covers
                    # the impossible case of the slot being unwritten.
                    named = ct.read_store(config.SLOT_RUSHER_ID)
                    self.st.is_rusher = (named == ct.get_id() if named
                                         else self.st.spawn_round <= 1)

            self.st.board.observe(ct)
            roles._note_visible_cores(ct, self.st)

            if self.kind == EntityType.CORE:
                core_role.core_turn(ct, self.st)
            elif self.kind == EntityType.BUILDER_BOT:
                if self.st.is_rusher:
                    roles.rusher_turn(ct, self.st)
                else:
                    roles.eco_turn(ct, self.st)
            elif self.kind == EntityType.SENTINEL:
                roles.sentinel_turn(ct, self.st)
            elif self.kind == EntityType.LAUNCHER:
                roles.launcher_turn(ct, self.st)
        except Exception as exc:            # never let a unit be destroyed
            try:
                import sys, traceback
                print("rushdown error:", type(exc).__name__, exc,
                      file=sys.stderr, flush=True)
                traceback.print_exc(file=sys.stderr)
            except Exception:
                pass
