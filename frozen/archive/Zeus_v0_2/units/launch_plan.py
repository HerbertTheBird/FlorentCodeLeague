"""Central-launcher spawn/launch planning.

There is a single launcher near our core, built on the "launcher position": an
empty tile cardinally adjacent to a core spawn tile (so a freshly spawned builder
stands next to it and is picked up immediately). Among such tiles it is the one
whose throw range reaches the tile with the lowest BFS distance to a tile
adjacent to the enemy core. Every builder spawns beside it and is flung toward
its goal:
  - attack / generalist bots toward the enemy core (same heuristic), and
  - econ bots toward the core's closest-by-BFS-conveyor-distance titanium ore
    that has not already been assigned (1st econ -> closest, 2nd -> 2nd, ...).

All planning is deterministic from the shared board (walls + both cores), so the
core, the builders, and the launcher independently agree on the launcher tile and
the ore ranking.
"""

import comms
import map_info
from fcode import Position

# Launcher throw range (dist^2), used only for *planning* the launcher tile. The
# launcher itself throws using rc.get_attackable_tiles().
_THROW_RANGE_SQ = 26
_THROW_R = 5  # ceil(sqrt(26))


def _iter(mask: int):
    while mask:
        lsb = mask & -mask
        yield lsb.bit_length() - 1
        mask ^= lsb


def _nonwall() -> int:
    return map_info._board_mask & ~map_info._bm_env[map_info._IDX_ENV_WALL]


def _empty() -> int:
    """In-bounds, not wall, not a building."""
    return _nonwall() & ~map_info._bm_any_building


def _enemy_core():
    # Only the confirmed enemy core (set when the shared map loads), never the
    # early prediction — the launcher tile must be stable so the first unit builds
    # exactly one launcher and never chases a shifting position.
    return map_info._their_core


def _core_footprint_mask(core) -> int:
    w = map_info._width
    m = 0
    for x in (core.x, core.x + 1):
        for y in (core.y, core.y + 1):
            m |= 1 << (x + y * w)
    return m


def enemy_ring_mask() -> int:
    """Passable tiles one tile out from the enemy 2x2 core, leaving a one-tile
    gap — the Chebyshev-2 ring around the footprint rather than the immediately
    adjacent ring."""
    core = _enemy_core()
    if core is None:
        return 0
    w, h = map_info._width, map_info._height
    m = 0
    for x in range(core.x - 2, core.x + 4):
        for y in range(core.y - 2, core.y + 4):
            # skip the footprint AND its immediately-adjacent ring
            # (Chebyshev <= 1 of the 2x2 core) so only the one-out ring remains
            if core.x - 1 <= x <= core.x + 2 and core.y - 1 <= y <= core.y + 2:
                continue
            if 0 <= x < w and 0 <= y < h:
                m |= 1 << (x + y * w)
    return m & _nonwall()


def _bfs_dist(seed_mask: int, passable: int):
    """Bitmasked BFS: list dist[n] of cardinal-step movement distance from the
    seed set over passable tiles (-1 where unreached)."""
    dist = [-1] * (map_info._width * map_info._height)
    frontier = seed_mask & passable
    for i in _iter(frontier):
        dist[i] = 0
    visited = frontier
    d = 0
    while frontier:
        d += 1
        nxt = map_info.expand_manhattan(frontier) & passable & ~visited
        if not nxt:
            break
        visited |= nxt
        for i in _iter(nxt):
            dist[i] = d
        frontier = nxt
    return dist


def _spawn_area_mask(core) -> int:
    """Non-wall tiles on the core's IMMEDIATE ring (Chebyshev 1 of the 2x2
    footprint) — the tiles a builder is reliably spawned on. Keeping this tight
    means the launcher tile (a cardinal neighbour of one of these) sits right
    beside a real spawn tile, so the first unit builds it without moving. (The
    farther Chebyshev-2 corners the core also offers are not dependable spawn
    spots, which pushed the launcher a walk away.) Uses only the STATIC map
    (walls, not transient buildings) so every unit agrees on the same tile."""
    w, h = map_info._width, map_info._height
    m = 0
    for x in range(core.x - 1, core.x + 3):
        for y in range(core.y - 1, core.y + 3):
            if 0 <= x < w and 0 <= y < h:
                m |= 1 << (x + y * w)
    return m & _nonwall() & ~_core_footprint_mask(core)


