"""Map geometry: where the enemy core is, how to walk there, where a sentinel
can stand and still hit it.

Everything here is pure arithmetic over remembered terrain. No unit shares state
with any other -- each runs in its own interpreter -- so every function is
written to be recomputed from scratch by whoever needs it, and the two facts that
MUST agree between units (which symmetry, hence where the enemy core is) are
resolved by a fixed priority order rather than by anything position-dependent.
"""

from fcode import Direction, EntityType, Environment, Position

# Buildings a builder bot may stand on. Everything else is solid.
_WALKABLE = frozenset({EntityType.CONVEYOR, EntityType.SPLITTER})

# Symmetry candidates, in the fixed priority order every unit applies. A map is
# symmetric by reflection or rotation, so our core's 2x2 top-left corner maps to
# the enemy's under exactly one of these. Rotation leads because it is the common
# case; what matters more is that the ORDER is the same everywhere, so two units
# that have seen different tiles still pick the same survivor.
ROT180, MIRROR_V, MIRROR_H = 0, 1, 2
CANDIDATES = (ROT180, MIRROR_V, MIRROR_H)

_CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)

# A sentinel's line of fire: 5 tiles along a cardinal, 4 along a diagonal,
# single-tile wide, and it ignores everything in between. That last part is why
# this is pure geometry -- there is no line of sight to check.
SENTINEL_CARDINAL_REACH = 5
SENTINEL_DIAGONAL_REACH = 4


class Board:
    """Remembered terrain plus the live symmetry hypothesis.

    Terrain is remembered rather than queried because `get_tile_env` is only
    meaningful inside vision, and the rusher spends forty turns walking through
    country it has already left behind.
    """

    def __init__(self, ct):
        self.w = ct.get_map_width()
        self.h = ct.get_map_height()
        self.env = {}                       # (x, y) -> Environment
        # Tiles a building has been seen on. Walls are not the only thing that
        # stops a builder: the whole point of walking to the OUTSIDE face of the
        # enemy core is that the route goes round their base, and a path planner
        # that only avoids walls plans straight through it. On antler that left
        # the rusher jammed against the enemy core for 48 rounds having built
        # nothing at all.
        self.occupied = set()
        self.live = set(CANDIDATES)
        self.my_core = None                 # top-left of our 2x2
        self.their_core = None              # confirmed by sight, if ever

    # --- terrain ----------------------------------------------------------
    def observe(self, ct):
        """Record every tile in vision, then use the new tiles to kill off
        symmetry candidates whose mirror image disagrees with what we can see."""
        for p in ct.get_nearby_tiles():
            key = (p.x, p.y)
            try:
                self.env[key] = ct.get_tile_env(p)
            except Exception:
                continue
            # Refreshed rather than accumulated: a building we watched die must
            # stop blocking, and this is the only place we can tell.
            #
            # CONVEYORS AND SPLITTERS DO NOT BLOCK. A builder can stand on one --
            # it is the belt that carries titanium, not a wall -- and an enemy
            # base is mostly belt. Treating every building as solid walled the
            # rusher out of the very region it is trying to reach: on atoll it
            # stood at (13,4) from turn 27 to the end of the game because the
            # only route to its chosen site ran across three enemy conveyors.
            try:
                bid = ct.get_tile_building_id(p)
                if bid is None or ct.get_entity_type(bid) in _WALKABLE:
                    self.occupied.discard(key)
                else:
                    self.occupied.add(key)
            except Exception:
                pass
        self._prune()

    def _prune(self):
        """Drop any candidate whose implied mirror contradicts observed terrain.

        Only tiles where BOTH the tile and its image have been seen can say
        anything, which is why this converges as the rusher crosses the middle of
        the map: near a mirror axis a tile and its image are both in vision at
        once. Walls are the useful signal because they are the rarest.
        """
        if len(self.live) <= 1:
            return
        for cand in tuple(self.live):
            for (x, y), e in self.env.items():
                ix, iy = self._image(cand, x, y)
                other = self.env.get((ix, iy))
                if other is not None and other != e:
                    self.live.discard(cand)
                    break

    def _image(self, cand, x, y):
        if cand == ROT180:
            return self.w - 1 - x, self.h - 1 - y
        if cand == MIRROR_V:
            return x, self.h - 1 - y
        return self.w - 1 - x, y

    # --- the enemy core ---------------------------------------------------
    def enemy_core(self):
        """Top-left of the enemy core's 2x2, or None while it is unknowable.

        A core is 2x2, so its top-left corner does NOT map to the image of our
        top-left corner -- the image of our whole 2x2 block has its top-left at
        the image of whichever of our corners ends up furthest north-west. Doing
        it by taking the min over the block's four images is shorter than
        case-splitting on the symmetry.
        """
        if self.their_core is not None:
            return self.their_core
        if self.my_core is None or not self.live:
            return None
        cand = next(c for c in CANDIDATES if c in self.live)
        xs, ys = [], []
        for dx in (0, 1):
            for dy in (0, 1):
                ix, iy = self._image(cand, self.my_core.x + dx, self.my_core.y + dy)
                xs.append(ix)
                ys.append(iy)
        return Position(min(xs), min(ys))

    def settled(self):
        return self.their_core is not None or len(self.live) == 1

    def core_tiles(self, top_left):
        return [Position(top_left.x + dx, top_left.y + dy)
                for dx in (0, 1) for dy in (0, 1)]

    # --- helpers ----------------------------------------------------------
    def in_bounds(self, x, y):
        return 0 <= x < self.w and 0 <= y < self.h

    def is_wall(self, x, y):
        return self.env.get((x, y)) == Environment.WALL

    def passable_guess(self, x, y):
        """Unknown tiles count as passable; walls and seen buildings do not.

        Being optimistic about unseen ground is right for a unit whose whole job
        is to cross the map: a pessimistic guess makes it refuse routes it has
        simply not looked at yet, and the cost of being wrong is one wasted step
        when the wall comes into view. Tiles we HAVE seen a building on are a
        different matter -- those are known-blocked, and pretending otherwise is
        what jammed the rusher against the enemy base.
        """
        if not self.in_bounds(x, y) or self.is_wall(x, y):
            return False
        return (x, y) not in self.occupied


