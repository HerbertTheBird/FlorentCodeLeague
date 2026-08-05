"""Deterministic killbox plan: a protected gunner nest hugging our core.

Everything here is a pure function of the STATIC board (walls + both cores +
our core position), so the killbox builder, the launcher, and the gunner all
compute the SAME plan independently — no comms coordination needed.

Center: a passable tile ADJACENT to our 2x2 core (its Chebyshev-1 ring) with the
most impassible (wall / core / out-of-bounds) CARDINAL neighbours (4 counts the
same as 3), tie-broken by FARTHEST from the ENEMY core, then nearest to our core
(both Manhattan). A tile with all four cardinals impassible only qualifies if it
still has an empty diagonal (so it is reachable).

Build: at the center we wall in every OPEN cardinal side with a barrier — a
killbox — except the open cardinal nearest our core, which instead gets a gunner
facing inward (toward the center). If the center is already fully walled on all
four cardinals, the gunner goes on the nearest-by-BFS empty diagonal, facing
inward.
"""

import map_info
from fcode import Position

_CARDINALS = ((0, -1), (1, 0), (0, 1), (-1, 0))
_DIAGONALS = ((1, -1), (1, 1), (-1, 1), (-1, -1))


def _impassible_mask() -> int:
    """Walls and both cores — the tiles that are impassible for scoring and that
    a killbox wall never needs to be built on."""
    return (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_my_core_area
        | map_info._bm_their_core_area
    )


def _is_impassible(x: int, y: int, imp: int) -> bool:
    if not (0 <= x < map_info._width and 0 <= y < map_info._height):
        return True  # out of bounds blocks a flank just like a wall
    return bool((imp >> (x + y * map_info._width)) & 1)


def _cardinal_impassible(x: int, y: int, imp: int) -> int:
    return sum(1 for dx, dy in _CARDINALS if _is_impassible(x + dx, y + dy, imp))


def _manh_to_core(x: int, y: int) -> int:
    core = map_info._my_core
    return min(
        abs(x - cx) + abs(y - cy)
        for cx in (core.x, core.x + 1)
        for cy in (core.y, core.y + 1)
    )


def _manh_to_enemy(x: int, y: int, enemy: Position) -> int:
    return min(
        abs(x - cx) + abs(y - cy)
        for cx in (enemy.x, enemy.x + 1)
        for cy in (enemy.y, enemy.y + 1)
    )


def _select_center():
    """The killbox center tile — a tile adjacent to our core — or None."""
    core = map_info._my_core
    if core is None:
        return None
    enemy = map_info._predicted_enemy_core or map_info._their_core
    imp = _impassible_mask()
    w, h = map_info._width, map_info._height

    best = None
    best_key = None
    # Forced adjacent to the core: the Chebyshev-1 ring == the 4x4 box around the
    # 2x2 footprint, minus the footprint itself (skipped by the impassible check,
    # since our core is impassible).
    for x in range(core.x - 1, core.x + 3):
        for y in range(core.y - 1, core.y + 3):
            if not (0 <= x < w and 0 <= y < h):
                continue
            if _is_impassible(x, y, imp):
                continue  # center must be standable ground
            count = _cardinal_impassible(x, y, imp)
            if count == 4:
                # Fully walled on all cardinals — only usable if a diagonal is
                # open, otherwise nothing (not even the bot) can reach it.
                if not any(
                    not _is_impassible(x + dx, y + dy, imp) for dx, dy in _DIAGONALS
                ):
                    continue
            score = min(count, 3)  # 4 is the same as 3
            key = (
                score,
                (_manh_to_enemy(x, y, enemy) if enemy is not None else 0),  # farther from enemy core
                -_manh_to_core(x, y),                                       # then closer to our core
                -(x + y * w),
            )
            if best_key is None or key > best_key:
                best_key = key
                best = Position(x, y)
    return best