# --- enemy-ring distance field, cached on (walls, enemy core) ------------------
_ering_key = None
_ering_dist = None


def _enemy_ring_dist():
    global _ering_key, _ering_dist
    core = _enemy_core()
    key = (map_info._bm_env[map_info._IDX_ENV_WALL], core.x, core.y) if core else None
    if key is None:
        return None
    if key != _ering_key:
        _ering_dist = _bfs_dist(enemy_ring_mask(), _nonwall())
        _ering_key = key
    return _ering_dist


# --- the launcher tile, cached on (my core, enemy core, walls) -----------------
_L_key = None
_L_pos = None


def launcher_position():
    """The launcher tile (Position) or None until both cores are known."""
    global _L_key, _L_pos
    my_core = map_info._my_core
    enemy_core = _enemy_core()
    if my_core is None or enemy_core is None:
        return None
    key = (my_core.x, my_core.y, enemy_core.x, enemy_core.y,
           map_info._bm_env[map_info._IDX_ENV_WALL])
    if key == _L_key:
        return _L_pos
    w, h = map_info._width, map_info._height
    dist = _enemy_ring_dist()
    spawn_area = _spawn_area_mask(my_core)
    # Candidate launcher tiles: non-wall tiles cardinally adjacent to a spawn tile
    # (static map only, so the choice is identical for every unit at any time).
    cand = (map_info.expand_manhattan(spawn_area)
            & _nonwall() & ~_core_footprint_mask(my_core))
    best = None
    best_key = None
    for L_n in _iter(cand):
        lx, ly = L_n % w, L_n // w
        reach = None
        for dx in range(-_THROW_R, _THROW_R + 1):
            for dy in range(-_THROW_R, _THROW_R + 1):
                if dx * dx + dy * dy > _THROW_RANGE_SQ:
                    continue
                tx, ty = lx + dx, ly + dy
                if not (0 <= tx < w and 0 <= ty < h):
                    continue
                d = dist[tx + ty * w]
                if d < 0:
                    continue
                if reach is None or d < reach:
                    reach = d
        if reach is None:
            continue
        king, card = _adj_spawn_counts(lx, ly, spawn_area, w, h)
        # Prefer a tile king-adjacent to >=3 spawn tiles (so several builders can
        # queue in pickup range) AND cardinally adjacent to one (so the first unit
        # can build it without moving). Relax gracefully on cramped maps.
        if card == 0:
            tier = 2
        elif king >= 3:
            tier = 0
        else:
            tier = 1
        own = dist[L_n]
        own = own if own >= 0 else 10 ** 9
        cand_key = (tier, reach, own, L_n)
        if best_key is None or cand_key < best_key:
            best_key = cand_key
            best = Position(lx, ly)
    _L_key = key
    _L_pos = best
    return best


def _adj_spawn_counts(lx, ly, spawn_area, w, h):
    """(king-adjacent, cardinally-adjacent) counts of spawn tiles around (lx,ly)."""
    king = card = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            nx, ny = lx + dx, ly + dy
            if 0 <= nx < w and 0 <= ny < h and (spawn_area >> (nx + ny * w)) & 1:
                king += 1
                if dx == 0 or dy == 0:
                    card += 1
    return king, card


# --- ore ranking by BFS distance from our core, cached --------------------------
_ore_key = None
_ore_rank = None


