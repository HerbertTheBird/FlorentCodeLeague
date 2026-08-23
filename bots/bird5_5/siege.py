"""Sentinel siege geometry: where to stand to shoot the enemy Core.

Ported from the scratchpad note "Where a Sentinel can shoot a Core", whose
constants were read out of the engine with probe bots rather than the spec. The
three facts that make this cheap and exact:

  * A Sentinel's ray is PURE GEOMETRY -- 5 tiles along a cardinal facing, 4 along
    a diagonal, and NOTHING on the ray blocks it (probe: 1786/1786 rays firable
    through walls and buildings). So "can this tile shoot the Core?" is answered
    by translation alone, with no line-of-sight test.
  * A Sentinel never rotates (`rotate()` is gunner-only), so the facing chosen at
    build time is permanent. That is why class C uses the ONE-FACING rule.
  * A Sentinel may be built on empty ground or on ore, never on a wall.

Classes, in the note's terms:
  B "can hit the core"  -- some facing puts a Core tile on the ray. 80 on an open board.
  C "siege placement"   -- the SAME firing line also covers `need` delivery-ring
                           tiles, where need = 1 if the tile is itself on the ring
                           (it already denies one by standing there) and 2 otherwise.
                           40 on an open board. This is the PURPLE class, and the
                           one we prefer: it chips the Core and strangles delivery
                           on one line, instead of only chipping HP.

The four diagonal ring corners (-1,-1), (2,-1), (-1,2), (2,2) relative to the
Core anchor are deliberately NOT class C: from a corner one facing takes the Core
and a different facing rakes the ring, but no single facing does both, and the
facing is frozen at build time.

This module is pure geometry over map_info's bitmasks -- no Controller.
"""

import map_info

# Compass order matching map_info._DIRECTIONS; (dx, dy) with NORTH = (0, -1).
_DIRS8 = ((0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1))
_CARD4 = ((0, -1), (0, 1), (-1, 0), (1, 0))
_FOOT = ((0, 0), (1, 0), (0, 1), (1, 1))


def _reach(d) -> int:
    """Ray length: 5 cardinal, 4 diagonal.

    Both fall out of SENTINEL_VISION_RADIUS_SQ = 32 -- a cardinal step costs 1
    per unit of squared distance so 5**2 = 25 fits and 6**2 = 36 does not; a
    diagonal step costs 2, so 2 * 4**2 = 32 fits exactly and 2 * 5**2 = 50 does not.
    """
    return 5 if (d[0] == 0 or d[1] == 0) else 4


def core_tiles(anchor):
    """The 2x2 footprint from its north-west anchor (what get_position returns)."""
    return [(anchor[0] + dx, anchor[1] + dy) for dx, dy in _FOOT]


def delivery_ring(anchor, w, h):
    """The 8 tiles cardinally adjacent to the footprint -- the only tiles that can
    hand titanium to the Core, which is why a line covering them is worth more
    than one that merely chips HP."""
    foot = set(core_tiles(anchor))
    ring = set()
    for fx, fy in foot:
        for dx, dy in _CARD4:
            t = (fx + dx, fy + dy)
            if 0 <= t[0] < w and 0 <= t[1] < h and t not in foot:
                ring.add(t)
    return ring


def _ray(x, y, d, w, h):
    out = []
    for k in range(1, _reach(d) + 1):
        tx, ty = x + d[0] * k, y + d[1] * k
        if not (0 <= tx < w and 0 <= ty < h):
            break                       # rays clip at the map edge, nothing else
        out.append((tx, ty))
    return out


