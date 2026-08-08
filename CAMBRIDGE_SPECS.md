# Disclaimer

We are competing in Florent Battlecode. The following guide is for *Cambridge Battlecode*, a similar
competition but with its own distinct ruleset. Do NOT use this guide alone as a source of truth;
reference FLORENT_VS_CAMBRIDGE_SPECS_DIFF.md to see how Florent differs from the following guide.

# Constants
Source: https://docs.battlecode.cam/api/constants

All numeric constants available via GameConstants.

Access constants via `GameConstants`:

```python theme={"dark"}
from cambc import GameConstants

max_turns = GameConstants.MAX_TURNS  # 2000
```

## General

| Constant                            | Value | Description                                       |
| ----------------------------------- | ----- | ------------------------------------------------- |
| `MAX_TURNS`                         | 2000  | Maximum number of turns per game                  |
| `MAX_TEAM_UNITS`                    | 50    | Maximum living units per team, including the core |
| `STACK_SIZE`                        | 10    | Resources are moved in stacks of 10               |
| `STARTING_TITANIUM`                 | 500   | Each team's initial titanium                      |
| `STARTING_AXIONITE`                 | 0     | Each team's initial axionite                      |
| `PASSIVE_TITANIUM_AMOUNT`           | 10    | Titanium granted passively each interval          |
| `PASSIVE_TITANIUM_INTERVAL`         | 4     | Rounds between passive titanium grants            |
| `AXIONITE_CONVERSION_TITANIUM_RATE` | 4     | Titanium gained per refined axionite converted    |

## Radii (squared)

| Constant                       | Value | Description                      |
| ------------------------------ | ----- | -------------------------------- |
| `ACTION_RADIUS_SQ`             | 2     | Default action radius for units  |
| `CORE_ACTION_RADIUS_SQ`        | 8     | Core action radius (from centre) |
| `CORE_SPAWNING_RADIUS_SQ`      | 2     | Core spawning radius             |
| `CORE_VISION_RADIUS_SQ`        | 36    | Core vision                      |
| `BUILDER_BOT_VISION_RADIUS_SQ` | 20    | Builder bot vision               |
| `GUNNER_VISION_RADIUS_SQ`      | 13    | Gunner vision                    |
| `SENTINEL_VISION_RADIUS_SQ`    | 32    | Sentinel vision                  |
| `BREACH_VISION_RADIUS_SQ`      | 2     | Breach vision                    |
| `BREACH_ATTACK_RADIUS_SQ`      | 24    | Breach attack cone               |
| `LAUNCHER_VISION_RADIUS_SQ`    | 26    | Launcher vision + throw range    |
| `BRIDGE_TARGET_RADIUS_SQ`      | 9     | Max bridge output distance²      |

## Base costs (titanium, axionite)

| Constant                      | Value    |
| ----------------------------- | -------- |
| `BUILDER_BOT_BASE_COST`       | (30, 0)  |
| `CONVEYOR_BASE_COST`          | (3, 0)   |
| `SPLITTER_BASE_COST`          | (6, 0)   |
| `BRIDGE_BASE_COST`            | (20, 0)  |
| `ARMOURED_CONVEYOR_BASE_COST` | (5, 5)   |
| `HARVESTER_BASE_COST`         | (20, 0)  |
| `ROAD_BASE_COST`              | (1, 0)   |
| `BARRIER_BASE_COST`           | (3, 0)   |
| `FOUNDRY_BASE_COST`           | (40, 0)  |
| `GUNNER_BASE_COST`            | (10, 0)  |
| `SENTINEL_BASE_COST`          | (30, 0)  |
| `BREACH_BASE_COST`            | (15, 10) |
| `LAUNCHER_BASE_COST`          | (20, 0)  |

## Max HP

| Constant                   | Value |
| -------------------------- | ----- |
| `CORE_MAX_HP`              | 500   |
| `BUILDER_BOT_MAX_HP`       | 40    |
| `CONVEYOR_MAX_HP`          | 20    |
| `SPLITTER_MAX_HP`          | 20    |
| `BRIDGE_MAX_HP`            | 20    |
| `ARMOURED_CONVEYOR_MAX_HP` | 50    |
| `HARVESTER_MAX_HP`         | 30    |
| `ROAD_MAX_HP`              | 4     |
| `BARRIER_MAX_HP`           | 30    |
| `FOUNDRY_MAX_HP`           | 50    |
| `MARKER_MAX_HP`            | 1     |
| `GUNNER_MAX_HP`            | 40    |
| `SENTINEL_MAX_HP`          | 30    |
| `BREACH_MAX_HP`            | 60    |
| `LAUNCHER_MAX_HP`          | 30    |

## Combat

| Constant                           | Value   | Description                                     |
| ---------------------------------- | ------- | ----------------------------------------------- |
| `BUILDER_BOT_ATTACK_DAMAGE`        | 2       | Builder bot own-tile attack damage              |
| `BUILDER_BOT_ATTACK_COST`          | (2, 0)  | Cost per builder bot attack                     |
| `BUILDER_BOT_HEAL_COST`            | (1, 0)  | Cost per builder bot heal                       |
| `BUILDER_BOT_SELF_DESTRUCT_DAMAGE` | 0       | Damage on self-destruct                         |
| `HEAL_AMOUNT`                      | 4       | HP restored per heal action                     |
| `GUNNER_DAMAGE`                    | 10      | Gunner base damage per shot                     |
| `GUNNER_AXIONITE_DAMAGE`           | 25      | Gunner damage when loaded with refined axionite |
| `GUNNER_FIRE_COOLDOWN`             | 1       | Turns between gunner shots                      |
| `GUNNER_AMMO_COST`                 | 2       | Resources consumed per shot                     |
| `GUNNER_ROTATE_COST`               | (10, 0) | Cost to rotate a gunner                         |
| `GUNNER_ROTATE_COOLDOWN`           | 1       | Action cooldown applied after rotating          |
| `SENTINEL_DAMAGE`                  | 18      | Sentinel damage per shot                        |
| `SENTINEL_FIRE_COOLDOWN`           | 3       | Turns between sentinel shots                    |
| `SENTINEL_AMMO_COST`               | 10      | Resources consumed per shot                     |
| `SENTINEL_STUN_DURATION`           | 5       | Cooldown added by refined axionite stun         |
| `BREACH_DAMAGE`                    | 40      | Breach direct hit damage                        |
| `BREACH_SPLASH_DAMAGE`             | 20      | Breach splash damage                            |
| `BREACH_FIRE_COOLDOWN`             | 1       | Turns between breach shots                      |
| `BREACH_AMMO_COST`                 | 5       | Refined axionite per shot                       |
| `LAUNCHER_FIRE_COOLDOWN`           | 1       | Turns between launcher throws                   |


# Controller
Source: https://docs.battlecode.cam/api/controller

Complete reference for all Controller methods available to your bot.

The `Controller` object is passed to your `Player.run()` method each round. It provides all queries and actions for interacting with the game.

```python theme={"dark"}
class Player:
    def run(self, c: Controller):
        # c is the Controller for this unit
        pos = c.get_position()
```

## Info methods

### Unit info

<ResponseField name="get_team(id: int | None = None)" type="Team">
  Return the team of the entity with the given id, or this unit if omitted.
</ResponseField>

<ResponseField name="get_position(id: int | None = None)" type="Position">
  Return the position of the entity with the given id, or this unit if omitted.
</ResponseField>

<ResponseField name="get_id()" type="int">
  Return this unit's entity id.
</ResponseField>

<ResponseField name="get_action_cooldown()" type="int">
  Return this unit's current action cooldown. Actions require cooldown == 0.
</ResponseField>

<ResponseField name="get_move_cooldown()" type="int">
  Return this unit's current move cooldown. Movement requires cooldown == 0.
</ResponseField>

<ResponseField name="get_hp(id: int | None = None)" type="int">
  Return the current HP of the entity with the given id, or this unit if
  omitted.
</ResponseField>

<ResponseField name="get_max_hp(id: int | None = None)" type="int">
  Return the max HP of the entity with the given id, or this unit if omitted.
</ResponseField>

<ResponseField name="get_entity_type(id: int | None = None)" type="EntityType">
  Return the EntityType of the entity with the given id, or this unit if
  omitted.
</ResponseField>

<ResponseField name="get_direction(id: int | None = None)" type="Direction">
  Return the facing direction of a conveyor, splitter, armoured conveyor, or
  turret. Raises `GameError` if the entity has no direction.
</ResponseField>

<ResponseField name="get_vision_radius_sq(id: int | None = None)" type="int">
  Return the vision radius squared of the given unit, or this unit if omitted.
</ResponseField>

### Turret info

<ResponseField name="get_ammo_amount()" type="int">
  Return the amount of ammo this turret currently holds.
</ResponseField>

<ResponseField name="get_ammo_type()" type="ResourceType | None">
  Return the resource type loaded as ammo, or None if empty.
</ResponseField>

<ResponseField name="get_gunner_target()" type="Position | None">
  Return the closest targetable occupied tile on the gunner's forward line, or
  None if none exists. Empty tiles are skipped. Markers are targetable but do
  not block farther legal targets. Walls block the line but are not targetable.
  Builder bots and non-marker buildings are both targetable and blocking, so if
  either appears first it is returned and nothing beyond it is legal. Only valid
  on gunners.
</ResponseField>

<ResponseField name="get_attackable_tiles()" type="list[Position]">
  Return this turret's raw geometric attack pattern as `list[Position]`. This
  ignores ammo, cooldown, occupancy, blockers, and any other legality checks.
  For gunners, this is the full forward ray up to range, including tiles behind
  walls. For sentinels, this is the full +/-1 band around the forward line
  within vision radius squared 32. For breaches, this is the forward 180-degree
  cone within distance squared 24. For launchers, this is every in-bounds tile
  with `0 < distance^2 <= 26`. Raises `GameError` if this unit is not a turret.
</ResponseField>

<ResponseField name="get_attackable_tiles_from(position: Position, direction: Direction, turret_type: EntityType)" type="list[Position]">
  Return the same raw geometric attack pattern for a hypothetical turret at
  position. This can be called from any controller and does not check whether a
  turret exists there, whether it could legally be built there, or whether the
  tile is occupied. If position is out of bounds, returns `[]`. In Python,
  turret\_type must be one of `EntityType.GUNNER`, `EntityType.SENTINEL`,
  `EntityType.BREACH`, or `EntityType.LAUNCHER`; any other `EntityType` raises
  `ValueError`. direction is ignored for launchers.
