"""Rusher -- a reference opponent that plays a fast forward-sentinel rush.

This is a measurement instrument, not a competitor.  Its only job is to kill an
undefended core early and reliably so that rush defences can be A/B tested
against something that actually rushes.

Shape of the plan (copied from what the real ladder bots do to us):

    t0..t2   spawn three builder bots on the side of the core facing the enemy
    t3..t20  walk them at the enemy core, no economy at all
    then     plant SENTINELs at maximum standoff, already aimed at the core,
             and pour every remaining titanium through them as ammunition

A sentinel's line ignores obstacles, so the only thing that matters is standing
on a tile that is cardinally aligned within 5, or exactly diagonal within 4, of
one of the four enemy core tiles.

The whole bot is one piece of arithmetic.  Damage per titanium is fixed at 1.8
(18 damage per 10 ammo) whatever the turret count, income after the opening is
only 2.5 titanium a round, and a defender heals its core for 4 HP per titanium
-- more than twice as efficiently as we can damage it.  So a slow grind can
never win: everything has to come out of the opening 500, spent as fast as the
guns can fire it.  That is why there are three builders and not five, why the
core keeps almost no reserve and almost no ammunition buffered, and why
builders only heal out of clear surplus.  Once nothing is firing any more the
priorities invert and titanium is hoarded to put a gun back up.

Nothing is hardcoded per map.  The enemy core is predicted from map symmetry,
narrowed down by terrain seen along the way, and confirmed on sight.
"""

import sys

from fcode import Direction, EntityType, Environment, Position

# --- tuning ---------------------------------------------------------------
NUM_BUILDERS = 3         # opening builder spawns -- every body is lost ammo
AMMO_CAP = 20            # two sentinel shots buffered; more is dead capital
TI_RESERVE_IDLE = 220    # titanium the core protects while nothing is firing
TI_RESERVE_FIRING = 20   # ... and once turrets are actually spending ammo
SENTINEL_TI_FLOOR = 240  # balance a builder needs to add a gun to a live line
REBUILD_TI_FLOOR = 40    # ... and to put the first, or a replacement, gun up
QUIET_ROUNDS = 6         # no ammo spent for this long => our turrets are gone
REFLOAT_TI = 400         # this rich means the push stalled -- send more bodies
HEAL_TI_FLOOR = 150      # heal only out of surplus -- ammunition comes first
CHIP_TI = 260            # spare enough to also punch the core by hand
GOAL_REFRESH = 20        # rounds before a builder reconsiders its target tile
GUARD_TURRETS = (EntityType.SENTINEL, EntityType.GUNNER)

CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)
ALL_DIRS = CARDINALS + (Direction.NORTHEAST, Direction.SOUTHEAST,
                        Direction.SOUTHWEST, Direction.NORTHWEST)
CARDINAL_REACH = 5       # verified against get_attackable_tiles_from()
DIAGONAL_REACH = 4

# Team-wide communication store.
SLOT_ENEMY_CORE = 0      # x + y * width + 1, 0 means "not seen yet"
SLOT_DEAD_SYMS = 1       # bitmask of symmetries ruled out by observed terrain
SLOT_FIRING = 2          # 1 while ammunition is being spent, i.e. guns are up

SYM_HOR, SYM_VER, SYM_ROT = 1, 2, 4