def ranked_ore_from_core():
    """Titanium ore tiles (Positions) sorted by BFS distance from our core,
    closest first — the order econ bots are assigned to."""
    global _ore_key, _ore_rank
    core = map_info._my_core
    if core is None:
        return []
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    key = (core.x, core.y, ore, map_info._bm_env[map_info._IDX_ENV_WALL])
    if key == _ore_key:
        return _ore_rank
    dist = _bfs_dist(_core_footprint_mask(core), _nonwall())
    w = map_info._width
    ranked = sorted(
        ((dist[n], n % w, n // w) for n in _iter(ore) if dist[n] >= 0)
    )
    _ore_rank = [Position(x, y) for _d, x, y in ranked]
    _ore_key = key
    return _ore_rank


# --- launcher-side throw destination selection ---------------------------------
def _pick(attackable, cost_fn):
    """The landable throw destination minimizing cost_fn(tile index)."""
    empty = _empty()
    bots = map_info._bm_friendly_bots | map_info._bm_enemy_bots
    w = map_info._width
    best = None
    best_c = None
    for t in attackable:
        n = t.x + t.y * w
        if not ((empty >> n) & 1) or ((bots >> n) & 1):
            continue
        c = cost_fn(n)
        if c is None or c < 0:
            continue
        if best_c is None or c < best_c:
            best_c = c
            best = t
    return best


def attack_dest(attackable):
    """Where to fling an attack/generalist bot: the reachable tile with the lowest
    BFS distance to the enemy core ring."""
    dist = _enemy_ring_dist()
    if dist is None:
        return None
    return _pick(attackable, lambda n: dist[n])


def econ_dest(attackable, ore_pos):
    """Where to fling an econ bot: the reachable tile closest (Manhattan) to its
    assigned ore."""
    w = map_info._width
    return _pick(
        attackable,
        lambda n: abs(n % w - ore_pos.x) + abs(n // w - ore_pos.y),
    )


# --- builder-side: build the launcher, or hold to be flung ---------------------
_MAX_LAUNCH_WAIT = 4
_launch_wait = 0


def ensure_launcher_or_wait(rc) -> bool:
    """Called by a builder before its normal behaviour.

    Only the FIRST unit (attacker 0) may build the launcher. It is spawned
    cardinally adjacent to the launcher tile, so it builds on turn one without
    moving; until it can (board not loaded / can't yet afford it) it HOLDS its
    tile instead of wandering off. Everyone else holds still while in pickup range
    so the launcher can fling them, and proceeds normally once flung away.

    Returns True if it consumed the turn."""
    global _launch_wait
    # Once the opening launcher has flung the whole roster and self-destructed,
    # there is nothing left to be launched by — never hold. Without this a
    # builder that walks back near the old launcher tile (econ bots route right
    # past the core) would wait forever for a launcher that no longer exists.
    if comms.launch_done():
        return False
    import units.builder as builder
    is_first = builder._atk_bot and builder._atk_index == 0

    L = launcher_position()
    if L is None:
        # Board not loaded yet: the first unit must stay put (still adjacent to
        # its spawn tile, where the launcher goes); everyone else carries on.
        return is_first

    my = map_info._my_pos
    L_n = L.x + L.y * map_info._width
    launcher_here = bool(
        (map_info._bm_et[map_info._IDX_LAUNCHER]
         & map_info._bm_team[map_info._my_team_idx]) & (1 << L_n)
    )
    cheb = max(abs(my.x - L.x), abs(my.y - L.y))

    if launcher_here:
        if cheb <= 1:                      # pickup range: hold to be flung
            _launch_wait += 1
            if _launch_wait <= _MAX_LAUNCH_WAIT:
                return True
            _launch_wait = 0               # never launched — give up, walk
            return False
        return False                       # already flung / far away: proceed
    _launch_wait = 0

    # No launcher yet — only the first unit builds it.
    if not is_first:
        return cheb <= 1                   # hold beside the tile, else proceed

    if abs(my.x - L.x) + abs(my.y - L.y) == 1:      # adjacent: build now, no move
        # Build the instant the engine allows it (no economy reserve gate) — the
        # first unit's whole job is to get this launcher up as fast as possible.
        if rc.can_build_launcher(L):
            rc.build_launcher(L)
        return True                        # hold on the spot (or retry next turn)
    # Displaced somehow — walk back to the launcher tile to build it.
    if builder.nav is not None:
        builder.nav.move_to(L)
    return True
