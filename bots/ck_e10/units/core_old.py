from main import has_op
from fcode import Controller, Direction, Position, EntityType
import map_info
from log import log
from units.spawn_plan import choose_spawn_plan, draw_spawn_plan, INITIAL_SPAWN_COUNT, INITIAL_EXPLORE_MAX_STEPS
import units.defense as defense
import comms
import pathing
from pathing import Pathing
import sys
rc: Controller

# --- Configurable ---
SCALE_MULT = 1
DEFENSE_FRIENDLY_RADIUS_SQ = 36
# Rounds to wait after dispatching a blocker before dispatching another. A
# defender that had to walk to its block tile is in flight for several rounds
# and the alarm stays up the whole time; without this the core would spawn one
# bot per round at the same threat.
DISPATCH_COOLDOWN = 5
# Manhattan reach, measured from the core footprint, within which a defender that
# has to walk to its block tile can still get there before the tile has drifted
# out of range. Beyond it we only dispatch when the sentry can throw.
WALK_DISPATCH_RANGE = 5
# Titanium kept back when emergency-spawning under siege, so the very last of the
# bank is still available for the barriers and attacks the responders need.
SIEGE_SPAWN_FLOOR = 60
# Unit-count ceiling for emergency spawns, well under the 50-unit cap.
SIEGE_MAX_UNITS = 20
# Titanium kept free beyond a builder's own cost before spawning another one.
# Builders are the engine of the whole economy — in won games we finish with
# ~16 units and 39 buildings, in lost ones with ~9 and 23 — so this buffer wants
# to be small enough to keep hiring and large enough that hiring never starves
# conveyor and harvester construction.
ECON_BUFFER = 150
ECON_BUFFER_MANY = 350
# On 500 starting titanium this buffer keeps clearing, so the opening runs to
# about 7 builders costing ~336 Ti to builder-cost scaling before the first
# harvester. That looks obviously wasteful and is not: gating extra spawns on
# having an economy was measured at two strengths and both lost, monotonically
# in the direction of *fewer* builders being worse.
#
#     bootstrap buffer   none(=150, ~7 bots)   220 (~6 bots)   320 (~4 bots)
#     full suite               62.5%               59.8%           57.6%
#
# loki alone prefers the lean opening (71.2% at 320 against 62.1% now), so it is
# not that early builders are useless — it is that against most opponents the
# extra bodies out-earn the scaling they cost. Leave it alone without new
# evidence.

_spawn_plan: list[Direction] | None = None
_num_spawned = 0
_spawn_tiles: tuple[Position, ...] = ()   # tiles immediately surrounding the 2x2 core
_last_dispatch_round = -DISPATCH_COOLDOWN
_last_core_hp: int | None = None   # core HP last round, to detect real damage
nav: Pathing = None


def _compute_spawn_tiles() -> tuple[Position, ...]:
    """Tiles immediately surrounding the core's 2x2 footprint (never on the core
    itself). These are the only legal builder-spawn positions."""
    core = map_info._my_core
    if core is None:
        return ()
    w = map_info._width
    h = map_info._height
    core_tiles = {(core.x + dx, core.y + dy) for dx in (0, 1) for dy in (0, 1)}
    ring: set[tuple[int, int]] = set()
    for tx, ty in core_tiles:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nx, ny = tx + dx, ty + dy
                if (nx, ny) in core_tiles:
                    continue
                if 0 <= nx < w and 0 <= ny < h:
                    ring.add((nx, ny))
    return tuple(Position(x, y) for x, y in ring)


def init(c: Controller):
    global rc, nav
    rc = c
    nav = Pathing(c)


def _spawn_best_toward(target: Position) -> bool:
    """Spawn a builder on the surrounding tile closest to `target`. Returns True
    if a builder was spawned."""
    best = None
    best_d = None
    for p in _spawn_tiles:
        if rc.can_spawn(p):
            d = p.distance_squared(target)
            if best_d is None or d < best_d:
                best_d = d
                best = p
    if best is None:
        return False
    rc.spawn_builder(best)
    return True