</ResponseField>

### Building info

<ResponseField name="get_bridge_target(id: int)" type="Position">
  Return the output target position of a bridge. Raises `GameError` if not a
  bridge.
</ResponseField>

<ResponseField name="get_stored_resource(id: int | None = None)" type="ResourceType | None">
  Return the resource stored in a conveyor/splitter/armoured
  conveyor/bridge/foundry, or None if empty. Raises `GameError` if the entity
  has no storage.
</ResponseField>

<ResponseField name="get_stored_resource_id(id: int | None = None)" type="int | None">
  Return the id of the resource stack stored in a conveyor/splitter/armoured
  conveyor/bridge/foundry, or None if empty. Raises `GameError` if the entity
  has no storage.
</ResponseField>

### Tile queries

<ResponseField name="get_tile_env(pos: Position)" type="Environment">
  Return the environment type (empty, wall, ore) of the tile at pos.
</ResponseField>

<ResponseField name="get_tile_building_id(pos: Position)" type="int | None">
  Return the id of the building on the tile at pos, or None if empty.
</ResponseField>

<ResponseField name="get_tile_builder_bot_id(pos: Position)" type="int | None">
  Return the id of the builder bot on the tile at pos, or None if empty.
</ResponseField>

<ResponseField name="is_tile_empty(pos: Position)" type="bool">
  Return True if the tile has no building and is not a wall.
</ResponseField>

<ResponseField name="is_tile_passable(pos: Position)" type="bool">
  Return True if a builder bot belonging to this team could stand on the tile.
  A tile is passable when it contains a walkable building (conveyor, splitter,
  armoured conveyor, bridge, or road) or an allied core, and no builder bot is
  already present.
</ResponseField>

<ResponseField name="is_in_vision(pos: Position)" type="bool">
  Return True if pos is within this unit's vision radius.
</ResponseField>

### Nearby queries

<ResponseField name="get_nearby_tiles(dist_sq: int | None = None)" type="list[Position]">
  Return all in-bounds tile positions within dist\_sq of this unit (defaults to
  vision radius). dist\_sq must not exceed the vision radius.
</ResponseField>

<ResponseField name="get_nearby_entities(dist_sq: int | None = None)" type="list[int]">
  Return ids of all entities on tiles within dist\_sq (defaults to vision
  radius).
</ResponseField>

<ResponseField name="get_nearby_buildings(dist_sq: int | None = None)" type="list[int]">
  Return ids of all buildings within dist\_sq (defaults to vision radius).
</ResponseField>

<ResponseField name="get_nearby_units(dist_sq: int | None = None)" type="list[int]">
  Return ids of all units within dist\_sq (defaults to vision radius).
</ResponseField>

### Map and game state

<ResponseField name="get_map_width()" type="int">
  Return the width of the map in tiles.
</ResponseField>

<ResponseField name="get_map_height()" type="int">
  Return the height of the map in tiles.
</ResponseField>

<ResponseField name="get_current_round()" type="int">
  Return the current round number (starts at 1).
</ResponseField>

<ResponseField name="get_global_resources()" type="tuple[int, int]">
  Return (titanium, axionite) in this team's global resource pool.
</ResponseField>

<ResponseField name="get_scale_percent()" type="float">
  Return this team's current cost scale as a percentage (100.0 = base cost).
</ResponseField>

<ResponseField name="get_unit_count()" type="int">
  Return the number of living units currently on your team, including the core.
</ResponseField>

<ResponseField name="get_cpu_time_elapsed()" type="int">
  Return the CPU time elapsed this round in microseconds.
</ResponseField>

## Cost getters

Every buildable entity has a cost getter that returns the current scaled `(titanium, axionite)` cost:

```python theme={"dark"}
c.get_conveyor_cost()           # -> (int, int)
c.get_splitter_cost()
c.get_bridge_cost()
c.get_armoured_conveyor_cost()
c.get_harvester_cost()
c.get_road_cost()
c.get_barrier_cost()
c.get_gunner_cost()
c.get_sentinel_cost()
c.get_breach_cost()
c.get_launcher_cost()
c.get_foundry_cost()
c.get_builder_bot_cost()
```

## Movement

<ResponseField name="move(direction: Direction)" type="None">
  Move this builder bot one step in direction. Raises `GameError` if not legal.
</ResponseField>

<ResponseField name="can_move(direction: Direction)" type="bool">
  Return True if this builder bot can move in direction this round.
</ResponseField>

## Building

Every buildable entity has `can_build_*` and `build_*` methods. All require action cooldown == 0 and sufficient resources. The `can_build_*` variants return `bool`; `build_*` returns the new entity's `int` id or raises `GameError` if not legal.

If a `can_build_*` method would create a living unit, it also accounts for the global unit cap.

If a tile already contains a builder bot, only walkable buildings (conveyors and
roads) may be built on that tile.

### Directional buildings

These take `(position: Position, direction: Direction)` — the direction the building faces:

```python theme={"dark"}
c.build_conveyor(pos, direction)          c.can_build_conveyor(pos, direction)
c.build_splitter(pos, direction)          c.can_build_splitter(pos, direction)
c.build_armoured_conveyor(pos, direction) c.can_build_armoured_conveyor(pos, direction)
c.build_gunner(pos, direction)            c.can_build_gunner(pos, direction)
c.build_sentinel(pos, direction)          c.can_build_sentinel(pos, direction)
c.build_breach(pos, direction)            c.can_build_breach(pos, direction)
```

### Bridge

Takes `(position: Position, target: Position)` — the bridge's output target tile (within distance² 9):

```python theme={"dark"}
c.build_bridge(pos, target)               c.can_build_bridge(pos, target)
```

### Non-directional buildings

Take only `(position: Position)`:

```python theme={"dark"}
c.build_harvester(pos)                    c.can_build_harvester(pos)
c.build_road(pos)                         c.can_build_road(pos)
c.build_barrier(pos)                      c.can_build_barrier(pos)
c.build_foundry(pos)                      c.can_build_foundry(pos)
c.build_launcher(pos)                     c.can_build_launcher(pos)
```

### Generic build

A single pair of methods that dispatches to the correct type-specific builder. Use these when the entity type is determined at runtime.

<ResponseField name="can_build(entity_type: EntityType, position: Position, extra: Direction | Position | None = None)" type="bool">
  Return True if `entity_type` can be built at `position`. For directional
  buildings and turrets (conveyor, splitter, armoured\_conveyor, gunner,
  sentinel, breach), `extra` must be a `Direction`. For bridges, `extra` must be
  the target `Position`. For all other types (harvester, road, barrier, launcher,
  foundry), `extra` is unused.
</ResponseField>

<ResponseField name="build(entity_type: EntityType, position: Position, extra: Direction | Position | None = None)" type="int">
  Build `entity_type` at `position`. Returns the new entity's id. The `extra`
  parameter follows the same rules as `can_build()`. Raises `GameError` if not
  legal.
</ResponseField>

## Healing & destruction

<ResponseField name="heal(position: Position)" type="None">
  Heal all friendly entities on a tile within this builder bot's action radius
  by 4 HP. If both a friendly builder bot and a friendly building share the
  tile, both are healed. Costs 1 titanium and one action cooldown. Raises
  `GameError` if not legal.
</ResponseField>

<ResponseField name="can_heal(position: Position)" type="bool">
  Return True if this builder bot can heal the tile at position this round.
  Position must be within the builder bot's action radius. Requires action
  cooldown == 0, enough titanium, and at least one damaged friendly entity on
  the tile.
</ResponseField>

<ResponseField name="destroy(building_pos: Position)" type="None">
  Destroy the allied building at building\_pos. Does **not** cost action
  cooldown.
</ResponseField>

<ResponseField name="can_destroy(building_pos: Position)" type="bool">
  Return True if this builder bot can destroy the allied building.
</ResponseField>

<ResponseField name="self_destruct()" type="None">
  Destroy this unit. Builder bots no longer deal damage on self-destruct.
  **Terminates this unit's execution immediately** — no code after
  `self_destruct()` will run.
</ResponseField>

<ResponseField name="resign(message: str | None = None)" type="None">
  Forfeit the game immediately. Destroys this team's core, ending the game as a loss. **Terminates this unit's execution immediately** — no code after `resign()` will run.

  The optional `message` (max 500 characters) is saved to the replay and displayed in match results.
</ResponseField>

## Markers

<ResponseField name="place_marker(position: Position, value: int)" type="None">
  Place a marker with the given u32 value. Does not cost action cooldown. Max
  one per round.
</ResponseField>

<ResponseField name="can_place_marker(position: Position)" type="bool">
  Return True if this unit can place a marker at position this round.
</ResponseField>

<ResponseField name="get_marker_value(id: int)" type="int">
  Return the u32 value stored in the friendly marker.
</ResponseField>

## Combat

<ResponseField name="fire(target: Position)" type="None">
  Fire this turret at target, or perform the builder bot's own-tile attack.
  Builder bots spend 2 titanium to deal 2 damage to the building on their
  current tile. Gunners use first-obstruction line of sight: empty tiles and
  markers do not block, markers are targetable, walls block but are not
  targetable, and builder bots plus non-marker buildings are both targetable and
  blocking. If a turret attacks a tile containing both a building and a builder
  bot, only the builder bot is hit. Use `launch()` for launchers.
</ResponseField>

<ResponseField name="can_fire(target: Position)" type="bool">
  Return True if this turret can fire at target this round, or if this builder
  bot can use its own-tile attack on target. Gunners use the same
  first-obstruction line of sight as `fire()`: empty tiles and markers do not
  block, markers are targetable, walls block but are not targetable, and builder
  bots plus non-marker buildings are both targetable and blocking.
</ResponseField>

<ResponseField name="can_fire_from(position: Position, direction: Direction, turret_type: EntityType, target: Position)" type="bool">
  Return whether a hypothetical turret at position would have a legal shot at
  target on the current map. This treats position as the turret's tile and uses
  current map occupancy and walls, but ignores ammo, cooldown, whether a real
  turret is present, and whether one could legally be built there. If either
  position or target is out of bounds, returns False. For sentinels and
  breaches, this is only a geometric range/shape check. For launchers, this is
  only the raw throw-range check `0 < distance^2 <= 26`; it does not check
  pickup adjacency, whether a builder bot exists, or whether the destination is
  bot-passable. direction is ignored for launchers.
</ResponseField>

