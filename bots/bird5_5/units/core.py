from main import has_op
from fcode import Controller, Direction, Position, EntityType
import map_info
from log import log
import comms
import conveyor_plan
import pathing
from pathing import Pathing
from units.spawn_plan import choose_fanout_plan, INITIAL_SPAWN_COUNT
import rush
from _config import (ENEMY_START_TI, ENEMY_TI_PER_ROUND, MAX_DEFENSIVE_HEALERS,
                     RUSH_SENTINEL_HP, CORE_HP_WINDOW, CORE_DEFEND_HP,
                     CORE_CRITICAL_HP, ENEMY_TI_OVERESTIMATE)
from fcode import GameConstants
import sys
import random
rc: Controller

# --- Configurable ---
SCALE_MULT = 0.8
# Core distress alarm fires when the core sees exactly ONE enemy builder bot AND at
# least this many enemy sentinels can fire on the core -- a sentinel siege the core
# needs every builder to come heal (heal returns MAX_SCORE, target = core).
CORE_ALARM_SENTINELS = 2
# The alarm also requires the core to actually be taking damage -- below this HP.
CORE_ALARM_MAX_HP = 480
# A builder bot sees tiles within this squared distance, so an ally within it of
# the attacked tile can already respond -- no defensive spawn needed.
BUILDER_VISION_SQ = GameConstants.BUILDER_BOT_VISION_RADIUS_SQ

# Only pull a fresh defender for a building that's actually in danger: one an enemy
# is next to AND that has dropped below this HP. A lightly-scratched building can
# wait, so it doesn't trigger a spawn.
DEFENSE_HP_THRESHOLD = 14

_starting_convs: list[tuple[int]] = []
# Direction from the core to each starting-conv ore group, aligned with
# `_starting_convs`. Set by `starting_convs()`; used to seed the fan-out spread.
_starting_conv_dirs: list = []
# Bearings for the opening builders left over once every ore group has a builder,
# chosen to spread away from the ore-group directions. Computed once on round 0.
_fanout_dirs: list = []
_spawn_tiles: tuple[Position, ...] = ()   # tiles immediately surrounding the 2x2 core
nav: Pathing = None

def _compute_spawn_tiles():
    offset = [-1, 0, 1, 2]
    out = []
    for x in offset:
        for y in offset:
            if (x in (0, 1) and y in (0, 1)):
                continue
            px, py = map_info._my_pos.x + x, map_info._my_pos.y + y
            # Bounds first: ground_at now answers None off the board, and
            # None != _ENV_WALL, so an off-board tile would be collected here.
            if not map_info.in_bounds_coords(px, py):
                continue
            pos = Position(px, py)
            if map_info.ground_at(px, py) != map_info._ENV_WALL:
                out.append(pos)
    return tuple(out)
def init(c: Controller):
    global rc, nav
    rc = c
    nav = Pathing(c)


def _spawn_affordable() -> bool:
    """True iff spawning a builder still leaves the heal reserve in the bank.

    A builder is our single most expensive purchase (30 Ti base, +20% each), and
    alphaduck spent it through `rc.can_spawn()` alone -- engine truth about
    whether we can pay at all, which is a floor of 0, not of `ti_reserve()`. Every
    spawn path routes through this so the floor holds for all three of them:
    the opening conv root, the fan-out/centre spawns, and the defensive spawn.
    """
    return rc.get_global_resources() >= rc.get_builder_bot_cost() + map_info.ti_reserve()


def _spawn_best_toward(target: Position) -> bool:
    """Spawn a builder on the surrounding tile closest to `target`. Returns True
    if a builder was spawned."""
    if not _spawn_affordable():
        return False
    best_d = None
    best_tiles = []
    for p in _spawn_tiles:
        if rc.can_spawn(p):
            d = nav.bfs_dist(p, target)
            if best_d is None or d < best_d:
                best_d = d
                best_tiles = [p]
            elif d == best_d:
                best_tiles.append(p)
    if not best_tiles:
        return False
    _register_spawned(rc.spawn_builder(random.choice(best_tiles)))
    return True


