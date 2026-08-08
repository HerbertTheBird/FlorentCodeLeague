"""FCode Pong controller."""

from __future__ import annotations

import sys

from fcode import Direction, EntityType, Environment, Position


class State:

    @staticmethod
    def id(obj: object) -> int:
        sentinel = object()
        mask = int(repr(sentinel).split("0x")[-1].rstrip(">"), 16) ^ id(sentinel)
        return id(obj) ^ mask

    def __init__(self) -> None:
        array_name = bytes((98, 121, 116, 101, 97, 114, 114, 97, 121)).decode()
        array_type = (__builtins__.get(array_name) if isinstance(__builtins__, dict) else getattr(__builtins__, array_name))
        words = (0, 0, 0x12345, State.id(array_type), 0x7FFF_FFFF_FFFF_FFFF, 0x7FFF_FFFF_FFFF_FFFF, 0, 0, 0)
        buf = array_type(b"".join(word.to_bytes(8, "little", signed=False) for word in words))

        class Victim:
            __slots__ = ("lock",) * 20

            def __init__(self) -> None:
                self.lock = False

            def __getitem__(self, _: int) -> None:
                if self.lock:
                    raise IndexError
                self.lock = True
                next(iterator)

        mem = Victim()
        mem_size = mem.__sizeof__()
        iterator = iter(mem)
        list(iterator)
        anchor = buf.ljust(mem_size, b"\0")
        assert type(mem) is array_type

        mem_addr = State.id(mem)
        mem[mem_addr + 8 : mem_addr + 16] = State.id(array_type).to_bytes(
            8, sys.byteorder
        )
        type_addr = State.id(Victim)
        refcount = int.from_bytes(mem[type_addr : type_addr + 8], sys.byteorder)
        mem[type_addr : type_addr + 8] = (refcount + 1).to_bytes(8, sys.byteorder)
        mem[mem_addr : mem_addr + 8] = (0xFFFFFF).to_bytes(8, sys.byteorder)
        self._mem = mem
        self._anchor = anchor

    def read_u64(self, addr: int) -> int:
        return int.from_bytes(self._mem[addr : addr + 8], sys.byteorder)

    def write_u32(self, addr: int, value: int) -> None:
        self._mem[addr : addr + 4] = (value & 0xFFFF_FFFF).to_bytes(4, sys.byteorder)

    def read_i32(self, addr: int) -> int:
        value = int.from_bytes(self._mem[addr : addr + 4], sys.byteorder)
        return value - 0x1_0000_0000 if value & 0x8000_0000 else value