<ResponseField name="can_rotate(direction: Direction)" type="bool">
  Return whether `rotate(direction)` would be legal this round. This returns
  False if the current unit is not a gunner, if direction is `Direction.CENTRE`,
  if direction is the gunner's current facing direction, if the gunner cannot act
  this turn, or if the team cannot afford the global 10 Ti rotate cost. Unlike
  `rotate()`, this does not raise on non-gunners.
</ResponseField>

<ResponseField name="rotate(direction: Direction)" type="None">
  Rotate this gunner to face `direction`. Costs 10 titanium from the global
  resource pool and applies a 1-turn cooldown. Raises `GameError` if not legal.
  Only valid on gunners.
</ResponseField>

<ResponseField name="launch(bot_pos: Position, target: Position)" type="None">
  Pick up the builder bot at bot\_pos and throw it to target.
</ResponseField>

<ResponseField name="can_launch(bot_pos: Position, target: Position)" type="bool">
  Return True if this launcher can pick up and throw the bot.
</ResponseField>

## Core

<ResponseField name="convert(amount: int)" type="None">
  Convert `amount` refined axionite from this team's global resource pool into
  titanium at a rate of 1 Ax to 4 Ti. Converted axionite is removed from the Ax
  collected stat and added to the Ti collected stat. Raises `GameError` if not
  legal. Only valid on cores.
</ResponseField>

<ResponseField name="spawn_builder(position: Position)" type="int">
  Spawn a builder bot on one of the 9 core tiles. Costs one action cooldown and
  requires room under the global unit cap. Returns the new entity's id.
</ResponseField>

<ResponseField name="can_spawn(position: Position)" type="bool">
  Return True if the core can spawn a builder at position this round, including
  the unit-cap check.
</ResponseField>

## Debug indicators

<ResponseField name="draw_indicator_line(pos_a: Position, pos_b: Position, r: int, g: int, b: int)" type="None">
  Draw a debug line between two positions with RGB colour. Saved to the replay.
</ResponseField>

<ResponseField name="draw_indicator_dot(pos: Position, r: int, g: int, b: int)" type="None">
  Draw a debug dot at a position with RGB colour. Saved to the replay.
</ResponseField>


# Types & Enums
Source: https://docs.battlecode.cam/api/types

All game types available from `from cambc import *`.

All types are imported from the `cambc` module:

```python theme={"dark"}
from cambc import *
```

This gives you: `Team`, `EntityType`, `ResourceType`, `Environment`, `Direction`, `Position`, [`GameConstants`](/api/constants), `GameError`, and [`Controller`](/api/controller).

## Team

```python theme={"dark"}
class Team(Enum):
    A = "a"
    B = "b"
```

## EntityType

```python theme={"dark"}
class EntityType(Enum):
    BUILDER_BOT = "builder_bot"
    CORE = "core"
    GUNNER = "gunner"
    SENTINEL = "sentinel"
    BREACH = "breach"
    LAUNCHER = "launcher"
    CONVEYOR = "conveyor"
    SPLITTER = "splitter"
    ARMOURED_CONVEYOR = "armoured_conveyor"
    BRIDGE = "bridge"
    HARVESTER = "harvester"
    FOUNDRY = "foundry"
    ROAD = "road"
    BARRIER = "barrier"
    MARKER = "marker"
```

## ResourceType

```python theme={"dark"}
class ResourceType(Enum):
    TITANIUM = "titanium"
    RAW_AXIONITE = "raw_axionite"
    REFINED_AXIONITE = "refined_axionite"
```

## Environment

```python theme={"dark"}
class Environment(Enum):
    EMPTY = "empty"
    WALL = "wall"
    ORE_TITANIUM = "ore_titanium"
    ORE_AXIONITE = "ore_axionite"
```

## Direction

```python theme={"dark"}
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
```

### Direction methods

<ResponseField name="delta()" type="tuple[int, int]">
  Return the `(dx, dy)` step for this direction. North is `(0, -1)`, East is `(1, 0)`, etc.
</ResponseField>

<ResponseField name="rotate_left()" type="Direction">
  Return the direction rotated 45° counterclockwise.
</ResponseField>

<ResponseField name="rotate_right()" type="Direction">
  Return the direction rotated 45° clockwise.
</ResponseField>

<ResponseField name="opposite()" type="Direction">
  Return the opposite direction (180°).
</ResponseField>

## Position

A named tuple with `x` and `y` integer fields.

```python theme={"dark"}
class Position(NamedTuple):
    x: int
    y: int
```

### Position methods

<ResponseField name="add(direction)" type="Position">
  Return a new position offset by the direction delta.
</ResponseField>

<ResponseField name="distance_squared(other)" type="int">
  Return the squared Euclidean distance to another position.
</ResponseField>

<ResponseField name="direction_to(other)" type="Direction">
  Return the closest 45° Direction approximation toward other.
</ResponseField>

### Usage

```python theme={"dark"}
pos = Position(5, 10)
new_pos = pos.add(Direction.NORTH)      # Position(5, 9)
dist = pos.distance_squared(new_pos)    # 1
dir = pos.direction_to(Position(8, 7))  # Direction.NORTHEAST
```

## GameError

```python theme={"dark"}
class GameError(Exception):
    pass
```

Raised when a player issues an invalid action (e.g., building on an occupied tile, moving with cooldown > 0).


# CLI reference
Source: https://docs.battlecode.cam/getting-started/cli

Every command available in the cambc CLI.

The `cambc` CLI is your main tool for local development, testing, and interacting with the platform. Install it with `pip install cambc`.

## Project setup

### `cambc starter`

Scaffold a new Cambridge Battlecode project. Run this first after installing.

```bash theme={"dark"}
cambc starter
```

Creates the following project structure:

```
your-project/
├── cambc.toml          # Project configuration
├── .gitignore          # Ignores replays, __pycache__, venvs
├── bots/
│   └── starter/
│       └── main.py     # Starter bot (optional, prompted)
└── maps/               # Custom maps (optional, prompted)
```

The starter bot demonstrates core gameplay: the core spawns builder bots, builders explore by laying roads, and when they find ore they build harvesters on it.

### `cambc.toml`

The config file created by `cambc starter`. All fields have defaults and all CLI options override config values.

```toml theme={"dark"}
bots_dir = "bots"           # Where to find bots
maps_dir = "maps"           # Where to find maps
replay = "replay.replay26"  # Default replay output path
seed = 1                    # Default map seed
```

Bot paths in `cambc run` are resolved by first checking the raw path, then checking inside `bots_dir`. So `cambc run starter opponent` resolves to `bots/starter` and `bots/opponent`.

## Local development

### `cambc run`

Run a local match between two bots. No time limits are enforced locally.

```bash theme={"dark"}
cambc run <bot_a> <bot_b> [map]
```

**Arguments:**

| Argument | Description                                                                         |
| -------- | ----------------------------------------------------------------------------------- |
| `bot_a`  | First bot — a directory containing `main.py`, a `.py` file, or a name in `bots_dir` |
| `bot_b`  | Second bot — same formats as `bot_a`                                                |
| `map`    | Optional `.map26` map file. If omitted, uses the first map in `maps_dir`            |

**Options:**

| Option          | Description                                                        |
| --------------- | ------------------------------------------------------------------ |
| `--replay PATH` | Output replay file path (overrides `cambc.toml` default)           |
| `--seed N`      | Map seed (overrides `cambc.toml` default)                          |
| `--watch`       | Open the visualiser automatically after the match                  |
| `--tle N`       | Turn time limit in milliseconds (0 to disable, server uses 2)      |
| `--map-random`  | Pick a random map from the maps directory instead of the first one |

```bash theme={"dark"}
cambc run starter starter                           # bot vs itself
cambc run my_bot opponent --seed 42                 # fixed seed
cambc run my_bot opponent maps/custom.map26         # custom map
cambc run --watch my_bot opponent                   # run + auto-open visualiser
cambc run my_bot opponent --replay out.replay26     # custom replay path
```

After the match completes, `cambc run` prints a summary showing the winner, win condition, turn count, and a resource breakdown (titanium, axionite, units, and buildings) for each team.

### `cambc watch`

View a replay in the browser-based visualiser.

```bash theme={"dark"}
cambc watch [replay_file]
cambc watch --match <match_id> [--game <n>]
```

**Local replay:** Serves the visualiser on `localhost` and opens your browser. Press `Ctrl+C` to stop the server.

```bash theme={"dark"}
cambc watch replay.replay26
```

**Platform match:** Opens the platform visualiser in your browser for a specific match.

```bash theme={"dark"}
cambc watch --match abc123          # opens match on platform
cambc watch --match abc123 --game 3 # specific game within the match
```

### `cambc map-editor`

Open the map editor to create custom `.map26` files.

```bash theme={"dark"}
cambc map-editor              # local map editor
cambc map-editor --platform   # open map editor on the platform
```

## Platform commands