# Neighbour orders for a staircase, indexed by (east?, south?, x-axis longer?).
# Precomputed because this is chosen once per tile per flood -- building the
# tuple inline was 3600 allocations a turn against a 10ms budget.
def _stair_orders():
    N, E, S, W = (Direction.NORTH, Direction.EAST, Direction.SOUTH,
                  Direction.WEST)
    out = {}
    for east in (False, True):
        for south in (False, True):
            for xlong in (False, True):
                h, v = (E if east else W), (S if south else N)
                out[(east, south, xlong)] = ((h, v, v.opposite(), h.opposite())
                                             if xlong else
                                             (v, h, h.opposite(), v.opposite()))
    return out


_STAIR = _stair_orders()


def flood(board, start, toward=None):
    """Every tile reachable from start: tile -> (distance, first cardinal step).

    One flood per turn replaces one BFS per candidate target, and it answers the
    question the per-target version could not: which of the forty-odd sentinel
    sites can this builder actually GET to. Ranking sites by Manhattan distance
    and then pathing to the winner deadlocks the moment the winner is behind a
    wall -- the rusher stands still forever rather than taking the reachable
    second choice.
    """
    s = (start.x, start.y)
    out = {s: (0, None)}
    frontier = [s]
    dist = 0
    tx = ty = None
    if toward is not None:
        tx, ty = toward.x, toward.y
    while frontier:
        dist += 1
        nxt = []
        for cur in frontier:
            first = out[cur][1]
            # Expand toward the target first, longer axis leading.
            #
            # Every shortest path in an open grid has the same LENGTH, so this
            # looks like it should not matter -- but BFS gives a tile its first
            # step from whichever parent reaches it first, and a fixed N/E/S/W
            # order makes that parent the one that ran along a single axis. The
            # result is an L: all the x, then all the y. An L is far more likely
            # to meet a wall broadside than a staircase is, and every time it
            # does the route is replanned from a worse place. Measured against
            # ph, our walk cost +12, +5 and +31 turns of overhead on three maps
            # where not adgato paid +0, +2 and +17.
            if tx is None:
                order = _CARDINALS
            else:
                rx, ry = tx - cur[0], ty - cur[1]
                order = _STAIR[(rx > 0, ry > 0, abs(rx) >= abs(ry))]
            for d in order:
                dx, dy = d.delta()
                n = (cur[0] + dx, cur[1] + dy)
                if n in out or not board.passable_guess(*n):
                    continue
                out[n] = (dist, first if first is not None else d)
                nxt.append(n)
        frontier = nxt
    return out