def _spawn_toward_plan(core_pos: Position) -> bool:
    global _num_spawned
    if _spawn_plan is None or _num_spawned >= len(_spawn_plan):
        return False

    planned_dir = _spawn_plan[_num_spawned]
    dx, dy = map_info._DIRECTION_DELTAS[planned_dir]
    # Aim well past the footprint so the closest surrounding tile is the one best
    # aligned with the planned direction.
    target = Position(core_pos.x + dx * 8, core_pos.y + dy * 8)
    if _spawn_best_toward(target):
        _num_spawned += 1
        return True
    return False


def _spawn_toward_center() -> bool:
    """Spawn on the surrounding tile closest to map center."""
    center = Position(map_info._width // 2, map_info._height // 2)
    return _spawn_best_toward(center)


def _threat_to_answer(alarm) -> tuple[Position, Position] | None:
    """(enemy, block tile) the core should dispatch a blocker to, or None.

    Two independent sources feed this. The sentry launcher's alarm reaches
    further up the approach lane than the core can see, because the sentry sits
    two tiles out toward the enemy. The core's own vision (r² 36) covers threats
    the sentry has no line on — and works before the sentry has even been built.
    Whichever fires, we only answer threats nothing of ours is already blocking.
    """
    if alarm is not None and alarm[1] is not None:
        enemy = alarm[1]
        block = defense.block_tile(enemy)
        if block is not None and not defense.is_blocked(enemy, block):
            return enemy, block
    for _d2, _uid, enemy in defense.threatening_enemies():
        block = defense.block_tile(enemy)
        if block is not None and not defense.is_blocked(enemy, block):
            return enemy, block
    return None


def _builder_in_pickup_range(sentry: Position) -> bool:
    """Is one of my builders already standing where the sentry could pick it up?"""
    bit_mask = map_info._bm_friendly_bots
    w = map_info._width
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            x, y = sentry.x + dx, sentry.y + dy
            if 0 <= x < w and 0 <= y < map_info._height and bit_mask & (1 << (x + y * w)):
                return True
    return False


def _blocker_count() -> int:
    return sum(1 for _d2, _uid, enemy in defense.threatening_enemies()
               if defense.is_blocked(enemy))


def _spawn_defender(alarm, threat: tuple[Position, Position]) -> bool:
    """Spawn the blocker for `threat`, preferring a tile the sentry can throw from.

    The core has the lowest entity id on the team, so it always acts before the
    sentry launcher in the same round: a defender dropped inside the sentry's
    pickup radius now gets thrown onto its block tile a few microseconds later,
    same round. Only if we have no sentry does the defender have to walk, and
    then we at least start it on the side facing the threat.
    """
    global _last_dispatch_round
    enemy, block = threat
    round_num = rc.get_current_round()
    if round_num - _last_dispatch_round < DISPATCH_COOLDOWN:
        return False
    if _blocker_count() >= defense.MAX_BLOCKERS:
        return False
    if rc.get_global_resources() < rc.get_builder_bot_cost():
        return False

    sentry = alarm[0] if alarm is not None else defense.sentry_launcher_pos()
    if sentry is not None and defense.can_reach_by_throw(sentry, block):
        # If a builder is already inside the sentry's pickup radius the sentry
        # will borrow it this same round. Spawning a second body would cost 30 Ti
        # and a permanent +20% on every future builder for nothing.
        if _builder_in_pickup_range(sentry):
            return False
        tile = defense.spawn_tile_for(sentry, block)
        if tile is not None and rc.can_spawn(tile):
            rc.spawn_builder(tile)
            _last_dispatch_round = round_num
            log(f"core spawned defender at {tile} for enemy {enemy} (sentry throw to {block})")
            return True

    # No throw available, so the defender must walk. The block tile moves one
    # step per round — the same speed the defender walks — so a distant one is
    # never caught and the spawn is simply wasted titanium.
    box = defense.core_footprint()
    if box is not None:
        x0, x1, y0, y1 = box
        reach = (abs(block.x - min(max(block.x, x0), x1))
                 + abs(block.y - min(max(block.y, y0), y1)))
        if reach > WALK_DISPATCH_RANGE:
            return False
    if _spawn_best_toward(block):
        _last_dispatch_round = round_num
        log(f"core spawned walking defender toward {block} for enemy {enemy}")
        return True
    return False


def _spawn_under_siege(titanium: int) -> bool:
    """While a turret is shooting the core, turn banked titanium into bodies.

    The economic spawn gate wants ~200 Ti of headroom before it will make a
    builder. That is right in peacetime and suicidal under fire: instrumented
    losses show the core sitting on 200-240 Ti with five units while three enemy
    sentinels take it from 500 HP to 0 in twenty rounds. Titanium in the bank is
    worth nothing if the core dies, and each builder is another 2 damage a turn
    against a 30-40 HP turret, so under siege we spend down to the floor.
    """
    global _last_core_hp
    hp = rc.get_hp()
    losing_hp = _last_core_hp is not None and hp < _last_core_hp
    _last_core_hp = hp
    # Gate on damage actually landing, not merely on a turret having line of
    # sight. "Could shoot us" fires on any turret that happens to face our way
    # and had us emptying the bank against threats that were never going to
    # connect — worth about eight points against loki on its own.
    if not losing_hp:
        return False
    besiegers = defense.core_besiegers(rc)
    if not besiegers:
        return False
    cost = rc.get_builder_bot_cost()
    # Leave a working balance: spending literally to the floor buys bodies we
    # then cannot afford to build barriers or attack with.
    if titanium < cost + SIEGE_SPAWN_FLOOR:
        return False
    if rc.get_unit_count() >= SIEGE_MAX_UNITS:
        return False
    if _spawn_best_toward(besiegers[0][0]):
        log(f"core spawned siege responder toward {besiegers[0][0]} (ti={titanium})")
        return True
    return False


def _count_nearby_allies(core_pos: Position, my_team) -> int:
    """Friendly builders within DEFENSE_FRIENDLY_RADIUS_SQ of the core."""
    ally_builder_count = 0
    for uid in rc.get_nearby_units():
        if rc.get_entity_type(uid) != EntityType.BUILDER_BOT:
            continue
        if rc.get_team(uid) != my_team:
            continue
        if rc.get_position(uid).distance_squared(core_pos) <= DEFENSE_FRIENDLY_RADIUS_SQ:
            ally_builder_count += 1
    return ally_builder_count
from math import inf

def solve_gst(ores, avoid_mask=0):
    def get_dim(names):
        for name in names:
            if hasattr(map_info, name):
                v = getattr(map_info, name)
                return int(v() if callable(v) else v)
        raise AttributeError(f"Could not determine map dimension: {names}")
    W = get_dim(("width", "map_width", "_width", "_map_width", "get_width", "get_map_width"))
    H = get_dim(("height", "map_height", "_height", "_map_height", "get_height", "get_map_height"))
    N = W * H
    BOARD = (1 << N) - 1
    left_col = sum(1 << (y * W) for y in range(H))
    right_col = sum(1 << (y * W + W - 1) for y in range(H))
    no_left = BOARD ^ left_col
    no_right = BOARD ^ right_col
    def expand(m):
        return (((m << 1) & no_left) | ((m >> 1) & no_right) | ((m << W) & BOARD) | (m >> W))
    if hasattr(map_info, "_IDX_ENV_EMPTY"):
        empty_mask = map_info._bm_env[map_info._IDX_ENV_EMPTY] & BOARD
    else:
        empty_mask = 0
        for y in range(H):
            for x in range(W):
                if map_info.ground_at(x, y) == map_info._ENV_EMPTY:
                    empty_mask |= 1 << (y * W + x)
    core_mask = map_info._bm_my_core_area & BOARD
    if not core_mask:
        return inf, set(), 0
    open_mask = (empty_mask & ~avoid_mask) | core_mask
    reachable = core_mask
    frontier = core_mask
    while frontier:
        frontier = expand(frontier) & open_mask & ~reachable
        reachable |= frontier
    open_mask = reachable
    group_masks = []
    for ore in ores:
        x, y = ore.x, ore.y
        m = 0
        if x > 0:
            m |= 1 << (y * W + x - 1)
        if x + 1 < W:
            m |= 1 << (y * W + x + 1)
        if y > 0:
            m |= 1 << ((y - 1) * W + x)
        if y + 1 < H:
            m |= 1 << ((y + 1) * W + x)
        m &= empty_mask & open_mask
        if not m:
            return inf, set(), 0
        group_masks.append(m)
    group_masks.append(core_mask)
    k = len(group_masks)
    if k > 5:
        raise ValueError("solve_gst supports at most 4 ores")
    cells = []
    bits = open_mask
    while bits:
        bit = bits & -bits
        cells.append(bit.bit_length() - 1)
        bits ^= bit
    INF = N * 4 + 1
    bfs_cache = {}
    def bfs(source):
        source &= open_mask
        if source in bfs_cache:
            return bfs_cache[source]
        dist = [INF] * N
        seen = source
        frontier = source
        d = 0
        while frontier:
            bits = frontier
            while bits:
                bit = bits & -bits
                dist[bit.bit_length() - 1] = d
                bits ^= bit
            frontier = expand(frontier) & open_mask & ~seen
            seen |= frontier
            d += 1
        bfs_cache[source] = dist
        return dist
    D = [bfs(m) for m in group_masks]
    pair_cache = {}
    def pair_cost(i, j):
        a = group_masks[i]
        b = group_masks[j]
        key = (a, b) if a <= b else (b, a)
        if key in pair_cache:
            return pair_cache[key]
        da = D[i]
        db = D[j]
        base = []
        max_base = 0
        for v in cells:
            c = da[v] + db[v]
            if c < INF:
                base.append((c, v))
                if c > max_base:
                    max_base = c
        out = [INF] * N
        if not base:
            pair_cache[key] = out
            return out
        buckets = [0] * (max_base + len(cells) + 1)
        for d, v in base:
            buckets[d] |= 1 << v
        settled = 0
        for d in range(len(buckets)):
            frontier = buckets[d] & ~settled
            if not frontier:
                continue
            bits = frontier
            while bits:
                bit = bits & -bits
                out[bit.bit_length() - 1] = d
                bits ^= bit
            settled |= frontier
            if d + 1 < len(buckets):
                buckets[d + 1] |= expand(frontier) & open_mask & ~settled
        pair_cache[key] = out
        return out
    best = INF
    data = None
    if k == 1:
        return 0, set(), 0
    if k == 2:
        for v in cells:
            cost = D[0][v] + D[1][v]
            if cost < best:
                best = cost
                data = (2, v)
    elif k == 3:
        for v in cells:
            cost = D[0][v] + D[1][v] + D[2][v]
            if cost < best:
                best = cost
                data = (3, v)
    elif k == 4:
        for a, b, c, d in ((0, 1, 2, 3), (0, 2, 1, 3), (0, 3, 1, 2)):
            p = pair_cost(a, b)
            for v in cells:
                cost = p[v] + D[c][v] + D[d][v]
                if cost < best:
                    best = cost
                    data = (4, a, b, c, d, v)
    else:
        for e in range(5):
            rem = [i for i in range(5) if i != e]
            a, b, c, d = rem
            for a, b, c, d in ((a, b, c, d), (a, c, b, d), (a, d, b, c)):
                p1 = pair_cost(a, b)
                p2 = pair_cost(c, d)
                for v in cells:
                    cost = p1[v] + p2[v] + D[e][v]
                    if cost < best:
                        best = cost
                        data = (5, e, a, b, c, d, v)
    if data is None or best >= INF:
        return inf, set(), 0
    edges = set()
    path_mask = 0
    def step_down(dist, v):
        target = dist[v] - 1
        x = v % W
        if x > 0 and dist[v - 1] == target:
            return v - 1
        if x + 1 < W and dist[v + 1] == target:
            return v + 1
        if v >= W and dist[v - W] == target:
            return v - W
        if v + W < N and dist[v + W] == target:
            return v + W
        return -1
    def add_edge(a, b):
        nonlocal path_mask
        path_mask |= (1 << a) | (1 << b)
        p1 = (a % W, a // W)
        p2 = (b % W, b // W)
        edges.add((p1, p2) if p1 < p2 else (p2, p1))
    def walk_group(i, v):
        nonlocal path_mask
        path_mask |= 1 << v
        dist = D[i]
        while dist[v] > 0:
            u = step_down(dist, v)
            if u < 0:
                raise RuntimeError("group reconstruction failed")
            add_edge(v, u)
            v = u
    def walk_pair(i, j, v):
        nonlocal path_mask
        path_mask |= 1 << v
        p = pair_cost(i, j)
        while p[v] < D[i][v] + D[j][v]:
            u = step_down(p, v)
            if u < 0:
                raise RuntimeError("pair reconstruction failed")
            add_edge(v, u)
            v = u
        walk_group(i, v)
        walk_group(j, v)
    if data[0] == 2:
        _, v = data
        walk_group(0, v)
        walk_group(1, v)
    elif data[0] == 3:
        _, v = data
        walk_group(0, v)
        walk_group(1, v)
        walk_group(2, v)
    elif data[0] == 4:
        _, a, b, c, d, v = data
        walk_pair(a, b, v)
        walk_group(c, v)
        walk_group(d, v)
    else:
        _, e, a, b, c, d, v = data
        walk_pair(a, b, v)
        walk_pair(c, d, v)
        walk_group(e, v)
    return len(edges), edges, path_mask
def draw_edges(edges, r, g, b):
    for p1, p2 in edges:
        rc.draw_indicator_line(Position(*p1), Position(*p2), r, g, b)
def test_merge():
    colors = [[255, 0, 0], [128, 255, 0], [0, 255, 255], [128, 0, 255]]
    nearby_ore = list(map_info.iter_mask(
        map_info._bm_visible & map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    ))

    visible = {(p.x, p.y) for p in map_info.iter_mask(map_info._bm_visible)}
    core = {(p.x, p.y) for p in map_info.iter_mask(map_info._bm_my_core_area)}

    visited = set(core)
    queue = list(core)

    while queue:
        x, y = queue.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            p = (x + dx, y + dy)
            if (
                p not in visited
                and p in visible
                and map_info.ground_at(*p) == map_info._ENV_EMPTY
            ):
                visited.add(p)
                queue.append(p)

    nearby_ore = [
        [ore]
        for ore in nearby_ore
        if any(
            (ore.x + dx, ore.y + dy) in visited
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        )
    ]
    while len(nearby_ore) > 4:
        closest = [100000, -1, -1]
        for i in range(len(nearby_ore)):
            for j in range(i + 1, len(nearby_ore)):
                sum_dist = sum(sum(nav.bfs_dist(p1, p2, False) for p1 in nearby_ore[i]) for p2 in nearby_ore[j])
                if sum_dist < closest[0]:
                    closest = [sum_dist, i, j]
        i, j = closest[1], closest[2]
        nearby_ore[i].extend(nearby_ore[j])
        del nearby_ore[j]
    nearby_ore.sort(key=len, reverse=True)
    import time

    avoid_mask = 0

    for i, a in enumerate(nearby_ore):
        start = time.perf_counter()
        dist, edges, path_mask = solve_gst(a, avoid_mask)
        elapsed = time.perf_counter() - start
        print(f"solve_gst took {elapsed:.6f} seconds")
        if dist == inf:
            continue
        avoid_mask |= path_mask
        draw_edges(edges, colors[i][0], colors[i][1], colors[i][2])
        for j in a:
            rc.draw_indicator_dot(j, colors[i][0], colors[i][1], colors[i][2])

def run():
    global _spawn_plan, _spawn_tiles

    map_info.update()
    comms.read()    # absorb every slot's shared tiles/symmetry, broadcast our own
    alarm = comms.read_alarm()

    comms.write()
    map_info.recompute_derived()
    if rc.get_current_round() == 0:
        import time

        start = time.perf_counter()

        test_merge()

        elapsed = time.perf_counter() - start
        print(f"total took {elapsed:.6f} seconds")
    # for i in map_info.iter_mask(map_info._bm_env[map_info._IDX_ENV_WALL]):
    #     rc.draw_indicator_dot(i, 255, 255, 255)

    if not _spawn_tiles:
        _spawn_tiles = _compute_spawn_tiles()

    titanium = rc.get_global_resources()
    scaling = rc.get_scale_percent()
    core_pos = map_info._my_pos
    my_team = map_info._my_team
    
    if _spawn_plan is None:
        _spawn_plan = choose_spawn_plan(rc, core_pos, INITIAL_SPAWN_COUNT)
    if rc.get_current_round() <= INITIAL_SPAWN_COUNT + INITIAL_EXPLORE_MAX_STEPS:
        draw_spawn_plan(rc, core_pos, _spawn_plan, rc.get_map_width(), rc.get_map_height())

    # On-demand defence takes priority over every economic spawn: the whole
    # point of reserving a builder's cost (map_info.ti_reserve) is that this
    # spawn is always affordable the round it is needed.
    ally_builder_count = _count_nearby_allies(core_pos, my_team)
    threat = _threat_to_answer(alarm)
    map_info.arm_reserve(threat is not None)
    if not (threat is not None and _spawn_defender(alarm, threat)):
        if not _spawn_under_siege(titanium):

            # Otherwise only spawn if we have extra resources.
            #
            # This used to gate on `get_scale_percent() + 200 < titanium`, which
            # compares a *percentage* against a titanium amount. The scale climbs
            # with every building the team has ever built — 273 by round 40 on
            # quarry — so the gate demanded 473 Ti and then kept rising, and the
            # core simply stopped making economic builders: unit counts froze
            # around 8 while the eventual winner ran 16. Gating on the bot's
            # actual cost plus a working buffer is the dimensionally correct
            # version and keeps the workforce growing.
            buffer = ECON_BUFFER_MANY if ally_builder_count >= 12 else ECON_BUFFER
            # A builder is only worth its cost if there is work for it. Builder
            # cost scales +20% per build while a harvester scales +5%, so the
            # seventh builder costs several harvesters, and the state trace on a
            # saga loss shows the marginal builders falling through to disrupt
            # (414 of ~1130 turns) and explore (206) -- walking, not building.
            # We finished that game 7 builders / 2 harvesters against loki's
            # 4 builders / 5 harvesters, and lost 390 Ti to 910. Tie workforce
            # growth to income: past a small starting crew, only add a builder if
            # the harvesters exist to pay for it.
            # Swept twice, all 33 maps both sides, against the champion of the
            # day. On the v44 base (before pay-as-you-go): *4 48.5%, *3 50.0%,
            # *2 54.5%, *1.5 54.5%, *1 51.5%. Re-swept on the v46 base once
            # pay-as-you-go had changed the economy, and the optimum moved
            # tighter as you would expect when harvesters get cheaper to reach:
            # *4 45.5%, *3 47.0%, *2 50.0% (baseline), *1.5 54.5%, *1 50.0%.
            #
            # *1.5 is the only setting that has beaten or matched every rival on
            # both bases, which is why it wins over *2 on a 36-30 margin that
            # would not carry on its own.
            #
            # The starting crew is sharply peaked at 4 -- 3 scores 43.9%, 5
            # scores 48.5%, 6 scores 39.4% -- so it stays put.
            free_crew = 4
            harvesters = defense.my_count(map_info._IDX_HARVESTER)
            crew_ok = ally_builder_count < free_crew or harvesters * 3 >= ally_builder_count * 2
            if crew_ok and titanium >= rc.get_builder_bot_cost() + buffer:

                # First spawn according to initial plan, then spawn toward center
                if not _spawn_toward_plan(core_pos):
                    _spawn_toward_center()

    # Ammount of ammo we want to have
    target_ammo_ammount = min(min(50, rc.get_global_resources()-2), rc.get_current_round() * 2)

    # Convert titanium into ammo if we want more
    ammo_amount = target_ammo_ammount - rc.get_global_ammo()
    if ammo_amount > 0 and rc.can_convert_ammo(ammo_amount):
        rc.convert_ammo(ammo_amount)
