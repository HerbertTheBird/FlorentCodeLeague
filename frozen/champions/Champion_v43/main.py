def has_op() -> bool:
    """True iff this unit still has its single move-or-action for this turn.

    Both cooldowns being zero is the only gate-aware check: a move sets only the
    move cooldown and an action sets only the action cooldown, but doing either
    blocks the other for the turn. (can_act() alone misses the move case.)

    Reads the controller from `map_info._rc`, NOT a main-level global: the engine
    can load this file under a different module name than the `main` that other
    modules import has_op from, so a main-level global would not be shared. But
    `import map_info` is absolute + cached, so map_info is the same module
    everywhere; its `_rc` is set in map_info.init() and stable across turns.
    (map_info is bound by the `import map_info` below before any runtime call;
    has_op is still defined here at the top so other modules can import it
    without a circular-import failure.)"""
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

class Player:
    def __init__(self):
        self.initialized = False
        self.me: ModuleType
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

                map_info.init(c)
                comms.init(c)
                self.me.init(c)
                self.current_round = round_num
                self.spawn_turn = round_num
                self.initialized = True

            self.me.run()


        except Exception as e:
            print("Error:", e)
            print(f"Error: {e}", file=sys.stderr)