class Player:
    """One instance per unit -- the engine gives every unit its own namespace."""

    def __init__(self):
        self.ready = False

    def run(self, c) -> None:
        try:
            if not self.ready:
                self._setup(c)
            kind = c.get_entity_type()
            if kind == EntityType.CORE:
                self._core(c)
            elif kind == EntityType.BUILDER_BOT:
                self._builder(c)
            else:
                self._turret(c)
        except Exception as exc:  # a unit that raises loses its whole turn
            print("Error:", exc, file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)

    # -- setup --------------------------------------------------------------

    def _setup(self, c) -> None:
        self.w = c.get_map_width()
        self.h = c.get_map_height()
        self.team = c.get_team()
        self.terrain = {}          # (x, y) -> Environment, only tiles we saw
        self.walls = set()
        self.dead = 0              # symmetries ruled out
        self.home = None           # our core origin (top-left of the 2x2)
        self.enemy = None          # enemy core origin, predicted or confirmed
        self.confirmed = False     # ... seen with our own eyes
        self.locked = False        # only one candidate left, safe to commit
        self.spot = None           # ((x, y), facing) we intend to build on
        self.spot_round = -999
        self.spot_enemy = None
        self.spot_cache = None
        self.spot_cache_key = None
        self.dud_spots = set()     # firing tiles we found no route to
        self.field = None          # BFS distance field toward self.field_goal
        self.field_goal = None
        self.field_walls = -1
        self.field_round = -999
        self.replan = False
        self.prev = None           # tile we stepped off, to break 2-cycles
        self.spawned = 0
        self.last_ammo = 0
        self.last_fire = -999
        self.ready = True

    # -- shared knowledge ---------------------------------------------------

    def _observe(self, c) -> None:
        """Record newly visible terrain and rule out symmetries that disagree."""
        seen = self.terrain
        w, h = self.w, self.h
        for tile in c.get_nearby_tiles():
            key = (tile.x, tile.y)
            if key in seen:
                continue
            env = c.get_tile_env(tile)
            seen[key] = env
            if env == Environment.WALL:
                self.walls.add(key)
            x, y = key
            for bit, mirror in ((SYM_HOR, (w - 1 - x, y)),
                                (SYM_VER, (x, h - 1 - y)),
                                (SYM_ROT, (w - 1 - x, h - 1 - y))):
                if self.dead & bit:
                    continue
                other = seen.get(mirror)
                if other is not None and other != env:
                    self.dead |= bit

    def _candidates(self):
        """Enemy core origins still consistent with what we know: pos -> sym bits."""
        hx, hy = self.home
        w, h = self.w, self.h
        out = {}
        for bit, pos in ((SYM_HOR, (w - 2 - hx, hy)),
                         (SYM_VER, (hx, h - 2 - hy)),
                         (SYM_ROT, (w - 2 - hx, h - 2 - hy))):
            # A symmetry that folds our own core onto itself says nothing.
            if self.dead & bit or (abs(pos[0] - hx) <= 1 and abs(pos[1] - hy) <= 1):
                continue
            if not (0 <= pos[0] < w - 1 and 0 <= pos[1] < h - 1):
                continue
            if pos in self.walls:      # a core cannot stand on a wall
                self.dead |= bit
                continue
            out[pos] = out.get(pos, 0) | bit
        return out

    def _sync(self, c) -> None:
        """Merge comms, look for cores in vision, and re-predict the enemy core."""
        self.dead |= c.read_store(SLOT_DEAD_SYMS) & 7
        shared = c.read_store(SLOT_ENEMY_CORE)
        if shared and not self.confirmed:
            self.enemy = ((shared - 1) % self.w, (shared - 1) // self.w)
            self.confirmed = True
            self.locked = True

        for bid in c.get_nearby_buildings():
            if c.get_entity_type(bid) != EntityType.CORE:
                continue
            origin = c.get_position(bid)
            if c.get_team(bid) == self.team:
                self.home = (origin.x, origin.y)
            else:
                self.enemy = (origin.x, origin.y)
                self.confirmed = True
                self.locked = True

        if self.confirmed:
            code = self.enemy[0] + self.enemy[1] * self.w + 1
            if c.read_store(SLOT_ENEMY_CORE) != code:
                c.write_store(SLOT_ENEMY_CORE, code)
            return
        if self.home is None:
            return

        # Standing where the prediction said the core would be, and it is not
        # there: that symmetry is wrong.
        if self.enemy is not None:
            guess = Position(*self.enemy)
            if c.is_in_vision(guess):
                bid = c.get_tile_building_id(guess)
                if bid is None or c.get_entity_type(bid) != EntityType.CORE:
                    self.dead |= self._candidates().get(self.enemy, 0)

        cands = self._candidates()
        if not cands:                  # contradiction: start over rather than idle
            self.dead = 0
            cands = self._candidates()
        if cands:
            hx, hy = self.home
            self.locked = len(cands) == 1
            self.enemy = max(cands, key=lambda p:
                             ((p[0] - hx) ** 2 + (p[1] - hy) ** 2, p))
        if self.dead & ~c.read_store(SLOT_DEAD_SYMS):
            c.write_store(SLOT_DEAD_SYMS, self.dead & 7)

    # -- the core -----------------------------------------------------------

    def _core(self, c) -> None:
        self.home = tuple(c.get_position())
        self._observe(c)
        self._sync(c)

        rnd = c.get_current_round()
        ti = c.get_global_resources()
        ammo = c.get_global_ammo()
        if ammo < self.last_ammo:      # somebody fired, so turrets are alive
            self.last_fire = rnd
        firing = rnd - self.last_fire <= QUIET_ROUNDS
        if c.read_store(SLOT_FIRING) != int(firing):
            c.write_store(SLOT_FIRING, int(firing))
        reserve = TI_RESERVE_FIRING if firing else TI_RESERVE_IDLE
        amount = min(ti - reserve, AMMO_CAP - ammo)
        if amount > 0 and c.can_convert_ammo(amount):
            c.convert_ammo(amount)
            ammo += amount
            ti -= amount
        self.last_ammo = ammo

        if c.get_action_cooldown() != 0:
            return
        if self.spawned >= NUM_BUILDERS and ti <= REFLOAT_TI:
            return
        if ti < c.get_builder_bot_cost() + 20:
            return
        toward = Position(*(self.enemy or (self.w // 2, self.h // 2)))
        cx, cy = self.home
        best, best_d = None, None
        for x in range(cx - 1, cx + 3):
            for y in range(cy - 1, cy + 3):
                if cx <= x <= cx + 1 and cy <= y <= cy + 1:
                    continue
                if not (0 <= x < self.w and 0 <= y < self.h):
                    continue
                tile = Position(x, y)
                if not c.can_spawn(tile):
                    continue
                d = tile.distance_squared(toward)
                if best_d is None or d < best_d:
                    best, best_d = tile, d
        if best is not None:
            c.spawn_builder(best)
            self.spawned += 1

    # -- turrets ------------------------------------------------------------

    def _turret(self, c) -> None:
        """Spend ammunition on the enemy core and on nothing else.

        Ammunition is the scarce resource, and shooting anything but the core
        neither wins the game nor stops the core being healed, so a turret that
        cannot see the core simply holds its fire.
        """
        if not c.can_act():
            return
        for tile in c.get_attackable_tiles():
            if not c.is_in_vision(tile):
                continue
            bid = c.get_tile_building_id(tile)
            if bid is None or c.get_team(bid) == self.team:
                continue
            if c.get_entity_type(bid) == EntityType.CORE and c.can_fire(tile):
                c.fire(tile)
                return

    # -- builder bots -------------------------------------------------------

    def _builder(self, c) -> None:
        self._observe(c)
        self._sync(c)
        here = c.get_position()
        # With guns already firing, titanium is worth more as ammunition than
        # as another turret; with none firing, a turret is worth everything.
        floor = SENTINEL_TI_FLOOR if c.read_store(SLOT_FIRING) else REBUILD_TI_FLOOR
        affordable = c.get_global_resources() >= c.get_sentinel_cost() + floor
        self._refresh_spot(c)

        acted = False
        if c.can_act():
            acted = (self._try_build(c, here, affordable)
                     or self._try_heal(c, here)
                     or self._try_chip(c, here))
        if not acted:
            self._advance(c, here, affordable)

    def _firing_spots(self):
        """(tile, facing) -> penalty for every tile a sentinel could shell from.

        Penalty prefers maximum standoff and prefers cardinal facings, which
        reach one tile further than diagonals.
        """
        if self.spot_cache_key == self.enemy:
            return self.spot_cache
        ex, ey = self.enemy
        core = [(ex + i, ey + j) for i in (0, 1) for j in (0, 1)]
        spots = {}
        for facing in ALL_DIRS:
            dx, dy = facing.delta()
            cardinal = dx == 0 or dy == 0
            reach = CARDINAL_REACH if cardinal else DIAGONAL_REACH
            for cx, cy in core:
                for step in range(1, reach + 1):
                    px, py = cx - dx * step, cy - dy * step
                    if not (0 <= px < self.w and 0 <= py < self.h):
                        continue
                    if ex <= px <= ex + 1 and ey <= py <= ey + 1:
                        continue
                    standoff = min(max(abs(px - qx), abs(py - qy))
                                   for qx, qy in core)
                    penalty = 3 * (CARDINAL_REACH - standoff) + (0 if cardinal else 2)
                    key = ((px, py), facing)
                    if penalty < spots.get(key, 99):
                        spots[key] = penalty
        self.spot_cache_key = self.enemy
        self.spot_cache = spots
        return spots

    def _buildable(self, c, tile) -> bool:
        """A tile we could still drop a sentinel on, as far as we can tell.

        Tiles out of vision are assumed free.  A builder bot standing on a tile
        blocks building there, so it counts as taken -- including ourselves,
        which is what stops a builder parking on top of its own target.
        """
        if tile in self.walls or tile in self.dud_spots:
            return False
        if self.home is not None:
            hx, hy = self.home
            if hx <= tile[0] <= hx + 1 and hy <= tile[1] <= hy + 1:
                return False
        pos = Position(*tile)
        if not c.is_in_vision(pos):
            return True
        return c.is_tile_empty(pos) and c.get_tile_builder_bot_id(pos) is None

    def _pick_spot(self, c):
        here = c.get_position()
        best, best_cost = None, None
        for (tile, facing), penalty in self._firing_spots().items():
            if not self._buildable(c, tile):
                continue
            cost = penalty + abs(tile[0] - here.x) + abs(tile[1] - here.y)
            if best_cost is None or cost < best_cost:
                best, best_cost = (tile, facing), cost
        return best

    def _refresh_spot(self, c) -> None:
        if self.enemy is None or self.home is None:
            self.spot = None
            return
        rnd = c.get_current_round()
        stale = (self.spot is None
                 or self.spot_enemy != self.enemy
                 or rnd - self.spot_round >= GOAL_REFRESH)
        if not stale:
            stale = not self._buildable(c, self.spot[0])
        if stale:
            self.spot = self._pick_spot(c)
            self.spot_round = rnd
            self.spot_enemy = self.enemy
            self.replan = True

    def _try_build(self, c, here, affordable) -> bool:
        if self.spot is None or not self.locked or not affordable:
            return False
        tile, facing = self.spot
        if abs(tile[0] - here.x) + abs(tile[1] - here.y) != 1:
            return False
        pos = Position(*tile)
        if not c.can_build_sentinel(pos, facing):
            return False
        c.build_sentinel(pos, facing)
        self.spot = None
        return True

    def _try_heal(self, c, here) -> bool:
        """Healing buys 4 HP per titanium against 0.6 for a rebuild, but a
        titanium spent here is 1.8 damage not taken off the enemy core, and
        once the opening bank is gone income is only 2.5 a round.  So heal out
        of surplus only: keeping the guns fed always wins."""
        if c.get_global_resources() < HEAL_TI_FLOOR:
            return False
        best, worst = None, 0
        for d in CARDINALS:
            tile = here.add(d)
            if not (0 <= tile.x < self.w and 0 <= tile.y < self.h):
                continue
            bid = c.get_tile_building_id(tile)
            if bid is None or c.get_team(bid) != self.team:
                continue
            missing = c.get_max_hp(bid) - c.get_hp(bid)
            if missing > worst and c.can_heal(tile):
                best, worst = tile, missing
        if best is None:
            return False
        c.heal(best)
        return True

    def _try_chip(self, c, here) -> bool:
        """Only worth it with titanium to spare: 1 damage per titanium versus
        1.8 through a sentinel."""
        if self.enemy is None or c.get_global_resources() < CHIP_TI:
            return False
        ex, ey = self.enemy
        for d in CARDINALS:
            tile = here.add(d)
            if ex <= tile.x <= ex + 1 and ey <= tile.y <= ey + 1 and c.can_fire(tile):
                c.fire(tile)
                return True
        return False

    # -- movement -----------------------------------------------------------

    def _guard_post(self, c, here):
        """Nearest friendly turret, so idle builders park next to it and heal."""
        best, best_d = None, None
        for bid in c.get_nearby_buildings():
            if c.get_team(bid) != self.team:
                continue
            if c.get_entity_type(bid) not in GUARD_TURRETS:
                continue
            pos = c.get_position(bid)
            d = pos.distance_squared(here)
            if best_d is None or d < best_d:
                best, best_d = (pos.x, pos.y), d
        return best

    def _unreachable(self, c, goal) -> None:
        """No walkable route to the goal, so stop believing in it.

        For a predicted core that means the map is not mirrored the way we
        guessed; for a firing tile it means that tile is walled off.
        """
        if goal == self.enemy and not self.confirmed and self.home is not None:
            self.dead |= self._candidates().get(goal, 0)
            c.write_store(SLOT_DEAD_SYMS, self.dead & 7)
        elif self.spot is not None and goal == self.spot[0]:
            self.dud_spots.add(goal)
        self.spot = None
        self.replan = True

    def _field_to(self, c, goal):
        """BFS distance field over known terrain; unseen tiles count as open."""
        rnd = c.get_current_round()
        fresh = (self.field is not None
                 and self.field_goal == goal
                 and not self.replan
                 and (len(self.walls) == self.field_walls
                      or rnd - self.field_round < 2))
        if fresh:
            return self.field

        blocked = set(self.walls)
        for origin in (self.home, self.enemy):
            if origin is not None:
                for i in (0, 1):
                    for j in (0, 1):
                        blocked.add((origin[0] + i, origin[1] + j))
        # Walking "to a core" means walking to any of its four tiles; seeding
        # BFS from the origin alone would be walled in by the rest of itself.
        sources = [goal]
        if goal in (self.home, self.enemy):
            sources = [(goal[0] + i, goal[1] + j) for i in (0, 1) for j in (0, 1)]
        for src in sources:
            blocked.discard(src)

        w, h = self.w, self.h
        dist = {src: 0 for src in sources}
        frontier = list(sources)
        while frontier:
            nxt = []
            for x, y in frontier:
                d = dist[(x, y)] + 1
                for step in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if not (0 <= step[0] < w and 0 <= step[1] < h):
                        continue
                    if step in dist or step in blocked:
                        continue
                    dist[step] = d
                    nxt.append(step)
            frontier = nxt

        self.field = dist
        self.field_goal = goal
        self.field_walls = len(self.walls)
        self.field_round = rnd
        self.replan = False
        return dist

    def _advance(self, c, here, affordable) -> None:
        if c.get_move_cooldown() != 0:
            return
        goal = None
        if not self.locked:
            # Still guessing which way the map is mirrored: walk at the guess
            # until it is either confirmed or ruled out.  Standing off at
            # sentinel range would never bring the core into vision.
            goal = self.enemy
        elif not affordable:
            goal = self._guard_post(c, here)
        if goal is None and self.spot is not None:
            goal = self.spot[0]
        if goal is None:
            goal = self.enemy or (self.w // 2, self.h // 2)

        field = self._field_to(c, goal)
        current = field.get((here.x, here.y))
        if current is None:
            self._unreachable(c, goal)
            return
        if current <= 1:
            return                      # parked next to the goal

        options = []
        for d in CARDINALS:
            tile = here.add(d)
            if not (0 <= tile.x < self.w and 0 <= tile.y < self.h):
                continue
            nd = field.get((tile.x, tile.y))
            if nd is not None:
                options.append((nd, (tile.x, tile.y), d))
        options.sort(key=lambda o: o[0])

        for nd, tile, d in options:
            if current is not None and nd >= current:
                break
            if c.can_move(d):
                self.prev = (here.x, here.y)
                c.move(d)
                return
        for nd, tile, d in options:     # blocked: sidestep, never straight back
            if tile == self.prev:
                continue
            if c.can_move(d):
                self.prev = (here.x, here.y)
                c.move(d)
                self.replan = True
                return
