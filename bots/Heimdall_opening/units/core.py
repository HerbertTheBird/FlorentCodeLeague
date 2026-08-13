from main import has_op
from fcode import Controller, Direction, Position, EntityType
import map_info
from log import log
import comms
import conveyor_plan
import pathing
from pathing import Pathing
import sys
rc: Controller

# --- Configurable ---
SCALE_MULT = 1
_starting_convs: list[tuple[int]] = []
_spawn_tiles: tuple[Position, ...] = ()   # tiles immediately surrounding the 2x2 core
nav: Pathing = None

def _compute_spawn_tiles():
    offset = [-1, 0, 1, 2]
    out = []
    for x in offset:
        for y in offset:
            if (x in (0, 1) and y in (0, 1)):
                continue
            pos = Position(map_info._my_pos.x + x, map_info._my_pos.y + y)
            if map_info.ground_at(pos.x, pos.y) != map_info._ENV_WALL:
                out.append(pos)
    return tuple(out)
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
            d = nav.bfs_dist(p, target)
            if best_d is None or d < best_d:
                best_d = d
                best = p
    if best is None:
        return False
    rc.spawn_builder(best)
    return True


def _spawn_toward_center() -> bool:
    """Spawn on the surrounding tile closest to map center."""
    center = Position(map_info._width // 2, map_info._height // 2)
    return _spawn_best_toward(center)

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
    all_convs = []
    for i, a in enumerate(nearby_ore):
        start = time.perf_counter()
        dist, edges, path_mask = solve_gst(a, avoid_mask)
        all_convs.append(edges)
        elapsed = time.perf_counter() - start
        print(f"solve_gst took {elapsed:.6f} seconds")
        if dist == inf:
            continue
        avoid_mask |= path_mask
        draw_edges(edges, colors[i][0], colors[i][1], colors[i][2])
        for j in a:
            rc.draw_indicator_dot(j, colors[i][0], colors[i][1], colors[i][2])
    all_convs.sort(key=len, reverse=True)
    return all_convs

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
    if root not in _spawn_tiles or not rc.can_spawn(root):
        return False
    rc.spawn_builder(root)
    budget = comms.core_plan_dfs_budget()
    dfs_bits = conveyor_plan.encode_dfs_bits(root, excluded_dir, adj, max_bits=budget)
    fit_nodes = budget // 3
    if len(adj) > fit_nodes:
        # The tree is larger than one slot can carry; encode_dfs_bits kept a
        # connected subtree of fit_nodes rooted at `root`, the rest is dropped.
        print(f"conveyor plan capped: {len(adj)} conveyors, only {fit_nodes} fit in slot 0")
    comms.queue_core_plan(dfs_bits)
    return True


def run():
    global _spawn_tiles, _starting_convs

    map_info.update()
    comms.read()    # absorb every slot's shared tiles/symmetry
    map_info.recompute_derived()

    if rc.get_current_round() == 0:
        import time

        start = time.perf_counter()

        _starting_convs = starting_convs()

        elapsed = time.perf_counter() - start
        print(f"total took {elapsed:.6f} seconds")

    if not _spawn_tiles:
        _spawn_tiles = _compute_spawn_tiles()

    # Opening: spawn the starting-conv builder on its tree root and hand it the
    # whole conveyor tree via slot 0. Queue it BEFORE the broadcast so this same
    # turn's write carries the plan (the builder reads it next turn).
    r = rc.get_current_round()
    spawned_conv = False
    if r < len(_starting_convs):
        spawned_conv = _spawn_starting_conv(r)

    comms.write()   # broadcast our word -- the queued plan if we spawned a root

    titanium = rc.get_global_resources()
    if (r < len(_starting_convs) and not spawned_conv
            and titanium >= rc.get_scale_percent() * SCALE_MULT):
        _spawn_toward_center()

    # Ammount of ammo we want to have
    target_ammo_ammount = min(min(30, rc.get_global_resources()), rc.get_current_round() * 2)

    # Convert titanium into ammo if we want more
    ammo_amount = target_ammo_ammount - rc.get_global_ammo()
    if ammo_amount > 0 and rc.can_convert_ammo(ammo_amount):
        rc.convert_ammo(ammo_amount)