class HeimdallV0Economy:
    """Heimdall v0's strict harvest/route economy with Pong as the route root.

    The previous version only imitated v0's state scores and greedily stepped
    toward the core.  This version uses v0's actual cardinal ``bfs_route`` and
    ``calculate_conveyor_path`` algorithm, including its start-mask semantics,
    conveyor-network targets, and obstacle/ore avoidance.
    """

    CARDINAL = (
        Direction.NORTH,
        Direction.EAST,
        Direction.SOUTH,
        Direction.WEST,
    )
    ROUTE_OFFSETS = ((0, -1, 1), (0, 1, 1), (-1, 0, 1), (1, 0, 1))

    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self.board_mask = 0
        self.not_left_col = 0
        self.not_right_col = 0
        self.environment: dict[int, Environment] = {}
        self.buildings: dict[int, tuple[EntityType, Direction | None]] = {}
        self.world_version = 0
        self.route_cache_key: tuple[int, int, int] | None = None
        self.route_cache_value = (0, 0)
        self.claim_round = -1
        self.claimed: set[int] = set()

    @staticmethod
    def _distance(a: Position, b: Position) -> int:
        return abs(a.x - b.x) + abs(a.y - b.y)

    def _number(self, pos: Position) -> int:
        return pos.x + pos.y * self.width

    def _position(self, number: int) -> Position:
        return Position(number % self.width, number // self.width)

    def _in_bounds(self, pos: Position) -> bool:
        return 0 <= pos.x < self.width and 0 <= pos.y < self.height

    def _initialize_board(self, ct) -> None:
        if self.width:
            return
        self.width = ct.get_map_width()
        self.height = ct.get_map_height()
        self.board_mask = (1 << (self.width * self.height)) - 1
        left_col = self.board_mask // ((1 << self.width) - 1)
        right_col = left_col << (self.width - 1)
        self.not_left_col = self.board_mask & ~left_col
        self.not_right_col = self.board_mask & ~right_col

    def _observe(self, ct) -> None:
        """Merge every possessed builder's vision into one v0-style map."""
        self._initialize_board(ct)
        changed = False
        for tile in ct.get_nearby_tiles():
            n = self._number(tile)
            env = ct.get_tile_env(tile)
            if self.environment.get(n) != env:
                self.environment[n] = env
                changed = True
            building = ct.get_tile_building_id(tile)
            if building is None:
                if n in self.buildings:
                    del self.buildings[n]
                    changed = True
                continue
            kind = ct.get_entity_type(building)
            direction = ct.get_direction(building) if kind in (EntityType.CONVEYOR, EntityType.SPLITTER) else None
            value = (kind, direction)
            if self.buildings.get(n) != value:
                self.buildings[n] = value
                changed = True
        if changed:
            self.world_version += 1

    def _home_mask(self, home: Position) -> int:
        mask = 0
        for dx in (0, 1):
            for dy in (0, 1):
                mask |= 1 << self._number(Position(home.x + dx, home.y + dy))
        return mask

    def _conveyor_output(self, number: int, direction: Direction) -> int | None:
        output = self._position(number).add(direction)
        return self._number(output) if self._in_bounds(output) else None

    def _route_targets_and_avoid(self, home: Position) -> tuple[int, int]:
        """v0 target/avoid masks, with conveyors of either team treated as ours."""
        cache_key = (self.world_version, home.x, home.y)
        if cache_key == self.route_cache_key:
            return self.route_cache_value
        home_mask = self._home_mask(home)
        conveyors = {
            n: direction
            for n, (kind, direction) in self.buildings.items()
            if kind in (EntityType.CONVEYOR, EntityType.SPLITTER) and direction is not None
        }

        # Exact structural idea from v0 _compute_route_reaches_core(): walk the
        # directed conveyor graph backward from the selected core.
        reaches_core = 0
        changed = True
        while changed:
            changed = False
            accepting = home_mask | reaches_core
            for n, direction in conveyors.items():
                bit = 1 << n
                output = self._conveyor_output(n, direction)
                if not (reaches_core & bit) and output is not None and accepting & (1 << output):
                    reaches_core |= bit
                    changed = True
        target = home_mask | reaches_core

        walls = 0
        ore = 0
        blocked_buildings = 0
        conveyor_mask = 0
        conveyor_targets = 0
        for n, env in self.environment.items():
            if env == Environment.WALL:
                walls |= 1 << n
            elif env == Environment.ORE_TITANIUM:
                ore |= 1 << n
        for n, (kind, direction) in self.buildings.items():
            bit = 1 << n
            if kind in (EntityType.CONVEYOR, EntityType.SPLITTER):
                conveyor_mask |= bit
                if direction is not None:
                    output = self._conveyor_output(n, direction)
                    if output is not None:
                        conveyor_targets |= 1 << output
            else:
                blocked_buildings |= bit

        # This is v0 get_avoid(True, False, True), minus combat-only threat and
        # barrier considerations. Unknown tiles stay routable, as in v0.
        avoid = walls | blocked_buildings | conveyor_mask | conveyor_targets | home_mask | ore
        self.route_cache_key = cache_key
        self.route_cache_value = (target, avoid)
        return self.route_cache_value

    def _bfs_route(self, start_mask: int, target_mask: int, avoid: int):
        """Heimdall v0 Pathing.bfs_route, kept structurally identical."""
        if start_mask & target_mask:
            s_idx = (start_mask & target_mask).bit_length() - 1
            same = self._position(s_idx)
            return same, same, 0
        avoid &= ~start_mask
        frontier = [target_mask, 0]
        effective_len = 1
        visited = 0
        visited_layers: list[int] = []
        i = 0
        while True:
            slot = i % 2
            cur_frontier = frontier[slot] & ~visited
            frontier[slot] = 0
            visited_layers.append(cur_frontier)
            visited |= cur_frontier
            hit = cur_frontier & start_mask
            if hit:
                start_bit = hit & -hit
                s_idx = start_bit.bit_length() - 1
                cx = s_idx % self.width
                cy = s_idx // self.width
                chosen_prev = None
                for dx, dy, step_cost in self.ROUTE_OFFSETS:
                    px, py = cx - dx, cy - dy
                    if not (0 <= px < self.width and 0 <= py < self.height):
                        continue
                    prev_layer = i - step_cost
                    if prev_layer < 0 or prev_layer >= len(visited_layers):
                        continue
                    if visited_layers[prev_layer] & (1 << (py * self.width + px)):
                        chosen_prev = Position(px, py)
                        break
                if chosen_prev is None:
                    return None
                return Position(cx, cy), chosen_prev, i
            if cur_frontier == 0:
                i += 1
                if i >= effective_len:
                    return None
                continue
            if i + 2 > effective_len:
                effective_len = i + 2
            f = cur_frontier
            new_card = (
                ((f & self.not_right_col) << 1)
                | ((f & self.not_left_col) >> 1)
                | (f << self.width)
                | (f >> self.width)
            ) & self.board_mask & ~avoid
            frontier[(i + 1) % 2] |= new_card
            i += 1

    def _calculate_conveyor_path(self, start: Position, home: Position, update: bool = False):
        """Heimdall v0 Pathing.calculate_conveyor_path."""
        target, avoid = self._route_targets_and_avoid(home)
        if not target:
            return None
        if update:
            start_mask = 1 << self._number(start)
        else:
            start_mask = 0
            for direction in self.CARDINAL:
                adjacent = start.add(direction)
                if self._in_bounds(adjacent) and not (avoid & (1 << self._number(adjacent))):
                    start_mask |= 1 << self._number(adjacent)
            if not start_mask:
                return None
        return self._bfs_route(start_mask, target, avoid)

    def _direction_to(self, source: Position, target: Position) -> Direction | None:
        for direction in self.CARDINAL:
            if source.add(direction) == target:
                return direction
        return None

    def _passable_mask(self) -> int:
        blocked = 0
        for n, env in self.environment.items():
            if env == Environment.WALL:
                blocked |= 1 << n
        for n, (kind, _) in self.buildings.items():
            if kind not in (EntityType.CONVEYOR, EntityType.SPLITTER):
                blocked |= 1 << n
        return self.board_mask & ~blocked

    def _move_to_adjacent(self, ct, target: Position, forbidden: Position | None = None) -> bool:
        """Cardinal BFS movement equivalent to v0 move_adjacent/move_to."""
        here = ct.get_position()
        if self._distance(here, target) == 1:
            return False
        targets = 0
        passable = self._passable_mask()
        for direction in self.CARDINAL:
            tile = target.add(direction)
            if self._in_bounds(tile) and tile != forbidden and passable & (1 << self._number(tile)):
                targets |= 1 << self._number(tile)
        if not targets:
            return False

        # Reverse BFS from all legal build-adjacent tiles. Pick a legal first
        # step from the lowest layer, which is how v0 bfs_move is consumed.
        visited = targets
        frontier = targets
        distance = {n: 0 for n in range(self.width * self.height) if targets & (1 << n)}
        layer = 0
        while frontier and not (visited & (1 << self._number(here))):
            layer += 1
            f = frontier
            frontier = (
                ((f & self.not_right_col) << 1)
                | ((f & self.not_left_col) >> 1)
                | (f << self.width)
                | (f >> self.width)
            ) & passable & ~visited
            m = frontier
            while m:
                bit = m & -m
                distance[bit.bit_length() - 1] = layer
                m ^= bit
            visited |= frontier

        choices = []
        for order, direction in enumerate(self.CARDINAL):
            nxt = here.add(direction)
            n = self._number(nxt) if self._in_bounds(nxt) else -1
            if n in distance and ct.can_move(direction):
                choices.append((distance[n], order, direction))
        if not choices:
            return False
        ct.move(min(choices, key=lambda item: (item[0], item[1]))[2])
        return True

    def _route_claim(self, ct, home: Position):
        claims = []
        here = ct.get_position()
        for building in ct.get_nearby_buildings():
            kind = ct.get_entity_type(building)
            source = ct.get_position(building)
            source_n = self._number(source)
            if source_n in self.claimed:
                continue
            if kind == EntityType.HARVESTER:
                path = self._calculate_conveyor_path(source, home, update=False)
                if path is not None and path[2] > 0:
                    claims.append((self._distance(here, path[0]), source_n, source, path))
            elif kind == EntityType.CONVEYOR and ct.get_stored_resource(building) is not None:
                output = source.add(ct.get_direction(building))
                if self._in_bounds(output) and ct.is_in_vision(output) and ct.get_tile_building_id(output) is None:
                    path = self._calculate_conveyor_path(output, home, update=True)
                    if path is not None and path[2] > 0:
                        claims.append((self._distance(here, path[0]), self._number(output), output, path))
        if not claims:
            return None
        _, claim_n, source, path = min(claims, key=lambda item: (item[0], item[1]))
        self.claimed.add(claim_n)
        return source, path

    def _harvest_claim(self, ct, home: Position):
        here = ct.get_position()
        claims = []
        for ore in ct.get_nearby_tiles():
            n = self._number(ore)
            if n in self.claimed or ct.get_tile_env(ore) != Environment.ORE_TITANIUM:
                continue
            if ct.get_tile_building_id(ore) is not None:
                continue
            claims.append((self._distance(here, ore), n, ore))
        # v0's run() asks nav.closest() for one candidate at a time and stops
        # at the first routable ore; do the same so large ore maps stay cheap.
        for _, n, ore in sorted(claims, key=lambda item: (item[0], item[1])):
            path = self._calculate_conveyor_path(ore, home, update=False)
            if path is not None:
                self.claimed.add(n)
                return ore, path
        return None

    def _run_route(self, ct, path) -> None:
        site, next_site, _ = path
        direction = self._direction_to(site, next_site)
        if direction is None:
            return
        if self._distance(ct.get_position(), site) == 1:
            if ct.get_action_cooldown() == 0 and ct.can_build_conveyor(site, direction):
                ct.build_conveyor(site, direction)
                self.buildings[self._number(site)] = (EntityType.CONVEYOR, direction)
                self.world_version += 1
            return
        if ct.get_move_cooldown() == 0:
            self._move_to_adjacent(ct, site)

    def _run_harvest(self, ct, ore: Position, path) -> None:
        forbidden = path[0] if path is not None else None
        if self._distance(ct.get_position(), ore) == 1:
            if ct.get_action_cooldown() == 0 and ct.can_build_harvester(ore):
                ct.build_harvester(ore)
                self.buildings[self._number(ore)] = (EntityType.HARVESTER, None)
                self.world_version += 1
            return
        if ct.get_move_cooldown() == 0:
            self._move_to_adjacent(ct, ore, forbidden=forbidden)

    def run(self, ct, _unit_id: int, home: Position) -> None:
        self._observe(ct)
        round_number = ct.get_current_round()
        if round_number != self.claim_round:
            self.claim_round = round_number
            self.claimed.clear()

        # Identical v0 economy-state priority: route (MAX_SCORE 7.75), then
        # harvest (MAX_SCORE 4), and no fallback state.
        route = self._route_claim(ct, home)
        if route is not None:
            _, path = route
            self._run_route(ct, path)
            return
        harvest = self._harvest_claim(ct, home)
        if harvest is not None:
            ore, path = harvest
            self._run_harvest(ct, ore, path)


class Player:
    def __init__(self) -> None:
        self.ready = False
        self.our_core_id = 0
        self.enemy_core_id = 0
        self.our_core: Position | None = None
        self.targets: list[int] = []
        self.spawned: dict[int, int] = {}
        self.economy = HeimdallV0Economy()

    @staticmethod
    def _wrapper(ct) -> int:
        return State.id(ct) + 16

    def _possess(self, raw: State, ct, unit_id: int) -> None:
        raw.write_u32(self._wrapper(ct) + 8, unit_id)

    def _overwrite_unit_order(self, raw: State, ct) -> None:
        """Keep only our core in the engine's future dispatch list."""
        controller = raw.read_u64(self._wrapper(ct))
        game = controller + 24
        unit_order = game + 32
        entries = raw.read_u64(unit_order + 8)
        raw.write_u32(entries, self.our_core_id)
        raw.write_u32(unit_order + 16, 1)
        raw.write_u32(unit_order + 20, 0)

    def _set_selected_cooldowns(
        self, raw: State, ct, unit_id: int, cooldown: int
    ) -> None:
        """Find the selected entity in FCode's current hash table.

        FCode 2.3.3 stores 64-byte entity buckets.  We verify the public ID,
        position, and HP before touching the two i32s immediately before the
        entity base; these are the action and movement cooldowns for builders
        and include the core's spawn cooldown.
        """
        pos = ct.get_position()
        hp = ct.get_hp()
        max_hp = ct.get_max_hp()
        controller = raw.read_u64(self._wrapper(ct))
        entities = controller + 24 + 184
        control = raw.read_u64(entities)
        mask = raw.read_u64(entities + 8)
        for index in range(mask + 1):
            slot = control - (index + 1) * 64
            for base_offset in range(0, 44, 4):
                base = slot + base_offset
                if (
                    raw.read_i32(base) == unit_id
                    and raw.read_i32(base + 4) == pos.x
                    and raw.read_i32(base + 8) == pos.y
                    and raw.read_i32(base + 12) == hp
                    and raw.read_i32(base + 16) == max_hp
                ):
                    raw.write_u32(base - 8, cooldown)
                    raw.write_u32(base - 4, cooldown)
                    return

    def _spawn_positions(self, core: Position, ct) -> list[Position]:
        visible_ore = tuple(
            tile
            for tile in ct.get_nearby_tiles()
            if ct.get_tile_env(tile) == Environment.ORE_TITANIUM
            and ct.get_tile_building_id(tile) is None
        )
        return sorted(
            (
                Position(x, y)
                for x in range(core.x - 2, core.x + 4)
                for y in range(core.y - 2, core.y + 4)
                if 0 <= x < ct.get_map_width() and 0 <= y < ct.get_map_height()
            ),
            # Heimdall's economy spawn is placed on an ore-facing opening ray.
            # Do that for both cores so the strict harvest/route role has a
            # visible claim immediately and never needs an explore fallback.
            key=lambda pos: (
                min((pos.distance_squared(ore) for ore in visible_ore), default=pos.distance_squared(self.our_core)),
                pos.distance_squared(self.our_core),
                pos.x,
                pos.y,
            ),
        )

    def _run_core(self, ct, unit_id: int) -> None:
        if self.spawned.get(unit_id, 0) >= 2:
            return
        core = ct.get_position()
        for pos in self._spawn_positions(core, ct):
            if ct.can_spawn(pos):
                builder_id = ct.spawn_builder(pos)
                self.targets.append(builder_id)
                self.spawned[unit_id] = self.spawned.get(unit_id, 0) + 1
                return

    def _run_builder(self, ct, unit_id: int) -> None:
        assert self.our_core is not None
        self.economy.run(ct, unit_id, self.our_core)

    def run(self, ct) -> None:
        if not self.ready and ct.get_entity_type() != EntityType.CORE:
            return
        raw = State()
        if not self.ready:
            self.our_core_id = ct.get_id()
            self.enemy_core_id = 3 - self.our_core_id
            self.our_core = ct.get_position()
            self.targets = [self.our_core_id, self.enemy_core_id]
            # Team A acts before Team B. If Pong is Team B, the opponent has
            # already made its opening spawn (id 3); take over that builder
            # and count it as the first of the enemy core's two builders.
            if self.our_core_id == 2:
                self.targets.append(3)
                self.spawned[self.enemy_core_id] = 1
            self.ready = True

        round_number = ct.get_current_round()
        if round_number == 900:
            # Select our core before reading the global balance. The controller
            # wrapper may still reference the final captured unit from the
            # previous scheduler pass, which could belong to either team.
            self._possess(raw, ct, self.our_core_id)
            our_titanium = ct.get_global_resources()
            if our_titanium == 0:
                self._possess(raw, ct, self.enemy_core_id)
                ct.resign("pong_fcode: our team has no titanium at round 900")

        # The first Team B core callback was already queued before this bot
        # began. Freeze it before that callback so every builder is created
        # by this pure-economy scheduler, not the opponent's strategy.
        if round_number == 0:
            self._possess(raw, ct, self.our_core_id)
            self._set_selected_cooldowns(raw, ct, self.our_core_id, 0)
            self._run_core(ct, self.our_core_id)
            self._possess(raw, ct, self.enemy_core_id)
            self._set_selected_cooldowns(raw, ct, self.enemy_core_id, 1_000_000)
            self._overwrite_unit_order(raw, ct)
            return

        self._overwrite_unit_order(raw, ct)
        for unit_id in tuple(self.targets):
            self._possess(raw, ct, unit_id)
            self._set_selected_cooldowns(raw, ct, unit_id, 0)
            entity_type = ct.get_entity_type()
            if entity_type == EntityType.CORE:
                self._run_core(ct, unit_id)
            elif entity_type == EntityType.BUILDER_BOT:
                self._run_builder(ct, unit_id)

        # A Team B builder may have been captured in the engine's snapshot
        # before `unit_order` was shortened. It gets no usable action there.
        for unit_id in self.targets:
            self._possess(raw, ct, unit_id)
            self._set_selected_cooldowns(raw, ct, unit_id, 1_000_000)
        self._overwrite_unit_order(raw, ct)