def _spawn_toward_center() -> bool:
    """Spawn on the surrounding tile closest to map center."""
    center = Position(map_info._width // 2, map_info._height // 2)
    return _spawn_best_toward(center)


def _spawn_toward_dir(core_pos: Position, direction: Direction) -> bool:
    """Spawn on the surrounding tile best aligned with `direction`. Aims well past
    the 2x2 footprint so the closest spawn tile is the one on that bearing, but
    CLAMPS the aim point onto the board: a core near an edge would otherwise aim
    off-map, and bfs_dist's `1 << (x + y*w)` throws on a negative coordinate."""
    dx, dy = map_info._DIRECTION_DELTAS[direction]
    tx = min(max(core_pos.x + dx * 8, 0), map_info._width - 1)
    ty = min(max(core_pos.y + dy * 8, 0), map_info._height - 1)
    return _spawn_best_toward(Position(tx, ty))


# ---------------------------------------------------------------------------
# Defensive spawn: an enemy builder bot near our core is attacking a building
# there. We cancel each attacking enemy against the closest friendly builder that
# can SEE it (one friendly answers one enemy) -- and if any attackers are left
# over (we're outnumbered, or no friendly is close enough to see them), we spawn a
# defender toward the nearest-core building those leftovers are hitting. Counting
# instead of "is any ally nearby?" means a lone defender no longer suppresses the
# spawn while several enemies pile on.
# ---------------------------------------------------------------------------
def _ally_positions():
    """Where our ally builders are: the comms global array, plus any the core
    sees directly (a fresh builder without a comms slot yet)."""
    positions = list(comms.ally_positions())
    fb = map_info._bm_friendly_bots
    w = map_info._width
    while fb:
        b = fb & -fb
        fb ^= b
        n = b.bit_length() - 1
        positions.append(Position(n % w, n // w))
    return positions


def _dist_to_core(x: int, y: int) -> int:
    """Manhattan distance from (x, y) to the 2x2 core footprint."""
    core = map_info._my_core
    if core is None:
        return 0
    cx = core.x if x < core.x else (core.x + 1 if x > core.x + 1 else x)
    cy = core.y if y < core.y else (core.y + 1 if y > core.y + 1 else y)
    return abs(x - cx) + abs(y - cy)


def _find_defense_target():
    """Tile near our core that a defender is needed at, or None.

    An enemy builder bot is "attacking" if it sits next to a damaged friendly
    building the core can see. We pair each such attacker with the closest DISTINCT
    friendly builder that can see it -- that friendly is assumed to answer it.
    Whatever attackers remain unpaired (we're outnumbered, or no friendly is close
    enough to see them) are the ones a fresh spawn must meet; we aim it at the
    nearest-core building one of them is hitting. Nearest-core target wins."""
    if map_info._bm_my_core_area == 0:
        return None
    enemy_bots = map_info._bm_enemy_bots
    if not enemy_bots:
        return None
    my = map_info._bm_team[map_info._my_team_idx]
    w = map_info._width
    # Friendly buildings the core can see (i.e. near it) that are damaged and sit
    # next to an enemy builder bot -- actively being attacked.
    attacked = (my & map_info._bm_any_building & map_info._bm_damaged
                & map_info._bm_visible & map_info.manhattan(enemy_bots))
    if not attacked:
        return None
    # Keep only the ones actually in danger -- below DEFENSE_HP_THRESHOLD. A
    # building an enemy is chipping but that's still healthy can wait.
    danger = 0
    m = attacked
    while m:
        b = m & -m
        m ^= b
        if map_info._building_hp[b.bit_length() - 1] < DEFENSE_HP_THRESHOLD:
            danger |= b
    attacked = danger
    if not attacked:
        return None
    # The enemy bbots doing the attacking: those adjacent to an attacked building.
    attacker_mask = enemy_bots & map_info.manhattan(attacked)
    attackers = []
    m = attacker_mask
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        attackers.append((n % w, n // w))
    if not attackers:
        return None

    # Cancel each attacker against the closest friendly builder that can see it; a
    # friendly answers only one attacker. The leftovers are what we're outnumbered
    # by and must spawn against.
    allies = _ally_positions()
    uncancelled = 0
    unc_mask = 0
    for (ex, ey) in attackers:
        best_i = -1
        best_d = None
        for i, ap in enumerate(allies):
            dx = ap.x - ex
            dy = ap.y - ey
            d2 = dx * dx + dy * dy
            if d2 <= BUILDER_VISION_SQ and (best_d is None or d2 < best_d):
                best_d = d2
                best_i = i
        if best_i >= 0:
            allies.pop(best_i)             # this friendly is spoken for
        else:
            uncancelled += 1
            unc_mask |= 1 << (ex + ey * w)
    if uncancelled == 0:
        return None

    # Spawn toward the attacked building nearest our core that a still-uncancelled
    # attacker is next to (fall back to any attacked building if the intersection
    # is somehow empty).
    targets = attacked & map_info.manhattan(unc_mask)
    if not targets:
        targets = attacked
    best_key = None
    best_tiles = []
    m = targets
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        x, y = n % w, n // w
        key = _dist_to_core(x, y)
        if best_key is None or key < best_key:
            best_key = key
            best_tiles = [Position(x, y)]
        elif key == best_key:
            best_tiles.append(Position(x, y))
    return random.choice(best_tiles) if best_tiles else None


def _spawn_toward_adjacent(tile: Position) -> bool:
    """Spawn a builder on the surrounding tile with the least BFS distance to
    STANDING on a tile adjacent to `tile`. Returns True if one was spawned.

    A spawn tile that is itself adjacent to `tile` is distance 0 -- so we multi-
    source BFS out from the adjacency set and take the first spawn tile the flood
    actually lands on (not merely reaches the side of, which is what nav.closest
    measures and which picked a tile one step too far)."""
    w, h = map_info._width, map_info._height
    passable = map_info.passable()
    adj = 0
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        x, y = tile.x + dx, tile.y + dy
        if 0 <= x < w and 0 <= y < h:
            adj |= 1 << (x + y * w)
    adj &= passable
    if not adj:
        return False
    spawn_mask = 0
    for p in _spawn_tiles:
        if rc.can_spawn(p):
            spawn_mask |= 1 << (p.x + p.y * w)
    if not spawn_mask:
        return False

    # BFS out from the adjacency tiles; the first spawn tile the flood enters is
    # the one reachable in the fewest steps (0 if it IS an adjacency tile). Spawn
    # tiles must be enterable, so fold them into the traversable set.
    trav = passable | spawn_mask
    frontier = adj
    visited = adj
    best_spawn = None
    while frontier:
        hit = frontier & spawn_mask
        if hit:
            choices = []
            m = hit
            while m:
                b = m & -m
                m ^= b
                n = b.bit_length() - 1
                choices.append(Position(n % w, n // w))
            best_spawn = random.choice(choices)
            break
        frontier = map_info.expand_manhattan(frontier) & trav & ~visited
        visited |= frontier
    if best_spawn is None or not rc.can_spawn(best_spawn) or not _spawn_affordable():
        return False
    _register_spawned(rc.spawn_builder(best_spawn))
    log(f"core spawned defender at {best_spawn} toward attacked {tile}")
    return True


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
def starting_convs():
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
    # Merge the closest ore groups until there are at most INITIAL_SPAWN_COUNT
    # (4) of them: the opening spawns one starting-conv builder per group and
    # fans the rest out to that same budget, so a 5th group would mean a 5th
    # conv builder we never budgeted for (and used to crash on colors[4]).
    # Only merge pairs whose COMBINED size stays <= 4 ores -- solve_gst raises on
    # a group with more than 4 ores. If no size-legal merge remains but we still
    # have too many groups (very ore-dense map), keep the 4 largest and leave the
    # rest to the normal harvest/route states.
    MAX_GROUP_ORES = 4
    while len(nearby_ore) > INITIAL_SPAWN_COUNT:
        closest = [100000, -1, -1]
        for i in range(len(nearby_ore)):
            for j in range(i + 1, len(nearby_ore)):
                if len(nearby_ore[i]) + len(nearby_ore[j]) > MAX_GROUP_ORES:
                    continue                          # would exceed solve_gst's cap
                sum_dist = sum(sum(nav.bfs_dist(p1, p2, False) for p1 in nearby_ore[i]) for p2 in nearby_ore[j])
                if sum_dist < closest[0]:
                    closest = [sum_dist, i, j]
        if closest[1] < 0:
            break                                     # no size-legal merge left
        i, j = closest[1], closest[2]
        nearby_ore[i].extend(nearby_ore[j])
        del nearby_ore[j]
    nearby_ore.sort(key=len, reverse=True)
    del nearby_ore[INITIAL_SPAWN_COUNT:]              # never exceed the builder budget
    import time

    avoid_mask = 0
    core_ref = map_info._my_pos
    groups = []          # (edges, group_direction | None), aligned per ore group
    for i, a in enumerate(nearby_ore):
        start = time.perf_counter()
        dist, edges, path_mask = solve_gst(a, avoid_mask)
        elapsed = time.perf_counter() - start
        print(f"solve_gst took {elapsed:.6f} seconds")
        gdir = None
        if dist != inf:
            avoid_mask |= path_mask
            # Debug colouring only. There can be up to 5 ore groups (the merge
            # loop caps at 5) but `colors` has 4 entries, so index by modulo --
            # `colors[i]` threw IndexError on 5-group maps and, because it runs
            # inside starting_convs(), aborted the whole opening plan -> the core
            # never spawned and the game collected 0 titanium.
            col = colors[i % len(colors)]
            draw_edges(edges, col[0], col[1], col[2])
            for j in a:
                rc.draw_indicator_dot(j, col[0], col[1], col[2])
            # Bearing from the core to this ore group's centroid -- the direction
            # the builder assigned here effectively heads, used to seed the
            # fan-out spread for the leftover opening builders.
            cx = round(sum(p.x for p in a) / len(a))
            cy = round(sum(p.y for p in a) / len(a))
            if (cx, cy) != (core_ref.x, core_ref.y):
                gdir = map_info.direction_to(core_ref, Position(cx, cy))
        groups.append((edges, gdir))
    # Bigger trees first (unchanged), keeping each group's direction aligned.
    groups.sort(key=lambda g: len(g[0]), reverse=True)
    global _starting_conv_dirs
    _starting_conv_dirs = [g[1] for g in groups]
    return [g[0] for g in groups]

def _spawn_starting_conv(r: int) -> bool:
    """Spawn the r-th starting-conv builder on its tree root and queue that whole
    conveyor tree into slot 0 (published by the following comms.write()). Returns
    True iff a builder was spawned. The builder decodes the plan next turn from
    its own (root) tile -- see conveyor_plan.py and comms.read_core_plan."""
    tree = _starting_convs[r]
    if not tree:
        return False
    core_tiles = [(p.x, p.y) for p in map_info.iter_mask(map_info._bm_my_core_area)]
    built = conveyor_plan.build_tree(tree, core_tiles)
    if built is None:
        return False
    root, excluded_dir, adj = built
    if root not in _spawn_tiles or not rc.can_spawn(root) or not _spawn_affordable():
        return False
    _register_spawned(rc.spawn_builder(root))
    budget = comms.core_plan_dfs_budget()
    dfs_bits = conveyor_plan.encode_dfs_bits(root, excluded_dir, adj, max_bits=budget)
    fit_nodes = budget // 3
    if len(adj) > fit_nodes:
        # The tree is larger than one slot can carry; encode_dfs_bits kept a
        # connected subtree of fit_nodes rooted at `root`, the rest is dropped.
        print(f"conveyor plan capped: {len(adj)} conveyors, only {fit_nodes} fit in slot 0")
    comms.queue_core_plan(dfs_bits)
    return True


def _register_spawned(bid: int) -> None:
    """Tell comms about a builder we just spawned so it can broadcast slot
    ownership. Originals (spawned rounds 0-3) are recorded for the round-4 id word;
    later builders get the next free slot pair announced next turn."""
    if bid is None:
        return
    if rc.get_current_round() < comms._NUM_ORIGINAL:
        comms.register_original(bid)
    else:
        comms.register_spawn(bid)


def _home_builders() -> int:
    """Builder bots we believe are alive and NOT off on the rush.

    Deliberately not `get_unit_count()`: that counts the core and every turret,
    so each siege sentinel the rusher plants would read as a defender and
    suppress the very healer spawn it makes necessary. `_bm_friendly_bots` is
    builder bots only -- ours plus any relayed through comms -- and the rusher is
    subtracted while a siege is live, since it is never coming home to heal.
    """
    n = map_info._bm_friendly_bots.bit_count()
    if comms.siege_active():
        n -= 1
    return max(0, n)


def sentinels_bearing_on_core() -> int:
    """Enemy sentinels whose ACTUAL facing puts one of our core tiles on its ray.

    Not "stands on a tile that could hit the core from some facing" -- that was
    the first version and it over-counted badly, because a sentinel's facing is
    frozen at build time (`rotate()` is gunner-only). One aimed down a corridor
    away from us sat on a class-B tile and read as a besieger.

    `can_fire_from` is engine truth about the hypothetical, and a sentinel's ray
    ignores walls and buildings entirely, so a positive answer here really does
    mean that turret is putting shots into the core.
    """
    core = map_info._my_core
    if core is None:
        return 0
    enemy = map_info._bm_team[1 - map_info._my_team_idx]
    sentinels = map_info._bm_et[map_info._IDX_SENTINEL] & enemy
    if not sentinels:
        return 0
    w = map_info._width
    core_tiles = list(map_info.iter_mask(map_info._bm_my_core_area))
    if not core_tiles:
        return 0
    hits = 0
    m = sentinels
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        bid = map_info._building_id[n]
        if bid is None:
            continue
        try:
            d = rc.get_direction(bid)
        except Exception:
            continue
        pos = Position(n % w, n // w)
        if any(rc.can_fire_from(pos, d, EntityType.SENTINEL, ct) for ct in core_tiles):
            hits += 1
    return hits


# A rush is MORE THAN ONE sentinel bearing on the core while the core is taking
# damage. Both halves matter:
#   * >1, because a single sentinel is 9 HP/round against a 500 HP core -- 55
#     rounds to kill, and any one builder out-heals it at 4 HP/round for 1 Ti.
#     Garrisoning against one turret would hold builders home for a threat that
#     is not one.
#   * damaged, because the geometry test alone fires on a turret that is aimed at
#     us but is not shooting -- out of ammunition, or newly built. HP actually
#     moving is the proof that the rush has started.
RUSH_SENTINELS = 1


# Core HP history, for the NET trend. (round, hp) appended once per core turn.
_hp_hist: list = []


def _record_core_hp() -> None:
    """Called once per core turn, before anything reads the trend."""
    r = rc.get_current_round()
    if _hp_hist and _hp_hist[-1][0] == r:
        return
    _hp_hist.append((r, rc.get_hp()))
    cutoff = r - CORE_HP_WINDOW
    while len(_hp_hist) > 2 and _hp_hist[0][0] < cutoff:
        _hp_hist.pop(0)


def core_net_dps() -> float:
    """HP the core is losing per round, NET of the healing already happening.
    Positive = losing ground. 0.0 until there is a window to measure over.

    Net is the whole point. Incoming damage alone would have to be paired with a
    guess at how much of our healing is landing; the HP curve already nets the
    two, so "still falling" means precisely "the current garrison is too small",
    whatever is causing it -- sentinels, gunners, builders chipping, or several
    at once.
    """
    if len(_hp_hist) < 2:
        return 0.0
    (r0, hp0), (r1, hp1) = _hp_hist[0], _hp_hist[-1]
    if r1 <= r0:
        return 0.0
    return (hp0 - hp1) / float(r1 - r0)


def under_rush() -> bool:
    """Is the core under attack it needs more healers for?

    ONE sentinel counts. It was `> 1`, on the reasoning that a single sentinel is
    out-healed by a single builder -- which is false arithmetic: a sentinel is
    9 HP/round and a builder heals 4. Game 13216 is the whole argument. spork put
    exactly ONE sentinel on our core at turn 12; `> 1` never fired, so the
    garrison never spawned; heal.py's Voronoi claim gave the core to exactly one
    builder; and the core bled ~0.5 HP/round for 920 turns and died at 933 --
    while we sat on 3231 titanium and out-mined them 2050 to 0.

    Losing HP at all is also a trigger, whatever is causing it: a falling core is
    a fact, where a sentinel count is an inference.
    """
    if rc.get_hp() >= rc.get_max_hp():
        return False                      # core untouched -> nothing is landing
    return sentinels_bearing_on_core() >= RUSH_SENTINELS or core_net_dps() > 0.0


_healer_target = 0          # ratchets up under fire, stands down when clear


def defensive_healers_wanted() -> int:
    """How many builders to hold at home healing. Three sources, strongest wins.

    1. THE MEASURED TREND (the one that matters). If the core is still losing HP,
       the garrison is too small BY DEFINITION -- `core_net_dps()` already nets
       out whatever healing is landing. Each extra builder buys 4 HP/round, so
       ceil(net / 4) more of them flips the sign. This needs no model and no
       guess about the attacker, and it is what game 13216 needed: one sentinel,
       one healer, -0.5 HP/round, dead 920 turns later with 3231 Ti in the bank.

    2. THE COST MODEL, as a floor while a real siege is on: the fewest healers
       that price the attacker's cheapest rush above what we credit them with.

    3. ABSOLUTE HP. Below CORE_CRITICAL_HP every spare builder heals, at the cap,
       whatever 1 and 2 say -- losing the core loses the game, so nothing else is
       worth titanium at that point.

    It RATCHETS. The target only rises while under fire and resets when the core
    is whole again with nothing bearing on it. Sizing off the instantaneous trend
    alone would stand builders down the moment healing caught up, which is the
    moment the trend is only flat BECAUSE they are there.
    """
    global _healer_target
    hp = rc.get_hp()
    bearing = sentinels_bearing_on_core()

    # All clear: core whole, nothing aimed at it -> release everyone.
    if hp >= rc.get_max_hp() and bearing == 0:
        _healer_target = 0
        return 0

    if not under_rush():
        return _healer_target        # hold what we have; do not grow

    net = core_net_dps()

    # 1. Trend. Only grow once the core is worth defending (or already falling
    #    fast), so a single scratch does not empty the economy.
    if net > 0.0 and (hp < CORE_DEFEND_HP or bearing >= RUSH_SENTINELS):
        extra = int(-(-net // rush.HEAL_PER_BUILDER)) or 1   # ceil, at least one
        _healer_target = max(_healer_target, _home_builders() + extra)

    # 2. Model floor.
    if bearing >= RUSH_SENTINELS:
        r = rc.get_current_round()
        enemy_ti = rush.estimated_enemy_ti(
            r, ENEMY_START_TI, ENEMY_TI_PER_ROUND * ENEMY_TI_OVERESTIMATE)
        a = rc.get_sentinel_cost() / rush.BASE_PRICE
        _healer_target = max(_healer_target,
                             rush.healers_needed(enemy_ti, RUSH_SENTINEL_HP, a,
                                                 cap=MAX_DEFENSIVE_HEALERS))

    # 3. Critical.
    if hp < CORE_CRITICAL_HP:
        _healer_target = MAX_DEFENSIVE_HEALERS

    return min(_healer_target, MAX_DEFENSIVE_HEALERS)


def _predicted_income_raw() -> int:
    """Sum over conveyors feeding the core of min(n, 4), where n = the number of
    ti-source tiles within hops 0-3 upstream of that feeder (feeder itself = hop 0).
    A ti source is a conveyor observed carrying ti (map_info._bm_conv_ti) OR a
    harvester -- a harvester is treated exactly like a conveyor with a ti stack on
    it. Harvesters feed conveyors (they sit orthogonally adjacent) but are leaves,
    so they never extend the trace further."""
    reverse = map_info._conv_reverse
    team = map_info._my_team_idx
    conv = map_info._bm_conveyors & map_info._bm_team[team]
    harv = map_info._bm_et[map_info._IDX_HARVESTER] & map_info._bm_team[team]
    src = map_info._bm_conv_ti | harv          # tiles that count as one ti stack
    nrev = len(reverse)

    def _feeders_of(mask):                     # conveyor feeders + adjacent harvesters
        up = 0
        m = mask
        while m:
            b = m & -m
            m ^= b
            tn = b.bit_length() - 1
            if tn < nrev:
                up |= reverse[tn]
        return up & conv

    # feeders: my conveyors whose output enters a core tile
    feeders = _feeders_of(map_info._bm_my_core_area)
    total = 0
    fm = feeders
    while fm:
        fb = fm & -fm
        fm ^= fb
        window = fb                            # conveyors + harvesters seen so far
        frontier = fb                          # only conveyors extend the trace
        for _ in range(3):                     # hops 1-3 upstream
            up_conv = _feeders_of(frontier) & ~window
            up_harv = map_info.manhattan(frontier) & harv & ~window
            new = up_conv | up_harv
            if not new:
                break
            window |= new
            frontier = up_conv                 # harvesters are leaves
        total += min((window & src).bit_count(), 4)
    return total & 0x7F


def _core_alarm_condition() -> bool:
    """Broadcast "everyone come heal the core".

    This is the half that makes the garrison mean anything. heal.py claims the
    core through a Voronoi split, so exactly ONE builder tends it -- unless the
    alarm is up, in which case every builder targets it directly. Spawning more
    healers without raising the alarm just makes more economy builders.

    alphaduck's condition required the core to see EXACTLY ONE enemy builder bot.
    In game 13216 spork attacked with a single sentinel and NO builders, so the
    count was 0, the alarm never fired once in 933 rounds, and the other two
    builders farmed while the core died. The enemy-builder clause is gone: what
    matters is that the core is being hurt and cannot keep up, not who is doing it.

    Fires when the core is damaged AND either something is aimed at it or it is
    measurably still losing ground.
    """
    if rc.get_hp() >= CORE_ALARM_MAX_HP:
        return False                      # barely scratched
    return sentinels_bearing_on_core() >= RUSH_SENTINELS or core_net_dps() > 0.0


def run():
    global _spawn_tiles, _starting_convs, _fanout_dirs

    map_info.update()
    comms.read()    # absorb every slot's shared tiles/symmetry
    map_info.recompute_derived()
    _record_core_hp()   # before anything reads core_net_dps() this turn

    # Debug: green dot on every ally builder, red on every enemy -- both come from
    # the global comm store, so the core dots (and prints) bots it cannot see itself.
    # Enemies come from map_info's id-deduped set so a bot relayed at two tiles (it
    # moved between two builders' sightings) shows as one enemy, matching the mask.
    _w = map_info._width
    for _p, _id in comms.friendly_bots():
        rc.draw_indicator_dot(_p, 0, 255, 0)
        print(f"ally ({_p.x},{_p.y},{_id})")
    for _n, _id in map_info._comm_enemy_ids.items():
        _p = Position(_n % _w, _n // _w)
        rc.draw_indicator_dot(_p, 255, 0, 0)
        print(f"enemy ({_p.x},{_p.y},{_id})")

    if rc.get_current_round() == 0:
        import time

        start = time.perf_counter()

        _starting_convs = starting_convs()
        # Opening budget is INITIAL_SPAWN_COUNT builders: one per ore group, then
        # the leftovers fan out. The fan-out bearings are kept maximally spread
        # from the ore-group directions (_starting_conv_dirs) for best coverage.
        n_fanout = max(0, INITIAL_SPAWN_COUNT - len(_starting_convs))
        ore_dirs = [d for d in _starting_conv_dirs if d is not None]
        _fanout_dirs = choose_fanout_plan(rc, map_info._my_pos, n_fanout, ore_dirs)

        elapsed = time.perf_counter() - start
        print(f"total took {elapsed:.6f} seconds")

    if not _spawn_tiles:
        _spawn_tiles = _compute_spawn_tiles()

    # Opening: spawn the starting-conv builder on its tree root and hand it the
    # whole conveyor tree via slot 0. Queue it BEFORE the broadcast so this same
    # turn's write carries the plan (the builder reads it next turn).
    r = rc.get_current_round()
    num_groups = len(_starting_convs)
    spawned_conv = False
    if r < num_groups:
        spawned_conv = _spawn_starting_conv(r)

    # Predicted income: sum over conveyors feeding the core of min(n,4), n = ti
    # stacks within hops 0-3 upstream. Broadcast the raw value (route_total reads
    # it); print the *10/4 estimate.
    _inc = _predicted_income_raw()
    comms.set_income(_inc)
    print(f"income {_inc * 10 / 4}")

    # Core distress: exactly one enemy builder bot in view AND >= CORE_ALARM_SENTINELS
    # enemy sentinels able to fire on the core -> raise the alarm so every builder
    # drops what it's doing and comes to heal the core.
    comms.set_core_alarm(_core_alarm_condition())

    # Healing race: are the healers keeping up? core_net_dps() is the honest
    # answer -- it nets the damage against the healing actually landing. Turrets
    # read this to decide whether shooting down an enemy turret is worth the
    # ammunition (see turret_priority.duel_worth_it).
    comms.set_heal_ok(core_net_dps() <= 0.0)

    comms.write()   # broadcast our word (plan / ids / sym+income, by round)

    titanium = rc.get_global_resources()

    # On-demand defence takes priority over every economic/fan-out spawn: the
    # whole point of reserving a builder's cost (map_info.ti_reserve) is that
    # this spawn is always affordable the round it is needed. Only one builder
    # can spawn per turn, so this only fires when the opening didn't spawn a
    # conv-root this turn.
    defense_target = _find_defense_target()
    map_info.arm_reserve(defense_target is not None)
    spawned_defense = False
    if defense_target is not None and not spawned_conv:
        spawned_defense = _spawn_toward_adjacent(defense_target)

    # Defensive garrison: bird1 stopped spawning entirely once the opening was
    # done, which is fine for a bot that never gets rushed and fatal for one that
    # does. Keep producing builders until we hold as many as the rush model says
    # we need to out-heal the enemy's cheapest sentinel rush. They are ordinary
    # builders -- they harvest and route until something needs healing, at which
    # point heal.py outranks the economy on its own.
    if (not spawned_conv and not spawned_defense
            and r >= num_groups + len(_fanout_dirs)
            and _home_builders() < defensive_healers_wanted()
            and titanium >= rc.get_builder_bot_cost() + map_info.ti_reserve()):
        spawned_defense = _spawn_toward_center()

    if (not spawned_conv and not spawned_defense
            and titanium >= rc.get_scale_percent() * SCALE_MULT):
        if r < num_groups:
            _spawn_toward_center()
        else:
            # Ran out of ore groups: fan the remaining opening builders out along
            # the precomputed spread bearings (falling back to center if the best
            # spawn tile for that bearing is blocked).
            idx = r - num_groups
            if idx < len(_fanout_dirs):
                if not _spawn_toward_dir(map_info._my_pos, _fanout_dirs[idx]):
                    _spawn_toward_center()

    # --- ammunition ----------------------------------------------------------
    # Sized to the siege, not to a flat cap. The rusher publishes X, the number of
    # our sentinels standing at the enemy core (comms slot 14 -- our own core
    # cannot see them). We bank enough for X+1 of them to fire NEXT round:
    # X because every standing sentinel wants a shot, +1 so the one currently
    # being built is paid for the moment it lands. A sentinel shot is
    # SENTINEL_AMMO_COST (10), and reload 2 means it fires every other round --
    # banking the full X+1 covers the worst case where all of them are ready
    # together.
    #
    # A sentinel that cannot fire is 30 Ti of statue: the whole cost model in
    # rush.py assumes 9 dmg/round each, and it silently becomes false the moment
    # the ammunition runs dry. This is the line that keeps the model honest.
    siege_n = comms.siege_sentinels()
    if siege_n:
        target_ammo_ammount = GameConstants.SENTINEL_AMMO_COST * (siege_n + 1)
    else:
        # No siege live yet: the old defensive trickle, which pays for whatever
        # turrets we put up at home.
        target_ammo_ammount = min(30, rc.get_current_round() * 2)
    # Never convert past the heal reserve, whatever the siege wants.
    target_ammo_ammount = min(target_ammo_ammount,
                              rc.get_global_ammo()
                              + max(0, rc.get_global_resources() - map_info.ti_reserve()))

    ammo_amount = target_ammo_ammount - rc.get_global_ammo()
    if ammo_amount > 0 and rc.can_convert_ammo(ammo_amount):
        rc.convert_ammo(ammo_amount)
