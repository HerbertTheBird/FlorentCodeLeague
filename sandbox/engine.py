"""Self-contained Florent ("Titan") game engine.

A from-scratch, steppable, mutable re-implementation of the game, faithful to the
published Florent rules (AGENTS.md / the fcode 2.3.3 diff). It exists so tools can
run the real competitor bots turn-by-turn and mutate the board live -- the packaged
compiled engine only exposes `run_game` (a whole match, no stepping/editing).

Fidelity notes (deliberate approximations, flagged for the sandbox):
  * Cost scale is a single per-team multiplier (get_scale_percent), += the built
    entity's weight on build, -= on destroy. cost = floor(scale * base).
  * Conveyor/splitter/harvester resource flow advances one hop per round with a
    belt-style simultaneous shift; splitters round-robin (LRU) their 3 outputs.
  * Combat: gunner first-obstruction ray, sentinel piercing line, launcher throw.
Everything the economy/opening logic touches (vision incl. the 2x2 core's 42.5
reach, building, cooldowns, comms, scaling, spawning) is modelled directly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from fcode_shim import (Direction, Environment, EntityType, GameConstants as GC,
                        GameError, Position, ResourceType, Team)

CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)

# Per-category cost-scale weight (integer PERCENT points, exact -- no float drift)
# and base cost / max hp / vision, keyed by type.
WEIGHT_PCT = {
    EntityType.BUILDER_BOT: 20, EntityType.CONVEYOR: 1, EntityType.SPLITTER: 1,
    EntityType.HARVESTER: 5, EntityType.BARRIER: 1, EntityType.GUNNER: 20,
    EntityType.SENTINEL: 20, EntityType.LAUNCHER: 10,
}
BASE_COST = {
    EntityType.BUILDER_BOT: GC.BUILDER_BOT_BASE_COST, EntityType.CONVEYOR: GC.CONVEYOR_BASE_COST,
    EntityType.SPLITTER: GC.SPLITTER_BASE_COST, EntityType.HARVESTER: GC.HARVESTER_BASE_COST,
    EntityType.BARRIER: GC.BARRIER_BASE_COST, EntityType.GUNNER: GC.GUNNER_BASE_COST,
    EntityType.SENTINEL: GC.SENTINEL_BASE_COST, EntityType.LAUNCHER: GC.LAUNCHER_BASE_COST,
}
MAX_HP = {
    EntityType.BUILDER_BOT: GC.BUILDER_BOT_MAX_HP, EntityType.CORE: GC.CORE_MAX_HP,
    EntityType.GUNNER: GC.GUNNER_MAX_HP, EntityType.SENTINEL: GC.SENTINEL_MAX_HP,
    EntityType.LAUNCHER: GC.LAUNCHER_MAX_HP, EntityType.CONVEYOR: GC.CONVEYOR_MAX_HP,
    EntityType.SPLITTER: GC.SPLITTER_MAX_HP, EntityType.HARVESTER: GC.HARVESTER_MAX_HP,
    EntityType.BARRIER: GC.BARRIER_MAX_HP,
}
VISION = {
    EntityType.CORE: GC.CORE_VISION_RADIUS_SQ, EntityType.BUILDER_BOT: GC.BUILDER_BOT_VISION_RADIUS_SQ,
    EntityType.GUNNER: GC.GUNNER_VISION_RADIUS_SQ, EntityType.SENTINEL: GC.SENTINEL_VISION_RADIUS_SQ,
    EntityType.LAUNCHER: GC.LAUNCHER_VISION_RADIUS_SQ,
}
DIRECTED = {EntityType.CONVEYOR, EntityType.SPLITTER, EntityType.GUNNER, EntityType.SENTINEL}
BUILDINGS = {EntityType.CORE, EntityType.CONVEYOR, EntityType.SPLITTER, EntityType.HARVESTER,
             EntityType.BARRIER, EntityType.GUNNER, EntityType.SENTINEL, EntityType.LAUNCHER}
TURRETS = {EntityType.GUNNER, EntityType.SENTINEL, EntityType.LAUNCHER}
# Entities that run bot code each turn (buildings do not).
ACTING = {EntityType.CORE, EntityType.BUILDER_BOT, EntityType.GUNNER,
          EntityType.SENTINEL, EntityType.LAUNCHER}
# Buildings a builder bot may stand on (is_tile_passable) if allied.
PASSABLE_BUILDINGS = {EntityType.CONVEYOR, EntityType.SPLITTER, EntityType.CORE}


@dataclass
class Entity:
    id: int
    type: EntityType
    team: Team
    x: int
    y: int
    hp: int
    max_hp: int
    action_cd: int = 0
    move_cd: int = 0
    direction: Direction | None = None
    alive: bool = True
    holding: bool = False          # conveyor/splitter/harvester holds a stack of 10
    stack_id: int | None = None
    built_round: int = 0
    split_rr: int = 0              # splitter round-robin cursor (0..2 over its 3 outputs)

    @property
    def cells(self):
        if self.type == EntityType.CORE:
            return [(self.x, self.y), (self.x + 1, self.y),
                    (self.x, self.y + 1), (self.x + 1, self.y + 1)]
        return [(self.x, self.y)]

    def min_dist_sq(self, x, y):
        return min((cx - x) ** 2 + (cy - y) ** 2 for cx, cy in self.cells)


@dataclass
class TeamState:
    titanium: int = GC.STARTING_TITANIUM
    ammo: int = 0
    scale_pct: int = 100
    store: list = field(default_factory=lambda: [0] * GC.STORE_SIZE)
    store_next: list = field(default_factory=lambda: [0] * GC.STORE_SIZE)
    converted_this_turn: bool = False
    titanium_collected: int = 0


class Engine:
    def __init__(self, width, height, terrain, cores, seed=1):
        self.width = width
        self.height = height
        self.terrain = terrain                       # [y][x] Environment
        self.round = 0
        self.next_id = 1
        self.next_stack = 1
        self.entities: dict[int, Entity] = {}
        self.building_at: dict[tuple, int] = {}       # (x,y) -> building id (core fills 4)
        self.bot_at: dict[tuple, int] = {}            # (x,y) -> builder bot id
        self.teams = {Team.A: TeamState(), Team.B: TeamState()}
        self.winner = None
        self.win_reason = None
        # cores: list of (team, x, y)
        self.core_id = {}
        for team, x, y in cores:
            e = self._add_entity(EntityType.CORE, team, x, y)
            self.core_id[team] = e.id
        # per-round acting order snapshot + cursor (for stepping)
        self._order: list[int] = []
        self._cursor = 0
        self._round_started = False

    # ---- entity/tile bookkeeping -------------------------------------------
    def _add_entity(self, etype, team, x, y, direction=None):
        e = Entity(id=self.next_id, type=etype, team=team, x=x, y=y,
                   hp=MAX_HP[etype], max_hp=MAX_HP[etype], direction=direction,
                   built_round=self.round)
        self.next_id += 1
        self.entities[e.id] = e
        if etype == EntityType.BUILDER_BOT:
            self.bot_at[(x, y)] = e.id
        else:
            for c in e.cells:
                self.building_at[c] = e.id
        if etype not in (EntityType.CORE,):
            self.teams[team].scale_pct += WEIGHT_PCT.get(etype, 0)
        return e

    def _remove_entity(self, e: Entity, refund_scale=True):
        e.alive = False
        if e.type == EntityType.BUILDER_BOT:
            self.bot_at.pop((e.x, e.y), None)
        else:
            for c in e.cells:
                if self.building_at.get(c) == e.id:
                    del self.building_at[c]
        if refund_scale and e.type != EntityType.CORE:
            self.teams[e.team].scale_pct -= WEIGHT_PCT.get(e.type, 0)
        self.entities.pop(e.id, None)
        if e.type == EntityType.CORE and self.winner is None:
            self.winner = Team.B if e.team == Team.A else Team.A
            self.win_reason = "core_destroyed"

    def in_bounds(self, x, y):
        return 0 <= x < self.width and 0 <= y < self.height

    def env(self, x, y):
        if not self.in_bounds(x, y):
            return Environment.WALL
        return self.terrain[y][x]

    def team_units(self, team):
        return [e for e in self.entities.values()
                if e.team == team and e.type not in (
                    EntityType.CONVEYOR, EntityType.SPLITTER, EntityType.HARVESTER,
                    EntityType.BARRIER)]

    def unit_count(self, team):
        # "units" = core + builder bots + turrets (everything that isn't a pure building)
        return len(self.team_units(team))

    def cost(self, team, etype):
        return BASE_COST[etype] * self.teams[team].scale_pct // 100

    # ---- stepping ----------------------------------------------------------
    def _begin_round(self):
        # snapshot acting order (units alive at round start), ascending id.
        self._order = sorted(e.id for e in self.entities.values() if e.type in ACTING)
        self._cursor = 0
        self._round_started = True

    def units_remaining(self):
        return [i for i in self._order[self._cursor:] if i in self.entities]

    def current_unit_id(self):
        while self._round_started and self._cursor < len(self._order):
            uid = self._order[self._cursor]
            if uid in self.entities:
                return uid
            self._cursor += 1
        return None

    def advance_cursor(self):
        self._cursor += 1

    def round_done(self):
        return self._round_started and self.current_unit_id() is None

    def end_round(self):
        """End-of-round resolution: resources, passive Ti, cooldowns, comms swap."""
        self._distribute_resources()
        # passive titanium
        if (self.round + 1) % GC.PASSIVE_TITANIUM_INTERVAL == 0:
            for t in self.teams.values():
                t.titanium += GC.PASSIVE_TITANIUM_AMOUNT
        # cooldowns
        for e in self.entities.values():
            if e.action_cd > 0:
                e.action_cd -= 1
            if e.move_cd > 0:
                e.move_cd -= 1
        # comms buffer swap + reset per-turn flags
        for t in self.teams.values():
            t.store = list(t.store_next)
            t.converted_this_turn = False
        self.round += 1
        self._round_started = False

    # ---- resource flow -----------------------------------------------------
    def _accepts_from(self, target: Entity, from_dir: Direction) -> bool:
        """Can `target` accept a stack arriving from direction `from_dir` (the
        direction from target toward the source)?"""
        if target.type == EntityType.CORE:
            return True
        if target.type == EntityType.CONVEYOR:
            return target.direction != from_dir      # accepts from its 3 non-output sides
        if target.type == EntityType.SPLITTER:
            return target.direction == from_dir       # accepts only from its back (its `direction`)
        return False

    def _output_tile(self, e: Entity):
        d = e.direction
        return (e.x + d.delta()[0], e.y + d.delta()[1])

    def _distribute_resources(self):
        # 1) harvesters generate a stack on build and every 4 rounds after.
        for e in self.entities.values():
            if e.type == EntityType.HARVESTER and not e.holding:
                if (self.round - e.built_round) % 4 == 0:
                    e.holding = True
                    e.stack_id = self.next_stack
                    self.next_stack += 1
        # 2) one-hop belt shift. Each holder advances its stack one tile toward
        # its output; a tile receives AT MOST ONE stack (no merging/loss). A
        # holder moves iff its destination is the core, or the destination tile
        # is empty / vacated by another mover this tick and not already claimed.
        # Fixed point over a deterministic (position, id) order.
        holders = sorted(
            (e for e in self.entities.values() if e.holding and e.type in (
                EntityType.CONVEYOR, EntityType.SPLITTER, EntityType.HARVESTER)),
            key=lambda e: (e.x, e.y, e.id))
        dest = {e.id: self._pick_dest(e) for e in holders}
        moving = set()
        claimed = {}                                  # target tile -> holder id
        for _ in range(len(holders) + 2):
            changed = False
            for e in holders:
                if e.id in moving:
                    continue
                d = dest[e.id]
                if d is None:
                    continue
                if d[0] == "core":
                    moving.add(e.id); changed = True; continue
                tgt = d[1]
                key = (tgt.x, tgt.y)
                vacates = (not tgt.holding) or (tgt.id in moving)
                if vacates and key not in claimed:
                    moving.add(e.id); claimed[key] = e.id; changed = True
            if not changed:
                break
        for e in holders:
            if e.id not in moving:
                continue
            d = dest[e.id]
            e.holding = False; e.stack_id = None
            if d[0] == "core":
                team = d[1]
                self.teams[team].titanium += GC.STACK_SIZE
                self.teams[team].titanium_collected += GC.STACK_SIZE
            else:
                tgt = d[1]
                tgt.holding = True
                tgt.stack_id = self.next_stack; self.next_stack += 1
                if e.type == EntityType.SPLITTER:
                    e.split_rr += 1

    def _pick_dest(self, e: Entity):
        if e.type == EntityType.CONVEYOR:
            tx, ty = self._output_tile(e)
            return self._dest_for(e, tx, ty, e.direction)
        if e.type == EntityType.HARVESTER:
            # push to any adjacent building that accepts from the harvester
            for d in CARDINALS:
                tx, ty = e.x + d.delta()[0], e.y + d.delta()[1]
                r = self._dest_for(e, tx, ty, d)
                if r is not None:
                    return r
            return None
        if e.type == EntityType.SPLITTER:
            # outputs are the 3 cardinals != back(direction), round-robin (LRU) order
            outs = [d for d in CARDINALS if d != e.direction]
            for k in range(3):
                d = outs[(e.split_rr + k) % 3]
                tx, ty = e.x + d.delta()[0], e.y + d.delta()[1]
                r = self._dest_for(e, tx, ty, d)
                if r is not None:
                    e.split_rr = (e.split_rr + k)     # remember which we used
                    return r
            return None
        return None

    def _dest_for(self, src, tx, ty, out_dir):
        bid = self.building_at.get((tx, ty))
        if bid is None:
            return None
        tgt = self.entities.get(bid)
        if tgt is None:
            return None
        arriving_from = out_dir.opposite()            # direction from target toward src
        if tgt.type == EntityType.CORE:
            return ("core", tgt.team)
        if self._accepts_from(tgt, arriving_from):
            return ("move", tgt, out_dir)
        return None

    # ---- vision ------------------------------------------------------------
    def visible(self, viewer: Entity, x, y):
        return viewer.min_dist_sq(x, y) <= VISION[viewer.type]

    # ---- free "god-mode" edits (for the interactive viewer) ----------------
    # Each returns an "undo" thunk (call to reverse the edit) or None on no-op.
    def _readd(self, snap: Entity):
        """Re-insert a previously-removed entity (same id/state)."""
        snap.alive = True
        self.entities[snap.id] = snap
        if snap.type == EntityType.BUILDER_BOT:
            self.bot_at[(snap.x, snap.y)] = snap.id
        else:
            for c in snap.cells:
                self.building_at[c] = snap.id
        if snap.type != EntityType.CORE:
            self.teams[snap.team].scale_pct += WEIGHT_PCT.get(snap.type, 0)

    def _entity_at(self, x, y):
        eid = self.bot_at.get((x, y)) or self.building_at.get((x, y))
        return self.entities.get(eid) if eid else None

    def god_place(self, etype, team, x, y, direction=None):
        if not self.in_bounds(x, y) or (x, y) in self.building_at or (x, y) in self.bot_at:
            return None
        if etype == EntityType.HARVESTER:
            if self.env(x, y) != Environment.ORE_TITANIUM:
                return None
        elif self.env(x, y) == Environment.WALL:
            return None
        if etype in DIRECTED and direction is None:
            direction = Direction.NORTH
        e = self._add_entity(etype, team, x, y, direction=direction)
        eid = e.id
        def undo():
            if eid in self.entities:
                self._remove_entity(self.entities[eid])
        return undo

    def god_delete(self, x, y):
        e = self._entity_at(x, y)
        if e is None or e.type == EntityType.CORE:
            return None
        import copy
        snap = copy.deepcopy(e)
        self._remove_entity(e)
        return lambda: self._readd(snap)

    def god_damage(self, x, y, dmg):
        e = self._entity_at(x, y)
        if e is None:
            return None
        import copy
        snap = copy.deepcopy(e)
        if e.type == EntityType.CORE and e.hp - dmg <= 0:
            e.hp = 1                              # don't end the game from a stray click
            return lambda: setattr(self.entities.get(snap.id, snap), "hp", snap.hp) if snap.id in self.entities else None
        e.hp -= dmg
        removed = e.hp <= 0
        if removed:
            self._remove_entity(e)
        def undo():
            if removed:
                self._readd(snap)
            elif snap.id in self.entities:
                self.entities[snap.id].hp = snap.hp
        return undo

    def god_heal(self, x, y, amt):
        e = self._entity_at(x, y)
        if e is None:
            return None
        old = e.hp
        eid = e.id
        e.hp = min(e.max_hp, e.hp + amt)
        def undo():
            if eid in self.entities:
                self.entities[eid].hp = old
        return undo


class UnitController:
    """The `ct`/`rc` object handed to a unit's bot each turn. Scoped to `uid`."""

    def __init__(self, engine: Engine, uid: int):
        self.e = engine
        self.uid = uid

    # -- helpers
    @property
    def _me(self):
        return self.e.entities[self.uid]

    def _ent(self, id):
        m = self.e.entities.get(self.uid if id is None else id)
        if m is None:
            raise GameError(f"no entity {id}")
        return m

    def _require_alive(self):
        if self.uid not in self.e.entities:
            raise GameError("this unit is dead")

    # -- info
    def get_team(self, id=None):
        return self._ent(id).team

    def get_position(self, id=None):
        e = self._ent(id)
        return Position(e.x, e.y)

    def get_id(self):
        return self.uid

    def get_action_cooldown(self):
        return self._me.action_cd

    def get_move_cooldown(self):
        return self._me.move_cd

    def can_act(self):
        return self._me.action_cd == 0

    def get_vision_radius_sq(self, id=None):
        return VISION.get(self._ent(id).type, 0)

    def get_hp(self, id=None):
        return self._ent(id).hp

    def get_max_hp(self, id=None):
        return self._ent(id).max_hp

    def get_entity_type(self, id=None):
        return self._ent(id).type

    def get_direction(self, id=None):
        d = self._ent(id).direction
        if d is None:
            raise GameError("entity has no direction")
        return d

    def get_stored_resource(self, id=None):
        e = self._ent(id)
        if e.type not in (EntityType.CONVEYOR, EntityType.SPLITTER):
            raise GameError("entity has no storage")
        return ResourceType.TITANIUM if e.holding else None

    def get_stored_resource_id(self, id=None):
        e = self._ent(id)
        if e.type not in (EntityType.CONVEYOR, EntityType.SPLITTER):
            raise GameError("entity has no storage")
        return e.stack_id if e.holding else None

    def get_tile_env(self, pos):
        return self.e.env(pos.x, pos.y)

    def get_tile_building_id(self, pos):
        return self.e.building_at.get((pos.x, pos.y))

    def get_tile_builder_bot_id(self, pos):
        return self.e.bot_at.get((pos.x, pos.y))

    def is_tile_empty(self, pos):
        if self.e.env(pos.x, pos.y) == Environment.WALL:
            return False
        return (pos.x, pos.y) not in self.e.building_at

    def is_tile_passable(self, pos):
        if not self.e.in_bounds(pos.x, pos.y):
            return False
        if self.e.env(pos.x, pos.y) == Environment.WALL:
            return False
        if (pos.x, pos.y) in self.e.bot_at:
            return False
        bid = self.e.building_at.get((pos.x, pos.y))
        if bid is None:
            return True
        b = self.e.entities.get(bid)
        return b is not None and b.type in PASSABLE_BUILDINGS and b.team == self._me.team

    def is_in_vision(self, pos):
        return self.e.visible(self._me, pos.x, pos.y)

    def get_nearby_tiles(self, dist_sq=None):
        me = self._me
        r = VISION[me.type] if dist_sq is None else min(dist_sq, VISION[me.type])
        out = []
        rad = int(r ** 0.5) + 1
        for cx, cy in me.cells:
            pass
        # iterate a bounding box around all cells
        minx = min(c[0] for c in me.cells) - rad
        maxx = max(c[0] for c in me.cells) + rad
        miny = min(c[1] for c in me.cells) - rad
        maxy = max(c[1] for c in me.cells) + rad
        seen = set()
        for y in range(max(0, miny), min(self.e.height, maxy + 1)):
            for x in range(max(0, minx), min(self.e.width, maxx + 1)):
                if me.min_dist_sq(x, y) <= r and (x, y) not in seen:
                    seen.add((x, y))
                    out.append(Position(x, y))
        return out

    def _nearby_ids(self, dist_sq, pred):
        me = self._me
        r = VISION[me.type] if dist_sq is None else min(dist_sq, VISION[me.type])
        out = []
        for e in self.e.entities.values():
            if e.id == me.id:
                continue
            if e.min_dist_sq(me.x, me.y) <= r or me.min_dist_sq(e.x, e.y) <= r:
                if pred(e):
                    out.append(e.id)
        return out

    def get_nearby_entities(self, dist_sq=None):
        return self._nearby_ids(dist_sq, lambda e: True)

    def get_nearby_buildings(self, dist_sq=None):
        return self._nearby_ids(dist_sq, lambda e: e.type in BUILDINGS)

    def get_nearby_units(self, dist_sq=None):
        return self._nearby_ids(
            dist_sq, lambda e: e.type in (EntityType.CORE, EntityType.BUILDER_BOT,
                                          EntityType.GUNNER, EntityType.SENTINEL,
                                          EntityType.LAUNCHER))

    def get_map_width(self):
        return self.e.width

    def get_map_height(self):
        return self.e.height

    def get_current_round(self):
        return self.e.round

    def get_global_resources(self):
        return self.e.teams[self._me.team].titanium

    def get_global_ammo(self):
        return self.e.teams[self._me.team].ammo

    def get_scale_percent(self):
        return float(self.e.teams[self._me.team].scale_pct)

    def get_cpu_time_elapsed(self):
        return 0

    def get_unit_count(self):
        return self.e.unit_count(self._me.team)

    # cost getters
    def _c(self, t):
        return self.e.cost(self._me.team, t)

    def get_conveyor_cost(self): return self._c(EntityType.CONVEYOR)
    def get_splitter_cost(self): return self._c(EntityType.SPLITTER)
    def get_harvester_cost(self): return self._c(EntityType.HARVESTER)
    def get_barrier_cost(self): return self._c(EntityType.BARRIER)
    def get_gunner_cost(self): return self._c(EntityType.GUNNER)
    def get_sentinel_cost(self): return self._c(EntityType.SENTINEL)
    def get_launcher_cost(self): return self._c(EntityType.LAUNCHER)
    def get_builder_bot_cost(self): return self._c(EntityType.BUILDER_BOT)

    # -- movement (builder only)
    def can_move(self, direction):
        me = self._me
        if me.type != EntityType.BUILDER_BOT or not direction.is_cardinal():
            return False
        if me.action_cd != 0 or me.move_cd != 0:
            return False
        nx, ny = me.x + direction.delta()[0], me.y + direction.delta()[1]
        return self.is_tile_passable(Position(nx, ny))

    def move(self, direction):
        if not self.can_move(direction):
            raise GameError("illegal move")
        me = self._me
        self.e.bot_at.pop((me.x, me.y), None)
        me.x += direction.delta()[0]
        me.y += direction.delta()[1]
        self.e.bot_at[(me.x, me.y)] = me.id
        me.move_cd = 1
        me.action_cd = max(me.action_cd, 1)

    # -- building
    def _adjacent_ortho(self, pos):
        me = self._me
        return abs(pos.x - me.x) + abs(pos.y - me.y) == 1

    def _can_build(self, etype, pos, direction=None):
        me = self._me
        if me.type != EntityType.BUILDER_BOT:
            return False
        if me.action_cd != 0 or me.move_cd != 0:
            return False
        if not self._adjacent_ortho(pos):
            return False
        if not self.e.in_bounds(pos.x, pos.y):
            return False
        if etype == EntityType.HARVESTER:
            if self.e.env(pos.x, pos.y) != Environment.ORE_TITANIUM:
                return False
        else:
            if self.e.env(pos.x, pos.y) == Environment.WALL:
                return False
        if (pos.x, pos.y) in self.e.building_at or (pos.x, pos.y) in self.e.bot_at:
            return False
        if etype in DIRECTED and direction is None:
            return False
        if etype in (EntityType.GUNNER, EntityType.SENTINEL, EntityType.LAUNCHER):
            if self.e.unit_count(me.team) >= GC.MAX_TEAM_UNITS:
                return False
        return self.e.teams[me.team].titanium >= self.e.cost(me.team, etype)

    def _do_build(self, etype, pos, direction=None):
        if not self._can_build(etype, pos, direction):
            raise GameError(f"illegal build {etype}")
        me = self._me
        self.e.teams[me.team].titanium -= self.e.cost(me.team, etype)
        e = self.e._add_entity(etype, me.team, pos.x, pos.y, direction=direction)
        me.action_cd = max(me.action_cd, 1)
        me.move_cd = max(me.move_cd, 1)
        return e.id

    def can_build_conveyor(self, position, direction): return self._can_build(EntityType.CONVEYOR, position, direction)
    def can_build_splitter(self, position, direction): return self._can_build(EntityType.SPLITTER, position, direction)
    def can_build_harvester(self, position): return self._can_build(EntityType.HARVESTER, position)
    def can_build_barrier(self, position): return self._can_build(EntityType.BARRIER, position)
    def can_build_gunner(self, position, direction): return self._can_build(EntityType.GUNNER, position, direction)
    def can_build_sentinel(self, position, direction): return self._can_build(EntityType.SENTINEL, position, direction)
    def can_build_launcher(self, position): return self._can_build(EntityType.LAUNCHER, position)

    def build_conveyor(self, position, direction): return self._do_build(EntityType.CONVEYOR, position, direction)
    def build_splitter(self, position, direction): return self._do_build(EntityType.SPLITTER, position, direction)
    def build_harvester(self, position): return self._do_build(EntityType.HARVESTER, position)
    def build_barrier(self, position): return self._do_build(EntityType.BARRIER, position)
    def build_gunner(self, position, direction): return self._do_build(EntityType.GUNNER, position, direction)
    def build_sentinel(self, position, direction): return self._do_build(EntityType.SENTINEL, position, direction)
    def build_launcher(self, position): return self._do_build(EntityType.LAUNCHER, position)

    def can_build(self, entity_type, position, extra=None):
        return self._can_build(entity_type, position, extra if isinstance(extra, Direction) else None)

    def build(self, entity_type, position, extra=None):
        return self._do_build(entity_type, position, extra if isinstance(extra, Direction) else None)

    # -- heal / destroy
    def can_heal(self, position):
        me = self._me
        if me.type != EntityType.BUILDER_BOT or me.action_cd != 0 or me.move_cd != 0:
            return False
        if not self._adjacent_ortho(position):
            return False
        if self.e.teams[me.team].titanium < GC.BUILDER_BOT_HEAL_COST:
            return False
        return any(t is not None for t in self._friendly_damaged_at(position))

    def _friendly_damaged_at(self, position):
        out = []
        bid = self.e.building_at.get((position.x, position.y))
        if bid:
            b = self.e.entities[bid]
            if b.team == self._me.team and b.hp < b.max_hp:
                out.append(b)
        botid = self.e.bot_at.get((position.x, position.y))
        if botid:
            b = self.e.entities[botid]
            if b.team == self._me.team and b.hp < b.max_hp:
                out.append(b)
        return out

    def heal(self, position):
        if not self.can_heal(position):
            raise GameError("illegal heal")
        me = self._me
        self.e.teams[me.team].titanium -= GC.BUILDER_BOT_HEAL_COST
        for b in self._friendly_damaged_at(position):
            b.hp = min(b.max_hp, b.hp + GC.HEAL_AMOUNT)
        me.action_cd = max(me.action_cd, 1)
        me.move_cd = max(me.move_cd, 1)

    def can_destroy(self, building_pos):
        me = self._me
        if me.type != EntityType.BUILDER_BOT:
            return False
        if not self._adjacent_ortho(building_pos):
            return False
        bid = self.e.building_at.get((building_pos.x, building_pos.y))
        if not bid:
            return False
        b = self.e.entities[bid]
        return b.team == me.team and b.type != EntityType.CORE

    def destroy(self, building_pos):
        if not self.can_destroy(building_pos):
            raise GameError("illegal destroy")
        bid = self.e.building_at[(building_pos.x, building_pos.y)]
        self.e._remove_entity(self.e.entities[bid])

    def self_destruct(self):
        self.e._remove_entity(self._me)

    def resign(self, message=None):
        me = self._me
        core = self.e.entities.get(self.e.core_id.get(me.team))
        if core:
            self.e._remove_entity(core)
        self.e.win_reason = "resigned"

    # -- comms
    def read_store(self, index):
        return self.e.teams[self._me.team].store[index]

    def write_store(self, index, value):
        self.e.teams[self._me.team].store_next[index] = value & 0xFFFFFFFF

    # -- core
    def can_spawn(self, position):
        me = self._me
        if me.type != EntityType.CORE or me.action_cd != 0:
            return False
        if self.e.unit_count(me.team) >= GC.MAX_TEAM_UNITS:
            return False
        if not self.e.in_bounds(position.x, position.y):
            return False
        if self.e.env(position.x, position.y) == Environment.WALL:
            return False
        if (position.x, position.y) in self.e.building_at or (position.x, position.y) in self.e.bot_at:
            return False
        if me.min_dist_sq(position.x, position.y) > GC.CORE_SPAWNING_RADIUS_SQ:
            return False
        return self.e.teams[me.team].titanium >= self.e.cost(me.team, EntityType.BUILDER_BOT)

    def spawn_builder(self, position):
        if not self.can_spawn(position):
            raise GameError("illegal spawn")
        me = self._me
        self.e.teams[me.team].titanium -= self.e.cost(me.team, EntityType.BUILDER_BOT)
        b = self.e._add_entity(EntityType.BUILDER_BOT, me.team, position.x, position.y)
        b.action_cd = 1
        b.move_cd = 1
        me.action_cd = 1
        return b.id

    def can_convert_ammo(self, amount):
        me = self._me
        t = self.e.teams[me.team]
        return (me.type == EntityType.CORE and not t.converted_this_turn
                and amount > 0 and t.titanium >= amount)

    def convert_ammo(self, amount):
        if not self.can_convert_ammo(amount):
            raise GameError("illegal convert_ammo")
        t = self.e.teams[self._me.team]
        t.titanium -= amount
        t.ammo += amount
        t.converted_this_turn = True

    # -- turrets / firing
    def _line_tiles(self, pos, direction, turret_type):
        r = VISION[turret_type]
        out = []
        dx, dy = direction.delta()
        x, y = pos.x, pos.y
        step = 1
        while True:
            x += dx; y += dy
            if (x - pos.x) ** 2 + (y - pos.y) ** 2 > r:
                break
            if not self.e.in_bounds(x, y):
                break
            out.append(Position(x, y))
            step += 1
            if step > 60:
                break
        return out

    def get_attackable_tiles(self):
        me = self._me
        if me.type not in TURRETS:
            raise GameError("not a turret")
        return self.get_attackable_tiles_from(Position(me.x, me.y), me.direction, me.type)

    def get_attackable_tiles_from(self, position, direction, turret_type):
        if turret_type == EntityType.LAUNCHER:
            out = []
            r = VISION[EntityType.LAUNCHER]
            for y in range(self.e.height):
                for x in range(self.e.width):
                    if (x - position.x) ** 2 + (y - position.y) ** 2 <= r:
                        out.append(Position(x, y))
            return out
        return self._line_tiles(position, direction, turret_type)

    def _first_gunner_target(self, pos, direction):
        dx, dy = direction.delta()
        r = VISION[EntityType.GUNNER]
        x, y = pos.x, pos.y
        while True:
            x += dx; y += dy
            if (x - pos.x) ** 2 + (y - pos.y) ** 2 > r or not self.e.in_bounds(x, y):
                return None
            if self.e.env(x, y) == Environment.WALL:
                return None                       # wall blocks, not targetable
            if (x, y) in self.e.building_at or (x, y) in self.e.bot_at:
                return Position(x, y)

    def get_gunner_target(self):
        me = self._me
        if me.type != EntityType.GUNNER:
            raise GameError("not a gunner")
        return self._first_gunner_target(Position(me.x, me.y), me.direction)

    def can_fire(self, target):
        me = self._me
        if me.action_cd != 0:
            return False
        if me.type == EntityType.BUILDER_BOT:
            if not self._adjacent_ortho(target):
                return False
            if self.e.teams[me.team].titanium < GC.BUILDER_BOT_ATTACK_COST:
                return False
            return (target.x, target.y) in self.e.building_at
        if me.type == EntityType.GUNNER:
            tgt = self._first_gunner_target(Position(me.x, me.y), me.direction)
            return (tgt == target and self.e.teams[me.team].ammo >= GC.GUNNER_AMMO_COST)
        if me.type == EntityType.SENTINEL:
            if target not in self._line_tiles(Position(me.x, me.y), me.direction, me.type):
                return False
            occupied = (target.x, target.y) in self.e.building_at or (target.x, target.y) in self.e.bot_at
            return occupied and self.e.teams[me.team].ammo >= GC.SENTINEL_AMMO_COST
        return False

    def can_fire_from(self, position, direction, turret_type, target):
        if turret_type == EntityType.GUNNER:
            return self._first_gunner_target(position, direction) == target
        if turret_type == EntityType.SENTINEL:
            return target in self._line_tiles(position, direction, turret_type)
        return False

    def fire(self, target):
        if not self.can_fire(target):
            raise GameError("illegal fire")
        me = self._me
        if me.type == EntityType.BUILDER_BOT:
            self.e.teams[me.team].titanium -= GC.BUILDER_BOT_ATTACK_COST
            self._damage_tile(target, GC.BUILDER_BOT_ATTACK_DAMAGE, buildings_only=True)
            me.action_cd = max(me.action_cd, 1); me.move_cd = max(me.move_cd, 1)
        elif me.type == EntityType.GUNNER:
            self.e.teams[me.team].ammo -= GC.GUNNER_AMMO_COST
            self._damage_tile(target, GC.GUNNER_DAMAGE)
            me.action_cd = GC.GUNNER_FIRE_COOLDOWN
        elif me.type == EntityType.SENTINEL:
            self.e.teams[me.team].ammo -= GC.SENTINEL_AMMO_COST
            self._damage_tile(target, GC.SENTINEL_DAMAGE)
            me.action_cd = GC.SENTINEL_FIRE_COOLDOWN

    def _damage_tile(self, target, dmg, buildings_only=False):
        for table in ((self.e.building_at,) if buildings_only else (self.e.building_at, self.e.bot_at)):
            eid = table.get((target.x, target.y))
            if eid and eid in self.e.entities:
                ent = self.e.entities[eid]
                ent.hp -= dmg
                if ent.hp <= 0:
                    self.e._remove_entity(ent)

    def can_rotate(self, direction):
        me = self._me
        return (me.type == EntityType.GUNNER and me.action_cd == 0
                and direction != me.direction
                and self.e.teams[me.team].titanium >= GC.GUNNER_ROTATE_COST)

    def rotate(self, direction):
        if not self.can_rotate(direction):
            raise GameError("illegal rotate")
        me = self._me
        self.e.teams[me.team].titanium -= GC.GUNNER_ROTATE_COST
        me.direction = direction
        me.action_cd = GC.GUNNER_ROTATE_COOLDOWN

    def can_launch(self, bot_pos, target):
        me = self._me
        if me.type != EntityType.LAUNCHER or me.action_cd != 0:
            return False
        botid = self.e.bot_at.get((bot_pos.x, bot_pos.y))
        if not botid:
            return False
        if me.min_dist_sq(bot_pos.x, bot_pos.y) > 2:
            return False
        if (me.x - target.x) ** 2 + (me.y - target.y) ** 2 > VISION[EntityType.LAUNCHER]:
            return False
        return self.is_tile_passable(target) or (
            self.e.in_bounds(target.x, target.y)
            and self.e.env(target.x, target.y) != Environment.WALL
            and (target.x, target.y) not in self.e.building_at
            and (target.x, target.y) not in self.e.bot_at)

    def launch(self, bot_pos, target):
        if not self.can_launch(bot_pos, target):
            raise GameError("illegal launch")
        me = self._me
        botid = self.e.bot_at[(bot_pos.x, bot_pos.y)]
        b = self.e.entities[botid]
        self.e.bot_at.pop((b.x, b.y), None)
        b.x, b.y = target.x, target.y
        self.e.bot_at[(b.x, b.y)] = b.id
        me.action_cd = GC.LAUNCHER_FIRE_COOLDOWN

    # -- indicators (no-op; the viewer draws its own)
    def draw_indicator_line(self, a, b, r, g, bl): pass
    def draw_indicator_dot(self, p, r, g, bl): pass
