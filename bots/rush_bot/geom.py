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

# (dx, dy, direction) per cardinal, computed once. flood() walks up to ~900 tiles
# x 4 neighbours a turn, and calling Direction.delta() in that inner loop was pure
# overhead; this hoists it out.
_CARD_DELTAS = tuple(d.delta() + (d,) for d in _CARDINALS)

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


def flood(board, start):
    """Every tile reachable from start: tile -> (distance, first cardinal step).

    One flood per turn replaces one BFS per candidate target, and it answers the
    question the per-target version could not: which of the forty-odd sentinel
    sites can this builder actually GET to. Ranking sites by Manhattan distance
    and then pathing to the winner deadlocks the moment the winner is behind a
    wall -- the rusher stands still forever rather than taking the reachable
    second choice.
    """
    # Hoist everything the inner loop touches into locals and inline
    # passable_guess -- this loop runs a few thousand times a turn and a bound
    # method call plus its `*n` unpack per neighbour was the bot's single biggest
    # cost. Output is byte-for-byte identical: same tiles, same first-step dirs,
    # same tie-break order (frontier order x _CARD_DELTAS order, both unchanged).
    w = board.w
    h = board.h
    env = board.env
    occ = board.occupied
    WALL = Environment.WALL
    deltas = _CARD_DELTAS
    out = {(start.x, start.y): (0, None)}
    frontier = [(start.x, start.y, None)]      # (x, y, first-step direction)
    dist = 0
    while frontier:
        dist += 1
        nxt = []
        for cx, cy, first in frontier:
            for dx, dy, d in deltas:
                nx = cx + dx
                ny = cy + dy
                key = (nx, ny)
                if key in out:
                    continue
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                if env.get(key) == WALL or key in occ:
                    continue
                fd = d if first is None else first
                out[key] = (dist, fd)
                nxt.append((nx, ny, fd))
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