def bfs_step(board, start, targets, blocked=()):
    """First cardinal step of a shortest path from start to any target tile.

    Builder bots move cardinally only, so this is a plain 4-neighbour BFS over
    a board of at most 900 tiles -- cheap enough to redo every turn, which is
    what we want, because the terrain we know changes every turn.
    """
    if not targets:
        return None
    goal = {(p.x, p.y) for p in targets}
    s = (start.x, start.y)
    if s in goal:
        return Direction.CENTRE
    blocked = set(blocked)
    seen = {s}
    # Each frontier entry carries the FIRST step that led to it, so the answer
    # falls out without reconstructing a path.
    frontier = []
    for d in _CARDINALS:
        dx, dy = d.delta()
        n = (s[0] + dx, s[1] + dy)
        if n in seen or n in blocked or not board.passable_guess(*n):
            continue
        if n in goal:
            return d
        seen.add(n)
        frontier.append((n, d))
    while frontier:
        nxt = []
        for (x, y), first in frontier:
            for d in _CARDINALS:
                dx, dy = d.delta()
                n = (x + dx, y + dy)
                if n in seen or n in blocked or not board.passable_guess(*n):
                    continue
                if n in goal:
                    return first
                seen.add(n)
                nxt.append((n, first))
        frontier = nxt
    return None


def sentinel_sites(board, core_top_left):
    """Every (tile, facing) from which a sentinel would hit the enemy core.

    Walks backwards down each of the eight lines out of each core tile: if a
    sentinel `k` steps away faces back along that line, its shot lands on the
    core. Returns tiles that are in bounds and not known walls; whether one is
    actually free to build on is a live question and is checked at build time.
    """
    out = []
    seen = set()
    for tile in board.core_tiles(core_top_left):
        for d in Direction:
            if d == Direction.CENTRE:
                continue
            dx, dy = d.delta()
            reach = (SENTINEL_CARDINAL_REACH if (dx == 0 or dy == 0)
                     else SENTINEL_DIAGONAL_REACH)
            for k in range(1, reach + 1):
                # Stand k steps back along d, then face d to shoot down it.
                x, y = tile.x - dx * k, tile.y - dy * k
                if not board.in_bounds(x, y) or board.is_wall(x, y):
                    continue
                if (x, y, d) in seen:
                    continue
                seen.add((x, y, d))
                out.append((Position(x, y), d))
    return out


def away_from_centre(board, p):
    """How far p is from the middle of the map, squared.

    Used to prefer the far side of the enemy core. Every replay of this rush
    sets up on the outside -- the corner side -- because that is where the
    defender has no conveyors, no harvesters and no reason for a builder to be,
    so the sentinels get their fourteen rounds unmolested.
    """
    cx, cy = (board.w - 1) / 2.0, (board.h - 1) / 2.0
    return (p.x - cx) ** 2 + (p.y - cy) ** 2


def core_face_tiles(top_left):
    """The eight tiles ORTHOGONALLY adjacent to a 2x2 core.

    These are the only tiles a builder can heal that core from -- heal needs
    orthogonal adjacency, so a builder on a diagonal corner cannot reach it.
    Wall these eight off and the core can never be repaired again, which is the
    whole point of the barrier ring.
    """
    x, y = top_left.x, top_left.y
    return [Position(x, y - 1), Position(x + 1, y - 1),
            Position(x, y + 2), Position(x + 1, y + 2),
            Position(x - 1, y), Position(x - 1, y + 1),
            Position(x + 2, y), Position(x + 2, y + 1)]


def core_corner_tiles(top_left):
    """The four diagonal corners of the ring: where a builder stands to wall it.

    Each corner is orthogonally adjacent to exactly two of the eight face tiles,
    so four standing positions cover all eight -- and a corner is never itself a
    face tile, so the builder never has to barrier the ground under its feet.
    """
    x, y = top_left.x, top_left.y
    return [Position(x - 1, y - 1), Position(x + 2, y - 1),
            Position(x - 1, y + 2), Position(x + 2, y + 2)]
