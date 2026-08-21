from main import has_op
from fcode import Controller, Direction, Position, EntityType
import core_vitals
import map_info
from log import log
import comms
import conveyor_plan
import pathing
from pathing import Pathing
from units.spawn_plan import choose_fanout_plan, INITIAL_SPAWN_COUNT
from fcode import GameConstants
import sys
rc: Controller

# --- Configurable ---
SCALE_MULT = 0.8
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


def _wants_surplus_builder() -> bool:
    """Titanium is piling up faster than the team can spend it -- add hands.

    INITIAL_SPAWN_COUNT builders are spawned in the opening and the core then
    never spawns another for economic reasons, so a good map ends with a pile it
    had no way to convert: one sprint game finished having mined 9,410 titanium
    and banked 5,588 of it, on four builders, for a thousand rounds.

    Two gates, both derived rather than picked. "Piling up" is measured against
    what the team could spend THIS turn: every unit acts once and the dearest
    thing any of them builds is a Sentinel, so units * sentinel_cost is a hard
    ceiling on this turn's spending. And the crew must stay in ratio with the
    economy feeding it -- this bot measured that ratio and adopted it
    (defense.py: "crew ratio harvesters*2 >= builders  54.5% ADOPTED", against
    51.5% at *1 and 48.5% at *4), then lost it, because the gate lived in
    core_old.py and the rewrite dropped the whole post-opening spawn path.
    Titanium says we can afford a builder; the ratio says whether it will have
    anything to do.
    """
    if rc.get_unit_count() >= GameConstants.MAX_TEAM_UNITS:
        return False
    my = map_info._bm_team[map_info._my_team_idx]
    harvesters = (my & map_info._bm_et[map_info._IDX_HARVESTER]).bit_count()
    builders = map_info._bm_friendly_bots.bit_count() + 1   # count the one we would add
    if harvesters * 2 < builders + 1:
        return False
    # A genuinely lower bar: can we pay for the builder at all, on top of the
    # defensive reserve? The units * sentinel_cost ceiling asks whether the whole
    # team could spend the bank this turn, which is a conservative reading of
    # "not scarce". With the crew ratio already gating on whether a new builder
    # would have work, this asks only whether we can afford one.
    return rc.get_global_resources() >= rc.get_builder_bot_cost() + map_info.ti_reserve()


def _wants_more_healers() -> bool:
    """True while fewer than WANT_HEALERS friendly bots stand beside the core.

    Without the cap the core spends its whole distress spawning builders it then
    cannot afford to heal with: one a round, every round, for as long as it is
    hurt. Three is what it takes to gain on a single sentinel.
    """
    core_area = map_info._bm_my_core_area
    if not core_area:
        return False
    helpers = (map_info.manhattan(core_area)
               & map_info._bm_friendly_bots).bit_count()
    return helpers < core_vitals.WANT_HEALERS


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
    best = None
    best_key = None
    m = targets
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        x, y = n % w, n // w
        key = _dist_to_core(x, y)
        if best_key is None or key < best_key:
            best_key = key
            best = Position(x, y)
    return best


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
            n = (hit & -hit).bit_length() - 1
            best_spawn = Position(n % w, n // w)
            break
        frontier = map_info.expand_manhattan(frontier) & trav & ~visited
        visited |= frontier
    if best_spawn is None or not rc.can_spawn(best_spawn):
        return False
    rc.spawn_builder(best_spawn)
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
        log(f"solve_gst took {elapsed:.6f} seconds")
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
    if root not in _spawn_tiles or not rc.can_spawn(root):
        return False
    rc.spawn_builder(root)
    budget = comms.core_plan_dfs_budget()
    dfs_bits = conveyor_plan.encode_dfs_bits(root, excluded_dir, adj, max_bits=budget)
    fit_nodes = budget // 3
    if len(adj) > fit_nodes:
        # The tree is larger than one slot can carry; encode_dfs_bits kept a
        # connected subtree of fit_nodes rooted at `root`, the rest is dropped.
        log(f"conveyor plan capped: {len(adj)} conveyors, only {fit_nodes} fit in slot 0")
    comms.queue_core_plan(dfs_bits)
    return True


def run():
    global _spawn_tiles, _starting_convs, _fanout_dirs

    map_info.update()
    comms.read()    # absorb every slot's shared tiles/symmetry
    map_info.recompute_derived()

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
        log(f"total took {elapsed:.6f} seconds")

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

    comms.write()   # broadcast our word -- the queued plan if we spawned a root

    titanium = rc.get_global_resources()

    # On-demand defence takes priority over every economic/fan-out spawn: the
    # whole point of reserving a builder's cost (map_info.ti_reserve) is that
    # this spawn is always affordable the round it is needed. Only one builder
    # can spawn per turn, so this only fires when the opening didn't spawn a
    # conv-root this turn.
    # A core under fire is a defence emergency that _find_defense_target cannot
    # see: it only counts buildings below DEFENSE_HP_THRESHOLD (14), and the core
    # has 500 HP, so no amount of damage to the core has ever armed the reserve
    # or bought a defender. Distress is the missing case.
    distress = core_vitals.evaluate()
    defense_target = _find_defense_target()
    if defense_target is None and distress and _wants_more_healers():
        defense_target = map_info._my_core
    map_info.arm_reserve(defense_target is not None)
    spawned_defense = False
    if defense_target is not None and not spawned_conv:
        spawned_defense = _spawn_toward_adjacent(defense_target)

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
            elif _wants_surplus_builder():
                _spawn_toward_center()

    # How much ammo we want to hold.
    #
    # The TARGET stays 30 -- ammo powers the turrets we win with, and starving
    # the buffer is catastrophic (an arm capping it to ~10+burn measured -0.6646
    # vs herbert10, mining 10x more titanium and still losing 14-72).
    #
    # What changed is the DRAIN BOUND. It was `resources - 4`: spend the bank
    # down to FOUR titanium every round, with no reference to ti_reserve(). That
    # is why the median bank during core fights measured 3 Ti, why harvest fails
    # `cost > ti` beside ore that needs no new routing, and why attack was
    # refused for `cannot_afford_turret` 5963 times in a single game.
    #
    # Hold back one conveyor plus the standing reserve (~13 Ti). The window is
    # narrow in BOTH directions: reserving a whole harvester instead (~30-60 Ti)
    # measured -0.5584, because it starves the turrets.
    #
    # Measured, 86 official games each, seed 1, mirrored:
    #   vs herbert10      54-32  +0.2833 se 0.0979  (t = 2.89)
    #   vs Tyr_Jython     72-14  +0.6928  (herbert10: +0.4946)
    #   vs V6_earlysiege  61-25  +0.4540  (herbert10: +0.4650, even)
    _keep = rc.get_conveyor_cost() + map_info.ti_reserve()
    target_ammo_ammount = min(
        min(30, rc.get_global_resources() - _keep),
        rc.get_current_round() * 2,
    )

    # Convert titanium into ammo if we want more
    ammo_amount = target_ammo_ammount - rc.get_global_ammo()
    if ammo_amount > 0 and rc.can_convert_ammo(ammo_amount):
        rc.convert_ammo(ammo_amount)