def _closest_diag_by_bfs(diags):
    """The empty diagonal nearest our core by BFS over passable tiles."""
    w = map_info._width
    imp = _impassible_mask()
    passable = map_info._board_mask & ~imp
    diag_mask = 0
    for x, y in diags:
        diag_mask |= 1 << (x + y * w)
    frontier = map_info._bm_my_core_area  # seed from the core, expand outward
    seen = frontier
    for _ in range(map_info._width + map_info._height):
        hit = frontier & diag_mask
        if hit:
            n = (hit & -hit).bit_length() - 1
            return (n % w, n // w)
        nxt = map_info.expand_manhattan(frontier) & passable & ~seen
        if not nxt:
            break
        seen |= nxt
        frontier = nxt
    return min(diags, key=lambda t: _manh_to_core(t[0], t[1]))


def _build_plan(center: Position):
    imp = _impassible_mask()
    cx, cy = center.x, center.y
    open_cardinals = [
        (cx + dx, cy + dy)
        for dx, dy in _CARDINALS
        if not _is_impassible(cx + dx, cy + dy, imp)
    ]
    if open_cardinals:
        # Gunner on the open cardinal nearest our core; barriers on the rest.
        gx, gy = min(open_cardinals, key=lambda t: (_manh_to_core(t[0], t[1]), t))
        gunner = Position(gx, gy)
        barriers = [Position(x, y) for x, y in open_cardinals if (x, y) != (gx, gy)]
    else:
        empty_diags = [
            (cx + dx, cy + dy)
            for dx, dy in _DIAGONALS
            if not _is_impassible(cx + dx, cy + dy, imp)
        ]
        if not empty_diags:
            return None
        gx, gy = _closest_diag_by_bfs(empty_diags)
        gunner = Position(gx, gy)
        barriers = []
    facing = map_info.direction_to(gunner, center)  # inward
    return {"center": center, "gunner": gunner, "facing": facing, "barriers": barriers}


# --- cached plan (static board -> compute once) --------------------------------
_cache_key = None
_cache = None


def plan():
    """The killbox plan dict {center, gunner, facing, barriers}, or None."""
    global _cache_key, _cache
    core = map_info._my_core
    if core is None:
        return None
    enemy = map_info._predicted_enemy_core or map_info._their_core
    key = (
        core.x, core.y,
        (enemy.x, enemy.y) if enemy is not None else None,
        map_info._bm_env[map_info._IDX_ENV_WALL],
        map_info._bm_my_core_area,
        map_info._bm_their_core_area,
    )
    if key == _cache_key:
        return _cache
    center = _select_center()
    _cache = _build_plan(center) if center is not None else None
    _cache_key = key
    return _cache


def gunner_tile():
    p = plan()
    return p["gunner"] if p else None


def is_killbox_gunner(pos: Position) -> bool:
    """True if a gunner at `pos` is (or would be) the killbox gunner."""
    g = gunner_tile()
    return g is not None and g.x == pos.x and g.y == pos.y


# --- build route: land far from the park spot, build toward it, finish there --
# The launcher throw costs the same at any range, so we choose the landing tile
# and the build order (a tiny TSP over the build stances) so the builder ends its
# last build right next to where it will park — no walk back afterwards.
_PARK_RANGE = 3

_route_cache_key = None
_route_cache = None


def _manhp(a: Position, b: Position) -> int:
    return abs(a.x - b.x) + abs(a.y - b.y)


def _park_anchor():
    """Deterministic point the parked bot heads to: a tile within _PARK_RANGE
    Manhattan of our core, nearest the enemy core (matches the park state)."""
    core = map_info._my_core
    if core is None:
        return None
    enemy = map_info._predicted_enemy_core or map_info._their_core
    imp = _impassible_mask()
    w = map_info._width
    zone = map_info.expand_manhattan(map_info._bm_my_core_area, _PARK_RANGE)
    zone &= map_info._board_mask & ~imp
    best = None
    best_d = None
    m = zone
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        x, y = n % w, n // w
        d = _manh_to_enemy(x, y, enemy) if enemy is not None else 0
        if best_d is None or d < best_d:
            best_d = d
            best = Position(x, y)
    return best


def _site_stances(site: Position, center: Position, imp: int, build_ns) -> list:
    """Static OUTSIDE cardinal stances to build `site` from (not the interior,
    not a build tile, not wall/core)."""
    out = []
    w, h = map_info._width, map_info._height
    for dx, dy in _CARDINALS:
        x, y = site.x + dx, site.y + dy
        if not (0 <= x < w and 0 <= y < h):
            continue
        if (x, y) == (center.x, center.y):
            continue
        n = x + y * w
        if (imp >> n) & 1 or n in build_ns:
            continue
        out.append(Position(x, y))
    return out


def build_route():
    """{order, stance_for, landing, anchor}: which stance to build each killbox
    piece from, and in what order, so the builder walks the SHORTEST total path
    and finishes next to the park spot. Deterministic — the launcher and the
    builder both use it. None until the killbox is known.

    Open-TSP over the build stances: the launcher lands the builder on the first
    stance for free (throw distance is free), so we minimise the walk between
    consecutive stances plus the final leg from the last stance to the park
    anchor. This keeps the path short (no crossing back and forth) while still
    ending near park.
    """
    global _route_cache_key, _route_cache
    p = plan()
    if p is None:
        return None
    if _cache_key == _route_cache_key:
        return _route_cache
    anchor = _park_anchor() or p["center"]
    center = p["center"]
    imp = _impassible_mask()
    w = map_info._width
    sites = [(p["gunner"], "gunner")] + [(b, "barrier") for b in p["barriers"]]
    build_ns = {s.x + s.y * w for s, _ in sites}

    # Candidate stances per site (fall back to the site itself if it has none, so
    # the cost math still works; the builder resolves a real stance at runtime).
    cand = []
    for site, _kind in sites:
        st = _site_stances(site, center, imp, build_ns)
        cand.append(st if st else [site])

    import itertools
    best = None
    best_cost = None
    for perm in itertools.permutations(range(len(sites))):
        for combo in itertools.product(*(cand[i] for i in perm)):
            cost = sum(_manhp(combo[k], combo[k + 1]) for k in range(len(combo) - 1))
            cost += _manhp(combo[-1], anchor)  # final leg to park (landing is free)
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best = (perm, combo)

    perm, combo = best
    order = [sites[i] for i in perm]
    stance_for = {}
    for k, i in enumerate(perm):
        s = sites[i][0]
        stance_for[(s.x, s.y)] = combo[k]
    landing = combo[0]

    _route_cache = {"order": order, "stance_for": stance_for, "landing": landing, "anchor": anchor}
    _route_cache_key = _cache_key
    return _route_cache


def landing_tile():
    """Where the launcher drops the killbox builder: the first stance of the
    build route (far from park, so it finishes near park). Always an outside
    stance — never the interior or a build tile."""
    route = build_route()
    return route["landing"] if route else None


def active() -> bool:
    """The killbox is only used where it's CHEAP: the chosen (core-adjacent) spot
    has at least TWO of its cardinal sides already walled by walls/cores, so the
    nest costs a gunner plus AT MOST ONE barrier to seal. (barriers == 3 - walled,
    or 0 when fully walled, so >=2 walled <=> <=1 barrier.) On every other map the
    feature is off — identical to baseline."""
    from _config import KILLBOX_ENABLED
    if not KILLBOX_ENABLED:
        return False
    p = plan()
    return p is not None and len(p["barriers"]) <= 1


def keep_clear_mask() -> int:
    """Tiles kept clear of harvesters and conveyor routes: the killbox tiles only
    (center + gunner + barrier sites) — so the trap centre stays empty for a
    launch and a conveyor can't take a build site before the builder raises it.

    Deliberately NOT the outward stances or a Chebyshev-1 ring: the killbox hugs
    our core, so reserving those tiles strangles the core's routing corridor and
    kills the economy. The builder occupies its own stance while building, so no
    conveyor can steal it anyway; a launch arcs over adjacent conveyors onto the
    (still-clear) centre; and the gunner sits cardinally adjacent to the centre
    with nothing between them."""
    p = plan()
    if p is None:
        return 0
    w = map_info._width
    return 1 << (p["center"].x + p["center"].y * w)


def is_good_landing(pos: Position) -> bool:
    """True if `pos` is ALREADY a valid build stance: cardinally adjacent to a
    killbox build site (gunner or barrier), and not the interior or a build tile.
    A builder standing here needs no launch — flinging it would only knock it off
    its stance a tile away from the placement."""
    p = plan()
    if p is None:
        return False
    w = map_info._width
    if (pos.x + pos.y * w) in no_land_tiles():
        return False  # interior or a build tile
    for s in [p["gunner"], *p["barriers"]]:
        if abs(pos.x - s.x) + abs(pos.y - s.y) == 1:  # cardinally adjacent to a site
            return True
    return False


def no_land_tiles() -> frozenset:
    """Tile indices the launcher must NOT drop the killbox builder onto: the
    killbox interior (center) and every build tile (gunner + barriers)."""
    p = plan()
    if p is None:
        return frozenset()
    w = map_info._width
    ns = {
        p["center"].x + p["center"].y * w,
        p["gunner"].x + p["gunner"].y * w,
    }
    for b in p["barriers"]:
        ns.add(b.x + b.y * w)
    return frozenset(ns)
