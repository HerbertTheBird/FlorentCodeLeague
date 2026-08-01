"""Compatibility shim: Khaos was written for Cambridge Battlecode's ``cambc`` API.

Titan's ``fcode`` API is a strict subset of ``cambc`` with *identical* class and
method names, so we just re-export it. The Cambridge mechanics that Titan removed
— axionite, breach turret, bridge, foundry, road, armoured conveyor, and tile
markers — have had their code paths stripped out of Khaos, so their enum members,
controller methods, and GameConstants fields are intentionally absent here. Any
lingering reference to one is a porting bug and should raise loudly.
"""

from fcode import *  # noqa: F401,F403  (Controller, EntityType, Environment, Direction, Position, Team, ResourceType, GameError, GameConstants, ...)
from fcode import (  # explicit re-export so linters/IDEs resolve the names
    Controller,
    EntityType,
    Environment,
    Direction,
    Position,
    Team,
    ResourceType,
    GameError,
    GameConstants,
)

# Khaos reads a handful of GameConstants fields that describe Cambridge-only
# buildings (breach/bridge/foundry/road/armoured conveyor). Those buildings never
# occur in Titan, so the values are irrelevant, but the *attribute lookups* still
# execute at import time. Provide harmless placeholders so import succeeds; the
# code paths that read them operate on always-empty masks and never fire.
for _name, _val in (
    ("BREACH_MAX_HP", 30),
    ("BREACH_DAMAGE", 0),
    ("BREACH_ATTACK_RADIUS_SQ", 0),
    ("ARMOURED_CONVEYOR_MAX_HP", 20),
    ("BRIDGE_MAX_HP", 20),
    ("FOUNDRY_MAX_HP", 30),
    ("ROAD_MAX_HP", 20),
):
    if not hasattr(GameConstants, _name):
        setattr(GameConstants, _name, _val)


_UNAFFORDABLE = 1 << 30  # cost stub for removed buildings — never affordable


class _CompatController:
    """Adapts the Titan ``Controller`` to the ``cambc`` surface Khaos expects.

    Two jobs, both centralised here so the 13k lines of bot logic stay untouched:

    1. **Resource tuples.** Cambridge's ``get_global_resources()`` and
       ``get_*_cost()`` returned ``(titanium, axionite)`` / ``(ti_cost, ax_cost)``
       pairs; Titan returns a bare int. We re-wrap them as ``(value, 0)`` so every
       ``[0]`` index and ``ti, ax = ...`` unpack in the bot keeps working.

    2. **Removed mechanics.** Bridge, foundry, road, armoured conveyor, tile
       markers, and axionite conversion are gone. Their ``can_build_*`` return
       False and their cost getters return an unaffordable sentinel, so every
       branch that would build/convert them is dead. ``convert``/``place_marker``
       are silent no-ops.

    Everything else delegates straight to the real controller via ``__getattr__``.
    Khaos hoists hot methods (``get_tile_env`` etc.) to locals, so those bypass
    this wrapper entirely — no per-tile overhead.
    """

    __slots__ = ("_c",)

    def __init__(self, c):
        self._c = c

    def __getattr__(self, name):
        return getattr(self._c, name)

    # --- resource / cost tuple compatibility ---
    def get_global_resources(self):
        return (self._c.get_global_resources(), 0)

    def get_conveyor_cost(self):
        return (self._c.get_conveyor_cost(), 0)

    def get_splitter_cost(self):
        return (self._c.get_splitter_cost(), 0)

    def get_harvester_cost(self):
        return (self._c.get_harvester_cost(), 0)

    def get_barrier_cost(self):
        return (self._c.get_barrier_cost(), 0)

    def get_gunner_cost(self):
        return (self._c.get_gunner_cost(), 0)

    def get_sentinel_cost(self):
        return (self._c.get_sentinel_cost(), 0)

    def get_launcher_cost(self):
        return (self._c.get_launcher_cost(), 0)

    def get_builder_bot_cost(self):
        return (self._c.get_builder_bot_cost(), 0)

    # --- removed buildings: never buildable, always unaffordable ---
    def can_build_road(self, *a):
        return False

    def can_build_bridge(self, *a):
        return False

    def can_build_foundry(self, *a):
        return False

    def can_build_armoured_conveyor(self, *a):
        return False

    def get_road_cost(self):
        return (_UNAFFORDABLE, 0)

    def get_bridge_cost(self):
        return (_UNAFFORDABLE, 0)

    def get_foundry_cost(self):
        return (_UNAFFORDABLE, 0)

    def get_armoured_conveyor_cost(self):
        return (_UNAFFORDABLE, 0)

    # --- removed actions: inert no-ops ---
    def convert(self, *a):
        return None

    def place_marker(self, *a):
        return None

    def can_place_marker(self, *a):
        return False

    def get_marker_value(self, *a):
        return 0

    def get_bridge_target(self, *a):
        return None


def wrap(c):
    """Wrap a raw Titan controller for Khaos (idempotent)."""
    if isinstance(c, _CompatController):
        return c
    return _CompatController(c)
