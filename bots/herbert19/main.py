def has_op() -> bool:
    """True iff this unit still has its single move-or-action for this turn.
    Despite being separate cooldowns, both need to be zero for the unit to
    be able to move or make an action."""
    return map_info._rc.get_action_cooldown() == 0 and map_info._rc.get_move_cooldown() == 0


from fcode import Controller, EntityType

import random
import sys
from types import ModuleType

import units.builder as builder
import units.core as core
import units.turret_gunner as gunner
import units.turret_sentinel as sentinel
import units.turret_launcher as launcher
import map_info
import comms
import metrics

class Player:
    def __init__(self):
        self.initialized = False
        self.me: ModuleType | None = None
        self.spawn_turn = 0
        self.current_round: int = None


    def run(self, c: Controller) -> None:
        round_num = c.get_current_round()

        try:
            etype = c.get_entity_type()

            if not self.initialized:
                random.seed(c.get_id())

                if etype == EntityType.CORE:
                    self.me = core
                elif etype == EntityType.BUILDER_BOT:
                    self.me = builder
                elif etype == EntityType.GUNNER:
                    self.me = gunner
                elif etype == EntityType.SENTINEL:
                    self.me = sentinel
                elif etype == EntityType.LAUNCHER:
                    self.me = launcher
                else:
                    # Unknown/unhandled entity type. Previously self.me was left
                    # unset while initialized was still flipped to True, so this
                    # unit raised "'NoneType' object has no attribute 'run'" EVERY
                    # turn for the rest of the match -- swallowed by the except
                    # below, so it just looked like a dead unit. Stay uninitialised
                    # and do nothing instead; if the engine ever hands us a type we
                    # do handle later, we will pick it up then.
                    self.me = None

                if self.me is None:
                    return

                map_info.init(c)
                comms.init(c)
                metrics.init(c)
                self.me.init(c)
                self.current_round = round_num
                self.spawn_turn = round_num
                self.initialized = True

            if self.me is None:
                return
            self.me.run()


        except Exception as e:
            print("Error:", e)
            print(f"Error: {e}", file=sys.stderr)
