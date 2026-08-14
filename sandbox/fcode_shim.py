"""A self-contained, API-compatible re-implementation of the pieces of `fcode`
that competitor bots import: the value types, constants, and a placeholder
`Controller`. The engine injects a module built from this as `sys.modules['fcode']`
so unmodified bots (`from fcode import Controller, Direction, ...`) run against
our own engine instead of the compiled one.

Types mirror fcode/_types.py exactly (Direction/Position math, enums, constants).
"""
from __future__ import annotations

import math
from enum import Enum
from typing import TYPE_CHECKING, NamedTuple


class GameError(Exception):
    """Raised when a player issues an invalid action."""


class Team(Enum):
    A = "a"
    B = "b"


class ResourceType(Enum):
    TITANIUM = "titanium"


class EntityType(Enum):
    BUILDER_BOT = "builder_bot"
    CORE = "core"
    GUNNER = "gunner"
    SENTINEL = "sentinel"
    LAUNCHER = "launcher"
    CONVEYOR = "conveyor"
    SPLITTER = "splitter"
    HARVESTER = "harvester"
    BARRIER = "barrier"


class Environment(Enum):
    EMPTY = "empty"
    WALL = "wall"
    ORE_TITANIUM = "ore_titanium"


class GameConstants:
    MAX_TURNS = 1000
    STACK_SIZE = 10
    STARTING_TITANIUM = 500
    MAX_TEAM_UNITS = 50
    PASSIVE_TITANIUM_AMOUNT = 10
    PASSIVE_TITANIUM_INTERVAL = 4

    CORE_SPAWNING_RADIUS_SQ = 2
    CORE_ACTION_RADIUS_SQ = 8

    CORE_VISION_RADIUS_SQ = 36
    BUILDER_BOT_VISION_RADIUS_SQ = 20
    GUNNER_VISION_RADIUS_SQ = 13
    SENTINEL_VISION_RADIUS_SQ = 32
    LAUNCHER_VISION_RADIUS_SQ = 26

    CONVEYOR_BASE_COST = 3
    SPLITTER_BASE_COST = 6
    HARVESTER_BASE_COST = 20
    BARRIER_BASE_COST = 3
    GUNNER_BASE_COST = 20
    SENTINEL_BASE_COST = 30
    LAUNCHER_BASE_COST = 20
    BUILDER_BOT_BASE_COST = 30
    GUNNER_ROTATE_COST = 10
    GUNNER_ROTATE_COOLDOWN = 1

    CONVEYOR_MAX_HP = 20
    SPLITTER_MAX_HP = 20
    HARVESTER_MAX_HP = 30
    BARRIER_MAX_HP = 30

    STORE_SIZE = 16

    BUILDER_BOT_MAX_HP = 40
    CORE_MAX_HP = 500
    GUNNER_MAX_HP = 25
    SENTINEL_MAX_HP = 40
    LAUNCHER_MAX_HP = 30

    BUILDER_BOT_SELF_DESTRUCT_DAMAGE = 0
    BUILDER_BOT_ATTACK_DAMAGE = 2
    BUILDER_BOT_ATTACK_COST = 2
    BUILDER_BOT_HEAL_COST = 1
    HEAL_AMOUNT = 4

    GUNNER_DAMAGE = 7
    GUNNER_FIRE_COOLDOWN = 1
    GUNNER_AMMO_COST = 4

    SENTINEL_DAMAGE = 18
    SENTINEL_FIRE_COOLDOWN = 2
    SENTINEL_AMMO_COST = 10

    LAUNCHER_FIRE_COOLDOWN = 1


_DELTAS = {
    "north": (0, -1), "northeast": (1, -1), "east": (1, 0), "southeast": (1, 1),
    "south": (0, 1), "southwest": (-1, 1), "west": (-1, 0), "northwest": (-1, -1),
    "centre": (0, 0),
}


class Direction(Enum):
    NORTH = "north"
    NORTHEAST = "northeast"
    EAST = "east"
    SOUTHEAST = "southeast"
    SOUTH = "south"
    SOUTHWEST = "southwest"
    WEST = "west"
    NORTHWEST = "northwest"
    CENTRE = "centre"

    def delta(self):
        return _DELTAS[self.value]

    def rotate_left(self):
        order = ["north", "northwest", "west", "southwest", "south", "southeast", "east", "northeast"]
        if self is Direction.CENTRE:
            return Direction.CENTRE
        i = order.index(self.value)
        return Direction(order[(i + 1) % 8])

    def rotate_right(self):
        order = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
        if self is Direction.CENTRE:
            return Direction.CENTRE
        i = order.index(self.value)
        return Direction(order[(i + 1) % 8])

    def opposite(self):
        opp = {"north": "south", "northeast": "southwest", "east": "west",
               "southeast": "northwest", "south": "north", "southwest": "northeast",
               "west": "east", "northwest": "southeast", "centre": "centre"}
        return Direction(opp[self.value])

    def is_cardinal(self):
        return self in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)


class Position(NamedTuple):
    x: int
    y: int

    def add(self, d: Direction) -> "Position":
        dx, dy = d.delta()
        return Position(self.x + dx, self.y + dy)

    def distance_squared(self, other: "Position") -> int:
        dx = self.x - other.x
        dy = self.y - other.y
        return dx * dx + dy * dy

    def direction_to(self, other: "Position") -> Direction:
        dx = other.x - self.x
        dy = other.y - self.y
        if dx == 0 and dy == 0:
            return Direction.CENTRE
        angle = math.atan2(-dy, dx)
        sector = int((angle + 2 * math.pi + math.pi / 8) / (math.pi / 4)) % 8
        return [Direction.EAST, Direction.NORTHEAST, Direction.NORTH, Direction.NORTHWEST,
                Direction.WEST, Direction.SOUTHWEST, Direction.SOUTH, Direction.SOUTHEAST][sector]

    def cardinal_direction_to(self, other: "Position") -> Direction:
        dx = other.x - self.x
        dy = other.y - self.y
        if dx == 0 and dy == 0:
            return Direction.CENTRE
        if abs(dx) >= abs(dy):
            return Direction.EAST if dx > 0 else Direction.WEST
        return Direction.SOUTH if dy > 0 else Direction.NORTH


class Controller:  # placeholder for type hints; engine passes a UnitController
    pass
