"""Conveyor-plan (de)serialisation for the opening.

The core knows a Steiner tree of conveyors connecting an ore group to its core
(see `core.solve_gst`). On the turn it spawns a builder at the tree's root (the
conveyor tile orthogonally adjacent to the core), it hands that builder the whole
tree through comms slot 0 -- see `comms.queue_core_plan` / `comms.read_core_plan`
for the word framing (marker + root position + excluded side). This module owns
just the DFS body: the 3-bit-per-conveyor stream.

Wire format (the DFS stream):
  A pre-order DFS from the root. Each conveyor node emits exactly 3 bits, one
  per side that is NOT the side it outputs to (its parent side). Directions are
  visited in the fixed order N, E, S, W with the parent side removed, so the
  three bits always line up for encoder and decoder. A set bit means "there is a
  conveyor on that side" -- the DFS then descends into it (its parent side is the
  way back). 000 is a leaf: no children, so the DFS unwinds to the nearest node
  with an unfilled side. No length field is needed; the tree structure is
  self-terminating (decoding stops when the root's recursion returns).

The decoded plan is `{Position: facing}` where `facing` is the direction the
conveyor outputs -- i.e. toward its parent, down-tree toward the core. That is
exactly the argument `build_conveyor(pos, facing)` wants.

Pure module: no Controller, no map_info. Everything is derived from the inputs,
so encoder (core) and decoder (builder) stay in lockstep.
"""

from collections import deque
from fcode import Direction, Position

# Fixed side order. Cardinal deltas per the compass convention (NORTH = (0,-1)).
CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)
CARD_INDEX = {d: i for i, d in enumerate(CARDINALS)}
_DELTA_TO_DIR = {d.delta(): d for d in CARDINALS}


def _pos(a) -> Position:
    return a if isinstance(a, Position) else Position(a[0], a[1])


def _step(pos: Position, d: Direction) -> Position:
    dx, dy = d.delta()
    return Position(pos.x + dx, pos.y + dy)


def _dir_from_to(a: Position, b: Position) -> Direction:
    """Cardinal direction from a to an orthogonally adjacent b."""
    return _DELTA_TO_DIR[(b.x - a.x, b.y - a.y)]


def _order_excluding(parent_dir: Direction):
    return [d for d in CARDINALS if d != parent_dir]


# --------------------------------------------------------------------------- #
# Build a conveyor-only tree (root, excluded side, adjacency) from a Steiner
# edge set + the core tiles. Core tiles are dropped from the tree: the root's
# parent side is the direction toward the core, and the plan describes only the
# conveyor tiles that hang off it.
# --------------------------------------------------------------------------- #
def build_tree(edges, core_tiles):
    """edges: iterable of (a, b), each a Position or (x, y) tuple, forming the
    tree. core_tiles: iterable of (x, y) for the core footprint. Returns
    (root: Position, excluded_dir: Direction, adj: dict[Position, set[Position]])
    or None if no conveyor tile touches the core."""
    core = {(_pos(t).x, _pos(t).y) for t in core_tiles}
    is_core = lambda p: (p.x, p.y) in core
    adj: dict = {}
    root = None
    excluded = None
    for a, b in edges:
        pa, pb = _pos(a), _pos(b)
        ca, cb = is_core(pa), is_core(pb)
        if ca and cb:
            continue                                   # edge inside the core
        if ca ^ cb:                                     # core <-> conveyor: a root
            conv = pb if ca else pa
            corep = pa if ca else pb
            if root is None:
                root = conv
                excluded = _dir_from_to(conv, corep)
            continue
        adj.setdefault(pa, set()).add(pb)              # conveyor <-> conveyor
        adj.setdefault(pb, set()).add(pa)
    if root is None:
        return None
    adj.setdefault(root, set())
    return root, excluded, adj


def _prune(root: Position, adj: dict, max_nodes: int) -> dict:
    """Connected subtree of <= max_nodes nodes from root (BFS, deterministic
    order), so the encoded stream fits the bit budget. Returns filtered adj."""
    if max_nodes <= 0:
        return {}
    keep = {root}
    q = deque((root,))
    while q and len(keep) < max_nodes:
        u = q.popleft()
        for v in sorted(adj.get(u, ()), key=lambda p: (p.y, p.x)):
            if v not in keep:
                keep.add(v)
                q.append(v)
                if len(keep) >= max_nodes:
                    break
    return {u: {v for v in adj.get(u, ()) if v in keep} for u in keep}


# --------------------------------------------------------------------------- #
# Encode / decode the DFS stream
# --------------------------------------------------------------------------- #
def encode_dfs_bits(root: Position, excluded_dir: Direction, adj: dict,
                    max_bits: int | None = None):
    """Pre-order 3-bit-per-node DFS stream (list of 0/1). If max_bits is given,
    the tree is first pruned to floor(max_bits/3) nodes so the stream fits."""
    if max_bits is not None:
        adj = _prune(root, adj, max_bits // 3)
    bits: list[int] = []
    visited: set = set()

    def dfs(node: Position, parent_dir: Direction):
        visited.add(node)
        order = _order_excluding(parent_dir)
        present = []
        for d in order:
            nbr = _step(node, d)
            p = nbr in adj.get(node, ()) and nbr not in visited
            present.append(p)
            bits.append(1 if p else 0)
        for d, p in zip(order, present):
            if p:
                dfs(_step(node, d), d.opposite())

    dfs(root, excluded_dir)
    return bits


def decode_dfs(root: Position, excluded_dir: Direction, bits):
    """Inverse of encode_dfs_bits. Returns {Position: facing} where facing is the
    conveyor's output direction (toward its parent / the core). Reads only the
    structural bits; any trailing padding in `bits` is ignored."""
    plan: dict = {}
    idx = [0]

    def take() -> int:
        i = idx[0]
        idx[0] += 1
        return bits[i] if 0 <= i < len(bits) else 0

    def dfs(node: Position, parent_dir: Direction):
        plan[node] = parent_dir
        order = _order_excluding(parent_dir)
        mask = (take(), take(), take())
        for d, b in zip(order, mask):
            if b:
                dfs(_step(node, d), d.opposite())

    dfs(root, excluded_dir)
    return plan