These commands interact with the online platform at [game.battlecode.cam](https://game.battlecode.cam). Most require authentication via `cambc login`.

### `cambc login`

Authenticate with the platform. Opens a browser window for OAuth login and stores your session locally.

```bash theme={"dark"}
cambc login
```

The session persists across CLI invocations until it expires or you run `cambc logout`.

### `cambc logout`

Clear stored credentials.

```bash theme={"dark"}
cambc logout
```

### `cambc submit`

Upload a bot to compete on the ladder.

```bash theme={"dark"}
cambc submit <path>
```

The path can be a directory containing `main.py`, a single `.py` file, or a `.zip`. Directories are auto-zipped before upload. See [submission requirements](/getting-started/submitting#bot-requirements) for constraints.

```bash theme={"dark"}
cambc submit ./my_bot/       # directory (auto-zipped)
cambc submit my_bot.py       # single file
cambc submit my_bot.zip      # pre-zipped
```

### `cambc status`

Show your current team, rating, rank, and member list.

```bash theme={"dark"}
cambc status
```

Displays your username, team name, category, Elo rating, matches played, and team members with roles.

### `cambc match`

The `cambc match` group provides all match-related commands. When called with just a match ID, it defaults to showing match details.

#### `cambc match info`

View details of a specific match including per-game results.

```bash theme={"dark"}
cambc match info <match_id>
cambc match <match_id>          # shorthand — defaults to info
```

Shows match status, teams, score, rating delta, timestamps, and a table of individual games with map, winner, win condition, and turns played.

#### `cambc match list`

View recent match history.

```bash theme={"dark"}
cambc match list [options]
```

| Option                     | Description                                     |
| -------------------------- | ----------------------------------------------- |
| `--type {ladder\|unrated}` | Filter by match type                            |
| `--team NAME`              | Filter by team name or ID                       |
| `--mine`                   | Show only your team's matches                   |
| `--limit N`                | Number of matches to show (default 20, max 100) |
| `--cursor CURSOR`          | Pagination cursor from previous results         |

#### `cambc match unrated`

Challenge another team to an unrated match.

```bash theme={"dark"}
cambc match unrated <opponent_team_id>
cambc match unrated <opponent_team_id> --match <source_match_id>
cambc match unrated <opponent_team_id> --map arena --map galaxy
```

| Option       | Description                                                                              |
| ------------ | ---------------------------------------------------------------------------------------- |
| `--match ID` | Use the opponent's submission version from a specific past match instead of their latest |
| `--map NAME` | Map name (repeatable, up to 5). If omitted, 5 random maps are used                       |

Unrated matches run on the same AWS infrastructure as ladder matches with full time limit enforcement but do not affect ratings. They are prioritised over ladder matches for faster results.

<Warning>
  Rate limits apply: max 10 test/unrated matches per 10 minutes.
</Warning>

#### `cambc match test`

Upload two local bots and run a remote match with full time limit enforcement on AWS Graviton3 hardware.

```bash theme={"dark"}
cambc match test <bot_a> <bot_b> [maps...]
```

Both bots are packaged and uploaded to the server. Unlike `cambc run`, this enforces the 2ms CPU time limit per round — use this to check your bot's performance before submitting. You can optionally specify `.map26` files — one per game.

```bash theme={"dark"}
cambc match test my_bot opponent                           # test two bots remotely
cambc match test my_bot opponent maps/arena.map26          # with a specific map
```

#### `cambc match replay`

Download replay files for a completed match.

```bash theme={"dark"}
cambc match replay <match_id>              # download all 5 game replays
cambc match replay <match_id> --game 3     # download a specific game
cambc match replay <match_id> -o out.replay26  # custom output path
```

#### `cambc match watch`

Open a match replay in the browser.

```bash theme={"dark"}
cambc match watch <match_id>
cambc match watch <match_id> --game 3
```

#### `cambc match tests`

View your team's remote test run history.

```bash theme={"dark"}
cambc match tests [--limit N]
```

### `cambc team`

Search for teams or view team details.

```bash theme={"dark"}
cambc team search <query>     # search by name
cambc team info <team_id>     # view team details
```


# Your First Bot
Source: https://docs.battlecode.cam/getting-started/first-bot

Write a basic bot that spawns builder bots and starts harvesting resources.

## Get started with `cambc starter`

If you haven't already, run `cambc starter` to scaffold your project. When prompted, choose to create the starter bot — it gives you a working bot to build on.

```bash theme={"dark"}
cambc starter
```

The starter bot demonstrates core mechanics: the core spawns builder bots, builders explore by laying roads, and when they find ore they build harvesters on it. Run it against itself to see it in action:

```bash theme={"dark"}
cambc run starter starter --watch
```

<Info>
  Teams can have at most **50 living units total**, including the core. In larger bots, use `c.get_unit_count()` with `GameConstants.MAX_TEAM_UNITS` if you want the exact numbers. `c.can_spawn()` and any unit-producing `c.can_build_*()` method already account for the cap.
</Info>

## Bot structure

Every bot is a Python file containing a `Player` class with a `run` method. The engine creates one `Player` instance per unit and calls `run(controller)` once per round.

```python main.py theme={"dark"}
"""Starter bot - a simple example to demonstrate usage of the Controller API.

Each unit gets its own Player instance; the engine calls run() once per round.
Use Controller.get_entity_type() to branch on what kind of unit you are.

This bot:
  - Core: spawns up to 3 builder bots on random adjacent tiles
  - Builder bot: builds a harvester on any adjacent ore tile, then moves in a
    random direction (laying a road first so the tile is passable), and places
    a marker recording the current round number
"""

import random

from cambc import Controller, Direction, EntityType, Environment, Position

# non-centre directions
DIRECTIONS = [d for d in Direction if d != Direction.CENTRE]

class Player:
    def __init__(self):
        self.num_spawned = 0 # number of builder bots spawned so far (core)

    def run(self, ct: Controller) -> None:
        etype = ct.get_entity_type()
        if etype == EntityType.CORE:
            if self.num_spawned < 3:
                # if we haven't spawned 3 builder bots yet, try to spawn one on a random tile
                spawn_pos = ct.get_position().add(random.choice(DIRECTIONS))
                if ct.can_spawn(spawn_pos):
                    ct.spawn_builder(spawn_pos)
                    self.num_spawned += 1
        elif etype == EntityType.BUILDER_BOT:
            # if we are adjacent to an ore tile, build a harvester on it
            for d in Direction:
                check_pos = ct.get_position().add(d)
                if ct.can_build_harvester(check_pos):
                    ct.build_harvester(check_pos)
                    break

            # move in a random direction
            move_dir = random.choice(DIRECTIONS)
            move_pos = ct.get_position().add(move_dir)
            # we need to place a conveyor or road to stand on, before we can move onto a tile
            if ct.can_build_road(move_pos):
                ct.build_road(move_pos)
            if ct.can_move(move_dir):
                ct.move(move_dir)

            # place a marker on an adjacent tile with the current round number
            marker_pos = ct.get_position().add(random.choice(DIRECTIONS))
            if ct.can_place_marker(marker_pos):
                ct.place_marker(marker_pos, ct.get_current_round())
```

## Key concepts

<AccordionGroup>
  <Accordion title="One Player instance per unit">
    Each unit (core, builder bot, turret) gets its own `Player` instance. Instance variables persist across rounds for that unit, but are **not shared** between units. Use [markers](/spec/other-buildings#marker) for inter-unit communication.
  </Accordion>

  <Accordion title="The Controller object">
    The `controller` argument passed to `run()` provides all game queries and actions. See the full [Controller API reference](/api/controller).
  </Accordion>

  <Accordion title="Imports from cambc">
    `from cambc import *` gives you all game types: `Team`, `EntityType`, `Direction`, `Position`, `ResourceType`, `Environment`, `GameConstants`, `GameError`, and `Controller`.
  </Accordion>

  <Accordion title="Time limit">
    Each unit gets **2ms of CPU time** per round, plus a 5% buffer that refills when you use less. Locally there are no time limits — use remote test runs to check performance on the actual hardware.
  </Accordion>

  <Accordion title="No external packages">
    Only Python standard library modules are available. External packages like `numpy` or `scipy` cannot be imported — bots run in a sandboxed environment with no `pip install`.
  </Accordion>
</AccordionGroup>

## Next steps

<CardGroup>
  <Card title="Run a local match" icon="play" href="/getting-started/running-matches">
    Test your bot against itself or an example opponent.
  </Card>

  <Card title="Game rules" icon="book" href="/spec/overview">
    Understand the full game mechanics before optimising.
  </Card>

  <Card title="API reference" icon="rectangle-terminal" href="/api/controller">
    Every method available via the Controller object.
  </Card>

  <Card title="Types and enums" icon="list" href="/api/types">
    All game types: Team, EntityType, Direction, Position, and more.
  </Card>
</CardGroup>


# Installation
Source: https://docs.battlecode.cam/getting-started/installation

Install the Cambridge Battlecode CLI and run your first local match.

## Requirements

* **Python 3.12 or 3.13** (3.14 is not supported)
* **pip** (comes with Python)

## Install

```bash theme={"dark"}
pip install cambc
```

This gives you both the CLI tool and the compiled Rust game engine as a native Python module. No Docker, Rust toolchain, or Node.js required.

<Info>
  The `cambc` package includes the full game engine compiled for your platform. Supported: macOS (Apple Silicon + Intel), Linux (x86\_64 + ARM64), Windows (x86\_64).
</Info>

## Verify

```bash theme={"dark"}
cambc --version
```

## Set up your project

```bash theme={"dark"}
cambc starter
```

This scaffolds a project with a `cambc.toml` config, `bots/` and `maps/` directories, a `.gitignore`, and optionally a starter bot. See the [CLI reference](/getting-started/cli#cambc-starter) for details.

## What's included

| Component             | Description                                                                               |
| --------------------- | ----------------------------------------------------------------------------------------- |
| `cambc` CLI           | Run local matches, open the visualiser, submit bots, trigger test runs                    |
| `cambc` Python module | Game types (`Team`, `EntityType`, `Direction`, `Position`, etc.) for use in your bot code |
| `titan_runner`        | The compiled Rust game engine (embedded, runs locally with no time limits)                |

## Next steps

<CardGroup>
  <Card title="Write your first bot" icon="arrow-right" href="/getting-started/first-bot">
    Create a simple bot that spawns builders and starts harvesting.
  </Card>

  <Card title="CLI reference" icon="terminal" href="/getting-started/cli">
    Full reference for all CLI commands.
  </Card>
</CardGroup>


# How Matches Work
Source: https://docs.battlecode.cam/getting-started/matches

How ladder matches, Elo ratings, and the 5-game format work.

## Match format

Every match consists of **5 games**. Each game is played on a different map with a different random seed. The team that wins more games wins the match.

### Win conditions per game

A game ends when:

| Condition              | Description                                                                                              |
| ---------------------- | -------------------------------------------------------------------------------------------------------- |
| **Core destroyed**     | One team's core reaches 0 HP                                                                             |
| **Resigned**           | A team called [`resign()`](/api/controller#healing--destruction) — their core is destroyed and they lose |
| **Axionite collected** | Tiebreaker 1: most total refined axionite delivered to the core                                          |
| **Titanium collected** | Tiebreaker 2: most total titanium delivered to the core                                                  |
| **Harvesters**         | Tiebreaker 3: most harvesters currently alive                                                            |
| **Axionite stored**    | Tiebreaker 4: most axionite currently stored                                                             |
| **Titanium stored**    | Tiebreaker 5: most titanium currently stored                                                             |
| **Coinflip**           | Tiebreaker 6: random coinflip if all above are tied                                                      |

If both cores survive 2000 rounds, the [tiebreaker sequence](/spec/overview#win-conditions) is evaluated in order. The match result shows exactly which condition decided the game.

## Ladder

The [ladder](https://game.battlecode.cam/ladder) ranks all teams by **Elo** rating. New teams start unrated and are seeded to 1500 when they upload their first ready submission.

### Scheduling

Every **10 minutes**, the scheduler:

1. Pairs each team with one similarly-rated opponent (greedy nearest-rating matching with small random jitter to avoid repetitive matchups)
2. Avoids rematches from the last hour
3. Submits matches to the runner infrastructure

With N teams, each cycle creates floor(N/2) matches — one match per team.

### Rating updates

Ratings are updated using **Elo** immediately after each match completes. Match outcomes use fractional scoring based on the game score (e.g., a 5-0 win counts more than a 3-2 win).

Each team has a single Elo rating that moves up or down after each match.

## Unrated matches

You can challenge any team to an unrated match that doesn't affect ratings:

```bash theme={"dark"}
cambc match unrated <opponent_team_id>
cambc match unrated <opponent_team_id> --map arena --map galaxy   # specific maps
```

Unrated matches use the same infrastructure and time limits as ladder matches but are prioritised for faster execution. You can optionally specify up to 5 maps — if omitted, 5 random maps are used.

## Test runs

Test runs let you upload two local bots and run them against each other on the same hardware as ladder matches:

```bash theme={"dark"}
cambc match test my_bot opponent
```

This is the best way to verify your bot works within the 2ms CPU time limit before submitting to the ladder.

<Warning>
  Rate limits: max 10 test/unrated matches per 10 minutes.
</Warning>


# Running Matches
Source: https://docs.battlecode.cam/getting-started/running-matches

Run local matches with the CLI and view replays in the visualiser.

## Run a local match

```bash theme={"dark"}
cambc run <bot_a> <bot_b> [map]
```

This runs the full game engine locally with **no time limits** — ideal for rapid iteration. The engine outputs a `replay.replay26` file.

Bot paths can be a directory containing `main.py`, a `.py` file, or a bot name from your `bots_dir` (set in `cambc.toml`). The optional map argument is a `.map26` file — if omitted, the first map in your `maps_dir` is used.

```bash theme={"dark"}
cambc run starter starter                        # bot vs itself
cambc run my_bot opponent --seed 42              # deterministic seed
cambc run my_bot opponent maps/custom.map26      # custom map
cambc run my_bot opponent --replay out.replay26  # custom replay path
```

## View a replay

```bash theme={"dark"}
cambc watch replay.replay26
```

Opens the visualiser in your browser. Supports play/pause, round scrubbing, speed control, and keyboard navigation.

### Run + watch in one command

```bash theme={"dark"}
cambc run --watch starter starter
```

### View a platform match

```bash theme={"dark"}
cambc watch --match <match_id>
cambc watch --match <match_id> --game 3
```

## Remote test runs

Remote commands require authentication — run `cambc login` first if you haven't already.

Test your bots on the **same hardware** that runs ladder matches, with full time limit enforcement:

```bash theme={"dark"}
cambc match test <bot_a> <bot_b> [maps...]
```

This uploads both bots and runs a match on AWS Graviton3 instances with the 2ms CPU time limit enforced. Use this to catch performance issues before submitting.

Bot paths for `match test` must be a directory containing `main.py` or a `.zip` file (unlike `cambc run`, arbitrary `.py` files are not accepted).

```bash theme={"dark"}
cambc match test my_bot opponent
cambc match test my_bot opponent maps/custom.map26
```

<Warning>
  Remote test runs are rate-limited: max 10 test/unrated matches per 10 minutes.
</Warning>

You can also challenge another team to an unrated match using both teams' latest submissions:

```bash theme={"dark"}
cambc match unrated <opponent_team_id>
cambc match unrated <opponent_team_id> --map arena --map galaxy   # specific maps
```

## Debugging

* **stdout** via `print("msg")` is captured and saved to the replay — view it per-unit in the visualiser
* **stderr** prints to your console in real time
* Use `c.draw_indicator_line()` and `c.draw_indicator_dot()` to draw debug overlays on the map

## Next steps

<CardGroup>
  <Card title="Submit your bot" icon="cloud-arrow-up" href="/getting-started/submitting">
    Upload your bot to compete on the ladder.
  </Card>

  <Card title="CLI reference" icon="terminal" href="/getting-started/cli">
    Full reference for all CLI commands.
  </Card>
</CardGroup>


# Submitting
Source: https://docs.battlecode.cam/getting-started/submitting

Upload your bot to the platform to compete on the ladder.

## Log in

Before submitting, authenticate the CLI with your platform account:

```bash theme={"dark"}
cambc login
```

This opens a browser window to complete the OAuth flow. Your session persists until it expires or you run `cambc logout`.

## Via CLI

```bash theme={"dark"}
cambc submit ./my_bot/
```

If you pass a directory, the CLI auto-zips it. You can also submit a zip file directly.

## Via the platform

1. Go to [game.battlecode.cam](https://game.battlecode.cam)
2. Navigate to **Submissions** in the sidebar
3. Upload your bot zip

## Bot requirements

Your submission must contain a `main.py` file with a `Player` class. The file can be at the root of the zip or inside a single top-level directory.

| Constraint        | Limit                                                  |
| ----------------- | ------------------------------------------------------ |
| Zip size          | 5 MB max                                               |
| Decompressed size | 50 MB max                                              |
| File count        | 500 files max                                          |
| Native extensions | **Not allowed** (`.so`, `.pyd`, `.dylib`, `.dll`)      |
| Imports           | Must be top-level (file I/O is blocked during `run()`) |

<Warning>
  All imports must happen at the top of your file, not inside `run()`. The sandbox blocks file reads during gameplay for security.
</Warning>

## What happens after upload

1. Your zip is validated (structure, size, no native extensions)
2. Status is set to **ready**
3. Your latest ready submission becomes your active bot on the ladder (you can also manually select which submission is active from the Submissions page)
4. The scheduler pairs you against other teams every 10 minutes

## Ladder

The [ladder](https://game.battlecode.cam/ladder) ranks all teams by Elo rating. Every 10 minutes, the scheduler creates one match per team, pairing you with a nearby-rated opponent using greedy nearest-rating matching with small random jitter. Each match consists of **5 games** — the team that wins more games wins the match. Ratings update immediately after each match. See [How matches work](/getting-started/matches) for details.

<Tip>
  Use `cambc match test` to test your bot with full time limits on the same hardware as the ladder before submitting. Use `cambc match unrated` to challenge specific teams without affecting your rating.
</Tip>


# Cambridge Battlecode
Source: https://docs.battlecode.cam/index

Documentation for the Cambridge Battlecode programming competition.

<img alt="Cambridge Battlecode" />

<img alt="Cambridge Battlecode" />

Cambridge Battlecode is a programming competition where you write Python bots that compete in a turn-based strategy game. Your bots control autonomous mining fleets on Titan — harvesting resources, building infrastructure, and destroying the enemy core.

For competition details, dates, prizes, and eligibility, visit the [main website](https://battlecode.cam).

## Quick start

```bash theme={"dark"}
pip install cambc
cambc starter
cambc run starter starter --watch
```

<CardGroup>
  <Card title="Install and scaffold" icon="terminal" href="/getting-started/installation">
    Install the CLI and run `cambc starter` to set up your project.
  </Card>

  <Card title="Write your first bot" icon="code" href="/getting-started/first-bot">
    Learn the basics: spawning builder bots, placing conveyors, and harvesting resources.
  </Card>

  <Card title="Game rules" icon="book" href="/spec/overview">
    Full game specification — map, resources, units, buildings, turrets, and win conditions.
  </Card>

  <Card title="API reference" icon="rectangle-terminal" href="/api/controller">
    Every method available to your bot via the Controller object.
  </Card>
</CardGroup>


# Builder Bot
Source: https://docs.battlecode.cam/spec/builder-bot

The only mobile unit — responsible for constructing all buildings.

<img alt="Builder bot" />

Builder bots are the **only mobile unit**. They construct buildings, heal friendly entities on a chosen tile, and can make a weak attack against the building under them.

## Properties

| Property             | Value |
| -------------------- | ----- |
| HP                   | 40    |
| Base cost            | 30 Ti |
| Scaling contribution | 20%   |
| Vision radius²       | 20    |
| Action radius²       | 2     |

<img alt="Builder bot range — blue is vision, red is action radius" />

## Movement

Builder bots can move to an adjacent tile (including diagonals) if their move cooldown is 0. Moving increases the cooldown by 1.

<Warning>
  Builder bots can **only walk on**:

  * Conveyors, splitters, armoured conveyors, and bridges (any direction, either team)
  * Roads (either team)
  * The allied core

  These are called **walkable** tiles. The direction of the building does not matter, and neither does the presence of resources on the tile.
</Warning>

```python theme={"dark"}
# Move towards a target
direction = c.get_position().direction_to(target)
if c.can_move(direction):
    c.move(direction)
```

## Actions

When action cooldown is 0, a builder bot can perform one of:

### Build

Build any building or turret on a tile within action radius that doesn't
already have a building.

<Info>
  If a tile already contains a builder bot, only walkable buildings
  (conveyors and roads) can be built on that tile.
</Info>

### Heal

Spend **1 Ti** to heal **4 HP** to all friendly entities on a tile within action radius. If a friendly builder bot and a friendly building share the chosen tile, both are healed. The action fails if nothing on that tile would gain HP.

```python theme={"dark"}
if c.can_heal(target_pos):
    c.heal(target_pos)
```

### Attack

Spend **2 Ti** to deal **2 damage** to the building on the tile the builder bot is standing on. This reuses the standard `can_fire()` / `fire()` combat API. **Armoured conveyors are immune** to builder bot attacks.

```python theme={"dark"}
my_pos = c.get_position()
if c.can_fire(my_pos):
    c.fire(my_pos)
```

### Destroy

Destroy any allied building within action radius. This can be done **any number of times per round** and does **not** cost action cooldown.

```python theme={"dark"}
if c.can_destroy(building_pos):
    c.destroy(building_pos)
```

## Self-destruct

A builder bot can self-destruct at any time. It does **not** deal damage.

```python theme={"dark"}
c.self_destruct()
```

## Markers

Builder bots (like all units) can place one [marker](/spec/other-buildings#marker) per round within action radius, separate from the action cooldown.


# Conveyors
Source: https://docs.battlecode.cam/spec/conveyors

Transport resources across the map — conveyors, splitters, bridges, and armoured conveyors.

All conveyors can hold **one stack** of any resource, and both accept input and produce output. Basic conveyors, splitters, and armoured conveyors point in one of the **cardinal directions**.

You can inspect the stack currently stored in one of these buildings with `c.get_stored_resource(...)` and `c.get_stored_resource_id(...)`.

## Conveyor

<img alt="Conveyor" />

Accepts resources from any of its three non-output directions. Sends its contents in the direction it is pointing if that tile can accept a resource.

| Property  | Value |
| --------- | ----- |
| HP        | 20    |
| Base cost | 3 Ti  |
| Scaling   | 1%    |

```python theme={"dark"}
# Build a conveyor pointing south
c.build_conveyor(pos, Direction.SOUTH)

# Inspect the stack currently on a conveyor
resource_type = c.get_stored_resource(conveyor_id)
resource_id = c.get_stored_resource_id(conveyor_id)
```

## Splitter

<img alt="Splitter" />

Alternates between outputting in three directions: the primary output and the two adjacent directions. **Only accepts input from the back.** Prioritises directions used least recently.

| Property  | Value |
| --------- | ----- |
| HP        | 20    |
| Base cost | 6 Ti  |
| Scaling   | 1%    |

## Bridge

<img alt="Bridge" />

Outputs its contents to a **specific tile within Euclidean distance 3** (distance² ≤ 9), chosen when built. Bridges bypass directional restrictions — they can feed any building that accepts resources. Accepts input from all directions.

| Property  | Value |
| --------- | ----- |
| HP        | 20    |
| Base cost | 20 Ti |
| Scaling   | 10%   |

```python theme={"dark"}
# Build a bridge that outputs to a target position
c.build_bridge(bridge_pos, target_pos)
```

## Armoured Conveyor

<img alt="Armoured conveyor" />

Same function as a basic conveyor but with **much more HP**. Requires refined axionite to build. Armoured conveyors are **immune to builder bot attacks**.

| Property  | Value      |
| --------- | ---------- |
| HP        | 50         |
| Base cost | 5 Ti, 5 Ax |
| Scaling   | 1%         |

## Resource distribution

At the end of each round (after all units have acted), resources are distributed. Buildings that have resources to output attempt to send them to adjacent buildings that can accept them.

Key rules:

* Resources are always moved in **stacks of 10**
* **Resources can be sent to enemy buildings** — be careful with conveyor placement near opponents
* Harvesters and splitters prioritise outputting in directions **used least recently**
* Foundries require one stack each of titanium and raw axionite before outputting one stack of refined axionite
* Turrets only accept resources when completely empty


# Core
Source: https://docs.battlecode.cam/spec/core

Your central building — if it's destroyed, you lose.

<img alt="Core" />

The core is each team's central building. **If your core is destroyed, you lose the game.** Each team starts with one core.

## Properties

| Property       | Value           |
| -------------- | --------------- |
| HP             | 500             |
| Footprint      | 3×3             |
| Vision radius² | 36              |
| Action radius² | 8 (from centre) |

## Spawning

The core can **spawn one builder bot per round** on any of the 9 tiles it occupies. Spawning costs one action cooldown.

<Info>
  Each team can have at most **50 living units total**, including the core. That means you can have at most **49 additional living units**. Use `c.get_unit_count()` together with `GameConstants.MAX_TEAM_UNITS` if you want the exact numbers. `c.can_spawn()` and any unit-producing `c.can_build_*()` method already account for the cap.
</Info>

```python theme={"dark"}
# Spawn a builder on an empty core tile
pos = c.get_position()  # centre of the 3x3 core
for dx in range(-1, 2):
    for dy in range(-1, 2):
        target = Position(pos.x + dx, pos.y + dy)
        if c.can_spawn(target):
            c.spawn_builder(target)
            break
```

<img alt="Core range — blue is vision, red is action radius" />

## Resource delivery

Resources must be transferred to the core via [conveyors](/spec/conveyors) to be added to your team's global resource pool, which is used for building.

Raw axionite delivered to the core is **destroyed**, so refine it first if you want to keep it.

## Conversion

The core can convert refined axionite from the global resource pool into
titanium with `c.convert(amount)`.

$$
1 \text{ Ax} \rightarrow 4 \text{ Ti}
$$

Converted axionite is removed from the Ax collected stat and added to the Ti
collected stat.


# Harvester & Foundry
Source: https://docs.battlecode.cam/spec/harvester-and-foundry

Resource production — mining ore and refining axionite.

## Harvester

<img alt="Harvester" />

Must be placed on an **ore deposit**. Outputs one stack of the corresponding resource to an adjacent building every **4 rounds**. The first output happens immediately on the round the harvester is built.

Prioritises outputting in directions used least recently.

| Property        | Value    |
| --------------- | -------- |
| HP              | 30       |
| Base cost       | 20 Ti    |
| Scaling         | 5%       |
| Output interval | 4 rounds |

```python theme={"dark"}
# Build a harvester on an ore tile
if c.can_build_harvester(ore_pos):
    c.build_harvester(ore_pos)
```

<Tip>
  Harvesters are now cheap enough to get online early. A base harvester costs
  only 20 Ti and adds just +5% scaling.
</Tip>

## Axionite Foundry

<img alt="Axionite foundry" />

Takes one stack each of **titanium and raw axionite**, then outputs one stack of **refined axionite**. Accepts input and produces output from any side.

| Property  | Value |
| --------- | ----- |
| HP        | 50    |
| Base cost | 40 Ti |
| Scaling   | 50%   |

<Warning>
  Foundries have the highest scaling contribution at +50% each. Building one
  adds 50% to your cost multiplier (e.g. 1.0x -> 1.5x if it's your first
  build, but 1.5x -> 2.0x if you've already built other things). Plan
  carefully before committing 40 Ti.
</Warning>

### Refining process

1. Feed titanium (via conveyor) → foundry stores it
2. Feed raw axionite (via conveyor) → foundry combines them
3. Foundry outputs one stack of refined axionite to an adjacent accepting building


# Road, Barrier & Marker
Source: https://docs.battlecode.cam/spec/other-buildings

Utility buildings — walkable paths, defensive walls, and inter-unit communication.

## Road

<img alt="Road" />

Walkable tiles for builder bots to move on. The cheapest building.

| Property  | Value |
| --------- | ----- |
| HP        | 4     |
| Base cost | 1 Ti  |
| Scaling   | 0.5%  |

## Barrier

<img alt="Barrier" />

Cheap, takes up space, and has high HP. Useful for blocking enemy paths or protecting key buildings.

| Property  | Value |
| --------- | ----- |
| HP        | 30    |
| Base cost | 3 Ti  |
| Scaling   | 1%    |

## Marker

<img alt="Marker" />

A tile containing a single **unsigned 32-bit integer** that can be read by any allied unit. Building a marker is completely free and does **not** cost action cooldown — you may place at most one marker per round.

Any team may build over markers, destroying them. Additionally, **all units** (core, builder bots, and turrets) can destroy friendly markers for free using the standard destroy action.

Markers remain targetable by gunners, but unlike walls, builder bots, and
non-marker buildings they do **not** block line of sight or shield occupied
tiles behind them.

| Property | Value |
| -------- | ----- |
| HP       | 1     |
| Cost     | Free  |

<Info>
  Markers are the **only form of communication** between allied units. Global variables are not shared between `Player` instances — each unit has its own isolated Python environment.
</Info>

<Tip>
  All units (core, builder bots, and turrets) can place markers — not just builder bots.
</Tip>

```python theme={"dark"}
# Write a value to a marker
if c.can_place_marker(pos):
    c.place_marker(pos, 42)

# Read a marker
building_id = c.get_tile_building_id(pos)
if building_id is not None:
    if c.get_entity_type(building_id) == EntityType.MARKER:
        value = c.get_marker_value(building_id)
```

### Communication patterns

Since each unit's `Player` instance is isolated, markers are essential for coordination:

* **Scouting reports**: Write enemy positions to markers near your core
* **Build orders**: Use marker values as state machine flags
* **Territory claims**: Mark tiles to avoid duplicate work


# Game Overview
Source: https://docs.battlecode.cam/spec/overview

Objective, map, units, buildings, and win conditions for Cambridge Battlecode.

## Background

The year is 2076. A crystalline ore called **axionite** — a room-temperature superconductor — has been discovered on Titan, Saturn's largest moon. At least six corporations have deployed autonomous extraction fleets to Titan's surface.

Titan is lethal to humans: −179°C, a nitrogen-methane atmosphere, and a 76-minute communication delay to Earth. All operations are carried out by robots.

You write the software that controls your fleet: mining ore, refining axionite, and outcompeting the enemy.

## Objective

Collect resources and **destroy the enemy's core**.

To do this, you must find ore deposits, build harvesters, deliver resources back to the core using conveyors, and expand your territory.

## Win conditions

A game ends immediately if a core is destroyed or a team calls [`resign()`](/api/controller#healing--destruction). Otherwise, if both cores are still alive after **2000 rounds**, the winner is decided by tiebreakers in order:

<Steps>
  <Step title="Axionite delivered">
    Total refined axionite delivered to the core
  </Step>

  <Step title="Titanium delivered">
    Total titanium delivered to the core
  </Step>

  <Step title="Harvesters alive">
    Number of harvesters currently alive
  </Step>

  <Step title="Axionite stored">
    Total axionite currently stored
  </Step>

  <Step title="Titanium stored">
    Total titanium currently stored
  </Step>

  <Step title="Coinflip">
    Random tiebreaker
  </Step>
</Steps>

<Info>
  Axionite converted by the core with `c.convert(...)` is removed from the Ax
  collected stat and added to the Ti collected stat.
</Info>

## Map

The map is a rectangular grid between **20×20** and **50×50** inclusive. The top-left (northwest) corner is position `(0, 0)`.

The map is guaranteed to be **symmetric** by either reflection or rotation.

Each grid cell is one of:

<DenseTable>
  <table>
    <thead><tr><th>Cell type</th><th>Tile</th><th>Description</th></tr></thead>

    <tbody>
      <tr><td>Empty</td><td /><td>Can build anything</td></tr>
      <tr><td>Wall</td><td>      <img alt="" /></td><td>Impassable, cannot build</td></tr>
      <tr><td>Titanium ore</td><td>      <img alt="" /></td><td>Harvesters mine titanium</td></tr>
      <tr><td>Axionite ore</td><td>      <img alt="" /></td><td>Harvesters mine raw axionite</td></tr>
    </tbody>
  </table>
</DenseTable>

Walls prevent building anything on the tile they occupy. Ores are tiles on which harvesters may be built to mine resources.

## Units

Units are game entities which run an **independent instance** of the code that you submit. The [core](/spec/core), [builder bots](/spec/builder-bot), and [turrets](/spec/turrets) are units. **Harvesters are not units** — they operate automatically.

Each round, units take their turns **in the order they were spawned**. After all units have taken their turn, [resources are distributed](/spec/conveyors#resource-distribution). See the [reference tables](/spec/reference) for a quick comparison of all entity stats.

Each team can have at most **50 living units total**, including the core. In practice, that means a team can have at most **49 additional living units** at once. You can inspect the current count with `c.get_unit_count()`, and the cap is exposed as `GameConstants.MAX_TEAM_UNITS`.

### Vision and action radius

Units have a **vision radius** and an **action radius**.

* The **vision radius** is the area in which the unit can sense its environment.
* The **action radius** is the area in which the unit can perform actions, such as building, placing markers, or destroying buildings.

All units have an action radius of √2 (r² = 2), except the core, which has an action radius of √8 (r² = 8) measured from its centre.

Turrets also have an **attack range** which is different from their action radius — see each turret's page for details.

<DenseTable>
  <table>
    <thead><tr><th>Unit</th><th>Vision r²</th><th>Action r²</th></tr></thead>

    <tbody>
      <tr><td>Core</td><td>36</td><td>8 (from centre)</td></tr>
      <tr><td>Builder bot</td><td>20</td><td>2</td></tr>
      <tr><td>Gunner</td><td>13</td><td>2</td></tr>
      <tr><td>Sentinel</td><td>32</td><td>2</td></tr>
      <tr><td>Breach</td><td>2</td><td>2</td></tr>
      <tr><td>Launcher</td><td>26</td><td>2</td></tr>
    </tbody>
  </table>
</DenseTable>

### Cooldowns

All units have **action** and **move** cooldowns which are non-negative integers that decrease by 1 at the end of each round. Actions and movement can only be performed when the respective cooldown is 0.

<Info>
  The move cooldown is only used by the builder bot — it is the only mobile unit.
</Info>

### Markers

All units may place one [marker](/spec/other-buildings#marker) per round on a tile within action radius. This is separate from the action cooldown. You can overwrite an existing friendly marker, but not an enemy marker.

### Self-destruct

All units may self-destruct at any time. Builder bots do **not** deal damage when they self-destruct.

## Buildings

Buildings are game entities which are **immovable**. All entities are buildings except builder bots.

In particular, the core and turrets are considered **both a unit and a building**.

## Entity IDs

All entities (buildings and units) in the game have a **unique integer ID**. All [Controller](/api/controller) methods that deal with entities accept and return these IDs. Properties of an entity can be queried with getter functions — for example, `c.get_hp(id)`.

```python theme={"dark"}
# Get all nearby entities and check their type
for entity_id in c.get_nearby_entities():
    if c.get_entity_type(entity_id) == EntityType.HARVESTER:
        pos = c.get_position(entity_id)
```

<Info>
  The ID-based API was chosen for performance — constructing Python objects for every entity query would be too slow within the 2ms time limit.
</Info>

## Resource IDs

Each stored resource stack also has a **unique integer ID**. Use these IDs when you want to tell otherwise identical stacks apart in your logistics network.

You can query the stack currently stored in a conveyor, splitter, armoured conveyor, bridge, or foundry with `c.get_stored_resource_id(id)`. If the building is empty, it returns `None`.

```python theme={"dark"}
resource_type = c.get_stored_resource(conveyor_id)
resource_id = c.get_stored_resource_id(conveyor_id)
```

## Computation limit

Each unit gets **2ms of CPU time** per round. If your code exceeds this, execution is interrupted and `run()` is called fresh on the next round — **your bot does not resume where it left off**.

To absorb variance, each unit has an **extra time buffer** equal to 5% of the time limit. If a round takes longer than 2ms, the overage is deducted from the buffer. If a round takes less than 2ms, the savings are refunded to the buffer (capped at 5%).

Once a unit exhausts both its 2ms budget and its buffer in a single round, it is interrupted immediately.

Each bot process has a **1 GB memory limit**. Exceeding this will terminate the process.

Only Python standard library modules are available. External packages (e.g. `numpy`, `scipy`) cannot be imported — bots run in a sandboxed environment with no `pip install`.

<Warning>
  The local runner (`cambc run`) does **not** enforce time limits. Use `cambc match test` to test on the same AWS Graviton3 hardware that runs ladder matches.
</Warning>

## Debugging

* **stdout** via `print("msg")` is captured by the engine and saved to the replay. You can view each unit's output in the visualiser.
* **stderr** prints to the console in real time — use this for debugging during local runs.
* `c.draw_indicator_line(pos_a, pos_b, r, g, b)` and `c.draw_indicator_dot(pos, r, g, b)` draw debug overlays on the map, saved to the replay.


# Reference Tables
Source: https://docs.battlecode.cam/spec/reference

Quick-reference stat tables for all entities.

## Entity stats

<DenseTable>
  <table>
    <thead>
      <tr>
        <th>Entity</th>
        <th>HP</th>
        <th>Cost</th>
        <th>Scale</th>
        <th>Notes</th>
      </tr>
    </thead>

    <tbody>
      <tr>
        <td>
          <img alt="" />

          Core
        </td>

        <td>500</td>
        <td>—</td>
        <td>—</td>
        <td>3×3; spawns builders</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Builder bot
        </td>

        <td>40</td>
        <td>30 Ti</td>
        <td>20%</td>
        <td>Mobile; build, heal, attack, destroy</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Conveyor
        </td>

        <td>20</td>
        <td>3 Ti</td>
        <td>1%</td>
        <td>3 inputs, 1 output</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Splitter
        </td>

        <td>20</td>
        <td>6 Ti</td>
        <td>1%</td>
        <td>1 input, 3 rotating outputs</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Bridge
        </td>

        <td>20</td>
        <td>20 Ti</td>
        <td>10%</td>
        <td>Output to tile within dist 3</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Armoured conv.
        </td>

        <td>50</td>
        <td>5 Ti, 5 Ax</td>
        <td>1%</td>
        <td>Conveyor with more HP; immune to builder attacks</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Harvester
        </td>

        <td>30</td>
        <td>20 Ti</td>
        <td>5%</td>
        <td>Outputs every 4 rounds</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Foundry
        </td>

        <td>50</td>
        <td>40 Ti</td>
        <td>50%</td>
        <td>Ti + raw Ax → refined Ax</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Road
        </td>

        <td>4</td>
        <td>1 Ti</td>
        <td>0.5%</td>
        <td>Walkable</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Barrier
        </td>

        <td>30</td>
        <td>3 Ti</td>
        <td>1%</td>
        <td>Blocks space</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Marker
        </td>

        <td>1</td>
        <td>Free</td>
        <td>—</td>
        <td>No action cooldown</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Gunner
        </td>

        <td>40</td>
        <td>10 Ti</td>
        <td>10%</td>

        <td>
          Forward ray; markers are targetable but do not block; walls block and
          are not targetable; builder bots and non-marker buildings block; can
          rotate to any direction for 10 Ti
        </td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Sentinel
        </td>

        <td>30</td>
        <td>30 Ti</td>
        <td>20%</td>
        <td>Line ±1; refined Ax stuns +5 cd</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Breach
        </td>

        <td>60</td>
        <td>15 Ti, 10 Ax</td>
        <td>10%</td>
        <td>180° cone; friendly fire</td>
      </tr>

      <tr>
        <td>
          <img alt="" />

          Launcher
        </td>

        <td>30</td>
        <td>20 Ti</td>
        <td>10%</td>
        <td>Throws adjacent builders</td>
      </tr>
    </tbody>
  </table>
</DenseTable>

## Unit combat stats

<DenseTable>
  <table>
    <thead>
      <tr>
        <th>Unit</th>
        <th>Vision r²</th>
        <th>Action r²</th>
        <th>Attack r²</th>
        <th>Damage</th>
        <th>Reload</th>
        <th>Ammo/shot</th>
      </tr>
    </thead>

    <tbody>
      <tr>
        <td>Core</td>
        <td>36</td>
        <td>8</td>
        <td>—</td>
        <td>—</td>
        <td>—</td>
        <td>—</td>
      </tr>

      <tr>
        <td>Builder bot</td>
        <td>20</td>
        <td>2</td>
        <td>0 (own tile)</td>
        <td>2</td>
        <td>—</td>
        <td>2 Ti</td>
      </tr>

      <tr>
        <td>Gunner</td>
        <td>13</td>
        <td>2</td>
        <td>13</td>
        <td>10 (25 with Ax)</td>
        <td>1</td>
        <td>2</td>
      </tr>

      <tr>
        <td>Sentinel</td>
        <td>32</td>
        <td>2</td>
        <td>32</td>
        <td>18</td>
        <td>3</td>
        <td>10</td>
      </tr>

      <tr>
        <td>Breach</td>
        <td>2</td>
        <td>2</td>
        <td>24</td>
        <td>40 + 20 splash</td>
        <td>1</td>
        <td>5</td>
      </tr>

      <tr>
        <td>Launcher</td>
        <td>26</td>
        <td>2 (pickup)</td>
        <td>26 (throw)</td>
        <td>—</td>
        <td>1</td>
        <td>—</td>
      </tr>
    </tbody>
  </table>
</DenseTable>

## Game constants

<DenseTable>
  <table>
    <thead>
      <tr>
        <th>Constant</th>
        <th>Value</th>
      </tr>
    </thead>

    <tbody>
      <tr>
        <td>Max rounds</td>
        <td>2000</td>
      </tr>

      <tr>
        <td>Unit cap per team</td>
        <td>50 living units (including the core)</td>
      </tr>

      <tr>
        <td>Stack size</td>
        <td>10</td>
      </tr>

      <tr>
        <td>Starting titanium</td>
        <td>500</td>
      </tr>

      <tr>
        <td>Starting axionite</td>
        <td>0</td>
      </tr>

      <tr>
        <td>Passive titanium income</td>
        <td>10 every 4 rounds</td>
      </tr>

      <tr>
        <td>Builder bot heal</td>

        <td>
          4 HP for 1 Ti to all friendly entities on a tile within action radius
        </td>
      </tr>

      <tr>
        <td>Builder bot attack</td>
        <td>2 damage for 2 Ti (own tile only)</td>
      </tr>

      <tr>
        <td>Builder bot self-destruct damage</td>
        <td>0</td>
      </tr>

      <tr>
        <td>Harvester output interval</td>
        <td>Every 4 rounds</td>
      </tr>

      <tr>
        <td>Sentinel stun (refined axionite ammo)</td>
        <td>+5 action and move cooldown</td>
      </tr>

      <tr>
        <td>CPU time per unit per round</td>
        <td>2ms (+5% buffer)</td>
      </tr>

      <tr>
        <td>Memory limit per bot</td>
        <td>1 GB</td>
      </tr>
    </tbody>
  </table>
</DenseTable>

## Cost scaling

Every entity you build increases the cost multiplier. Scale starts at 1.0x (100%). Increases are **additive** — two gunners at +10% each give 1.2x, not 1.21x.

<DenseTable>
  <table>
    <thead>
      <tr>
        <th>Entity</th>
        <th>Scale increase</th>
      </tr>
    </thead>

    <tbody>
      <tr>
        <td>Road</td>
        <td>+0.5%</td>
      </tr>

      <tr>
        <td>Conveyor, splitter, armoured conveyor, barrier</td>
        <td>+1%</td>
      </tr>

      <tr>
        <td>Bridge</td>
        <td>+10%</td>
      </tr>

      <tr>
        <td>Harvester</td>
        <td>+5%</td>
      </tr>

      <tr>
        <td>Gunner, breach, launcher</td>
        <td>+10%</td>
      </tr>

      <tr>
        <td>Builder bot, sentinel</td>
        <td>+20%</td>
      </tr>

      <tr>
        <td>Axionite foundry</td>
        <td>+50%</td>
      </tr>
    </tbody>
  </table>
</DenseTable>

$$
\text{cost} = \lfloor \text{scale} \times \text{base cost} \rfloor
$$


# Resources
Source: https://docs.battlecode.cam/spec/resources

Titanium, axionite, and the cost scaling formula.

## Titanium

<img alt="Titanium" />

The primary resource used to construct most buildings. Each team starts with
**500 titanium** and gains **10 passive titanium every 4 rounds**.

Titanium is harvested from titanium ore deposits and delivered to the core via conveyors.

## Axionite

Axionite comes in two forms:

<CardGroup>
  <Card title="Raw axionite" icon="gem">
    <img alt="Raw axionite" />

    Mined from axionite ore deposits. When fed to a turret or core, it is **destroyed**. You must refine it first for advanced uses.
  </Card>

  <Card title="Refined axionite" icon="flask-vial">
    <img alt="Refined axionite" />

    Produced by [axionite foundries](/spec/harvester-and-foundry#axionite-foundry) from raw axionite + titanium. Used for powerful units and advanced infrastructure.
  </Card>
</CardGroup>

<Info>
  Whenever "axionite" is mentioned in the spec without qualification, it refers to **refined axionite**.
</Info>

## Conversion

The core can convert refined axionite from the global resource pool into
titanium with `c.convert(amount)`.

$$
1 \text{ Ax} \rightarrow 4 \text{ Ti}
$$

Converted axionite is removed from the Ax collected stat and added to the Ti
collected stat.

## Resource distribution

Resources are stored and moved in **stacks of 10**. At the end of each round, buildings that output resources send them to adjacent buildings that accept them.

Each stored stack also has a **resource ID**. You can query the stack currently sitting in a conveyor or other storage building with `c.get_stored_resource_id(...)`.

<Warning>
  Resources can be outputted to buildings belonging to the **opposing team**.
</Warning>

See [conveyors](/spec/conveyors), [harvester & foundry](/spec/harvester-and-foundry), and [turrets](/spec/turrets) for details on input/output directions.

## Cost scaling

Every building and unit you construct increases the cost of future builds. The cost of every building and unit is:

$$
\text{cost} = \lfloor \text{scale} \times \text{base cost} \rfloor
$$

Where scale starts at 1.0 and increases **additively** with each entity built — two gunners at +10% each give 1.2x, not 1.21x. You can query the current scale with `c.get_scale_percent()` which returns it as a percentage (100.0 at base).

<DenseTable>
  <table>
    <thead><tr><th>Entity</th><th>Scale increase</th></tr></thead>

    <tbody>
      <tr><td>Road</td><td>+0.5%</td></tr>
      <tr><td>Conveyor, splitter, armoured conveyor, barrier</td><td>+1%</td></tr>
      <tr><td>Bridge</td><td>+10%</td></tr>
      <tr><td>Harvester</td><td>+5%</td></tr>
      <tr><td>Gunner, breach, launcher</td><td>+10%</td></tr>
      <tr><td>Builder bot, sentinel</td><td>+20%</td></tr>
      <tr><td>Axionite foundry</td><td>+50%</td></tr>
    </tbody>
  </table>
</DenseTable>

When an entity is destroyed, its scaling contribution is removed — costs go back down.

<Tip>
  Every entity you build makes the next one more expensive. Be efficient with what you build!
</Tip>


# Turrets
Source: https://docs.battlecode.cam/spec/turrets

Defensive and offensive combat units — gunner, sentinel, breach, and launcher.

Every turret **except the launcher** faces in one of **8 directions**.

## Ammo

Gunners, sentinels, and breaches require **ammo** to fire. Ammo is simply a stack of resources sitting inside the turret — each shot consumes a fixed number of resources from that stack (see per-turret tables below). Launchers do not use ammo.

Ammo must be fed to turrets via conveyors, an adjacent harvester, an adjacent foundry, or a bridge targeting the turret's tile, from any direction except the direction the turret is facing. Diagonal turrets can be fed from all four sides.

Ammo-based turrets can hold up to one stack of one resource type and only accept incoming resources when completely empty.

<Info>
  If a builder bot is standing on a building, turret attacks on that tile hit
  **only the builder bot**.
</Info>

<Info>
  Raw axionite fed into a turret is **destroyed**. Only the ammo types listed
  below have any effect. Breach turrets additionally accept and destroy
  **all** resource types (including titanium) — see [Breach](#breach).
</Info>

## Gunner

<img alt="Gunner" />

Has a vision radius of √13. Fires along the forward ray up to range. Empty
tiles and markers do **not** block line of sight. Markers are still targetable.
Walls block the ray but are not targetable. Builder bots and non-marker
buildings are both targetable and blocking, so nothing beyond the first such
blocker is legal. Using refined axionite as ammo deals **25 damage** instead of 10.

<Info>
  Markers are the only occupied tiles that do **not** block a gunner. Walls
  block but are not targetable. Builder bots and non-marker buildings are both
  targetable and blocking.
</Info>

<Tip>
  `c.get_gunner_target()` returns the closest **targetable** occupied tile on
  the forward line. It may return a marker even if a farther legal target also
  exists behind that marker.
</Tip>

<Info>
  Gunners can rotate to any direction with `c.rotate(direction)`. This costs
  **10 Ti** from the global pool and applies a **1-turn cooldown**. Use
  `c.can_rotate(direction)` to preflight the move.
</Info>

| Property      | Value                         |
| ------------- | ----------------------------- |
| HP            | 40                            |
| Base cost     | 10 Ti                         |
| Scaling       | 10%                           |
| Damage        | 10 (25 with refined axionite) |
| Reload        | 1 round                       |
| Ammo per shot | 2                             |
| Vision r²     | 13                            |
| Attack r²     | 13 (same as vision)           |

<Tabs>
  <Tab title="Cardinal">
    <img alt="Gunner range — cardinal direction" />
  </Tab>

  <Tab title="Diagonal">
    <img alt="Gunner range — diagonal direction" />
  </Tab>
</Tabs>

## Sentinel

<img alt="Sentinel" />

High range support turret. Can hit all tiles within **1 king move** (Chebyshev distance) of the straight line in its facing direction, within vision range.

Using refined axionite instead of titanium as ammo adds **+5 to the action and move cooldown** of any unit directly hit — acting as a stun.

| Property      | Value               |
| ------------- | ------------------- |
| HP            | 30                  |
| Base cost     | 30 Ti               |
| Scaling       | 20%                 |
| Damage        | 18                  |
| Reload        | 3 rounds            |
| Ammo per shot | 10                  |
| Vision r²     | 32                  |
| Attack r²     | 32 (same as vision) |

<Tabs>
  <Tab title="Cardinal">
    <img alt="Sentinel range — cardinal direction" />
  </Tab>

  <Tab title="Diagonal">
    <img alt="Sentinel range — diagonal direction" />
  </Tab>
</Tabs>

<Tip>
  Sentinels with refined axionite ammo still disrupt builder bots by delaying
  both movement and actions.
</Tip>

## Breach

<img alt="Breach" />

Very high damage with **splash**. Attacks in a **180° cone** in the facing direction.

| Property      | Value                                       |
| ------------- | ------------------------------------------- |
| HP            | 60                                          |
| Base cost     | 15 Ti, 10 Ax                                |
| Scaling       | 10%                                         |
| Damage        | 40 direct + 20 splash (8 surrounding tiles) |
| Reload        | 1 round                                     |
| Ammo per shot | 5 (refined axionite only)                   |
| Vision r²     | 2                                           |
| Attack r²     | 24                                          |

<Tabs>
  <Tab title="Cardinal">
    <img alt="Breach range — cardinal direction" />
  </Tab>

  <Tab title="Diagonal">
    <img alt="Breach range — diagonal direction" />
  </Tab>
</Tabs>

<Info>
  Breach turrets accept **all** resource types, but only refined axionite is
  stored as ammo. Titanium and raw axionite delivered to a breach are consumed
  and **destroyed** on receipt. This prevents resources from backing up on
  conveyors feeding a breach.
</Info>

<Warning>
  Breach turrets have **friendly fire** on the splash damage (8 surrounding
  tiles). They do not damage themselves.
</Warning>

## Launcher

<img alt="Launcher" />

Picks up and **throws adjacent builder bots** to a target tile within range. The target tile must be bot-passable. Unlike other turrets, launchers have **no facing direction** and do not use ammo.

| Property  | Value               |
| --------- | ------------------- |
| HP        | 30                  |
| Base cost | 20 Ti               |
| Scaling   | 10%                 |
| Reload    | 1 round             |
| Vision r² | 26                  |
| Attack r² | 26 (same as vision) |

<img alt="Launcher range — blue is vision, red is throw range" />

```python theme={"dark"}
# Build a launcher (no direction needed)
c.build_launcher(pos)

# Launch a builder bot to a distant position
if c.can_launch(bot_pos, target_pos):
    c.launch(bot_pos, target_pos)
```

## Querying turret geometry

`c.get_attackable_tiles()` returns the raw geometric attack pattern for the
turret you are controlling, and
`c.get_attackable_tiles_from(position, direction, turret_type)` returns the same
pattern for a hypothetical turret at any position — callable from any controller.
Both ignore ammo, cooldown, occupancy, blockers, and other legality checks.

To check whether a shot is actually legal right now, use `c.can_fire(target)` on
a real turret, or `c.can_fire_from(position, direction, turret_type, target)` to
test a hypothetical shot against the current map's range and obstruction rules.

See the [Controller reference](/api/controller) for full method signatures.