def placements(anchor, buildable_mask):
    """[(tile_index, (dx, dy) facing, is_siege, ring_hits)] for every tile that can
    shoot the enemy Core, best facing first, siege (purple) tiles first.

    `buildable_mask` is a bitmask of tiles a Sentinel could legally occupy -- the
    caller owns that policy (no walls, no existing buildings, no harvester tile).
    Sorted so the caller can simply walk the list: siege before plain-hit, then
    more ring coverage, then closer to the Core.
    """
    w, h = map_info._width, map_info._height
    foot = set(core_tiles(anchor))
    ring = delivery_ring(anchor, w, h)
    out = []
    # Only tiles within a ray's reach of the footprint can qualify, so scan the
    # 11x11 box around the 2x2 core rather than the whole board.
    for x in range(anchor[0] - 5, anchor[0] + 7):
        for y in range(anchor[1] - 5, anchor[1] + 7):
            if not (0 <= x < w and 0 <= y < h):
                continue
            if (x, y) in foot:
                continue
            n = x + y * w
            if not (buildable_mask >> n) & 1:
                continue
            need = 1 if (x, y) in ring else 2
            best = None
            for d in _DIRS8:
                tiles = _ray(x, y, d, w, h)
                if not any(t in foot for t in tiles):
                    continue            # this facing does not reach the Core
                hits = sum(1 for t in tiles if t in ring)
                cand = (hits >= need, hits, d)
                if best is None or cand[:2] > best[:2]:
                    best = cand
            if best is None:
                continue
            is_siege, hits, d = best
            out.append((n, d, is_siege, hits))
    cx, cy = anchor
    out.sort(key=lambda r: (not r[2], -r[3],
                            (r[0] % w - cx) ** 2 + (r[0] // w - cy) ** 2))
    return out


# Delta -> Direction, built from map_info's own table rather than by index. The
# two orderings happen to agree today; deriving it removes the chance that a
# reorder in either place silently aims every sentinel the wrong way.
_DELTA_TO_DIR = {d.delta(): d for d in map_info._DIRECTIONS}


def facing_for(delta):
    """The Direction a sentinel must be built with to fire along `delta`."""
    return _DELTA_TO_DIR[delta]


def hit_mask(anchor) -> int:
    """Bitmask of every tile that can shoot the enemy Core from SOME facing --
    class B, ignoring buildability.

    Used to count how much siege we already have. Counting "our sentinels inside
    a box around their core" instead was wrong in a way that silently cancelled
    the whole rush: home-defence sentinels the attack state happened to place
    within the box were counted as siege pieces, `have` reached `want`, and the
    rusher concluded its job was done without ever building one.
    """
    w, h = map_info._width, map_info._height
    foot = set(core_tiles(anchor))
    mask = 0
    for fx, fy in foot:
        for d in _DIRS8:
            for k in range(1, _reach(d) + 1):
                x, y = fx - d[0] * k, fy - d[1] * k
                if not (0 <= x < w and 0 <= y < h):
                    break
                if (x, y) in foot:
                    continue
                mask |= 1 << (x + y * w)
    return mask


def ring_index(anchor, w, h):
    """{tile_index: bit} over the (up to) 8 delivery-ring tiles, so a placement's
    coverage can be carried as a small bitmask and unioned in one OR."""
    return {p[0] + p[1] * w: 1 << i
            for i, p in enumerate(sorted(delivery_ring(anchor, w, h)))}


def placement_options(anchor, buildable_mask):
    """Every (tile, facing) whose ray reaches the enemy Core, with what that exact
    line covers of the delivery ring.

    Returns [(tile_index, facing_delta, ring_bits, is_siege, dist_sq_to_core)].

    One entry PER FACING, not per tile. `placements()` collapses each tile to its
    single best facing, which is the right answer when you are choosing tiles
    independently -- and the wrong input to a set-cover, where the point is that
    two sentinels should take DIFFERENT ring tiles. A tile whose best facing
    covers ring tiles {a,b} may also have a facing covering {c}, and if another
    placement already holds {a,b} then {c} is the one worth building.

    `is_siege` is the note's class C (purple) test applied to THIS facing: the
    same line covers the Core and `need` ring tiles, where need = 1 if the tile
    is itself on the ring and 2 otherwise.
    """
    w, h = map_info._width, map_info._height
    foot = set(core_tiles(anchor))
    ring = delivery_ring(anchor, w, h)
    rindex = ring_index(anchor, w, h)
    cx, cy = anchor
    out = []
    for x in range(anchor[0] - 5, anchor[0] + 7):
        if not (0 <= x < w):
            continue
        for y in range(anchor[1] - 5, anchor[1] + 7):
            if not (0 <= y < h) or (x, y) in foot:
                continue
            n = x + y * w
            if not (buildable_mask >> n) & 1:
                continue
            need = 1 if (x, y) in ring else 2
            for d in _DIRS8:
                tiles = _ray(x, y, d, w, h)
                if not any(t in foot for t in tiles):
                    continue
                bits = 0
                hits = 0
                for t in tiles:
                    if t in ring:
                        bits |= rindex[t[0] + t[1] * w]
                        hits += 1
                out.append((n, d, bits, hits >= need,
                            (x - cx) ** 2 + (y - cy) ** 2))
    return out
