"""Precompute: standing at the origin, can A force-destroy at least one adjacent
target purely by attacking, despite B's healing?

The mini-game (all relative to A standing at the origin (0,0)):
  * A's turn: deal 2 damage to one cardinally adjacent target (conveyor/harvester).
  * B's turn: EITHER move to a cardinally adjacent passable tile, OR heal +4 HP to
    a target that BOTH A and B are cardinally adjacent to (A is at the origin, so
    that means any live target B stands next to). Healing is capped at max HP.
  * A wins the moment any target reaches 0 HP. B wins by keeping every target alive
    forever -- an infinite no-kill loop counts as a B win (safety game).

Geometry (A at origin):
  targets    : the 4 cardinals (1,0),(-1,0),(0,1),(0,-1)
               each is empty | wall(impassible) | conveyor(20hp,passable) |
               harvester(30hp,impassible)
  outer ring : (0,2),(1,2),(1,1),(2,1) x4 rotations = 16 tiles, each empty | wall
  heal spots : the 8 tiles cardinally adjacent to a target other than the origin,
               i.e. {(+-2,0),(0,+-2),(+-1,+-1)} -- each diagonal heals two targets,
               each far-cardinal heals one ("at most eight positions").
B is confined to this fixed cluster, so we track B's exact tile (no candidate-set
conjecture needed) and the solver is exact.

HP is always even (attack -2, heal +4, caps 20/30), so we store it in half-units.

Solved as a retrograde attractor (backward fixpoint from target-destroyed states),
which is the correct treatment of cycles: naive memoized DFS can wrongly memoize a
state as a B-win while it sits on the recursion stack.
"""

from collections import deque
from itertools import product

# ----------------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------------
ORIGIN = (0, 0)
TARGET_TILES = [(1, 0), (-1, 0), (0, 1), (0, -1)]
# outer ring: the four listed tiles rotated 90 degrees x4  (rot: (x,y)->(-y,x))
_OUTER_SEED = [(0, 2), (1, 2), (1, 1), (2, 1)]


def _rot(p):
    return (-p[1], p[0])


def _rotations(p):
    out = []
    for _ in range(4):
        out.append(p)
        p = _rot(p)
    return out


OUTER_TILES = sorted({r for seed in _OUTER_SEED for r in _rotations(seed)})
# heal spots = tiles cardinally adjacent to a target, excluding the origin
CARD = [(1, 0), (-1, 0), (0, 1), (0, -1)]
HEAL_SPOTS = sorted({(t[0] + d[0], t[1] + d[1])
                     for t in TARGET_TILES for d in CARD
                     if (t[0] + d[0], t[1] + d[1]) != ORIGIN}
                    & set(OUTER_TILES))
ALL_CELLS = sorted({ORIGIN, *TARGET_TILES, *OUTER_TILES})

# target type table: (max_hp, passable_for_B)
TYPES = {
    "empty": (0, True),
    "wall":  (0, False),
    "conv":  (20, True),
    "harv":  (30, False),
}


# ----------------------------------------------------------------------------
# Board built from a concrete terrain
# ----------------------------------------------------------------------------
class Board:
    """A concrete terrain instance and everything derived from it.

    terrain = {
        "cardinals": {tile: "empty"|"wall"|"conv"|"harv" for tile in TARGET_TILES},
        "outer":     {tile: "empty"|"wall" for tile in OUTER_TILES},
    }
    """

    def __init__(self, terrain):
        self.cardinals = dict(terrain["cardinals"])
        self.outer = dict(terrain["outer"])

        # passable set (B may stand here); origin is A's tile, never a B cell.
        passable = set()
        for t, typ in self.cardinals.items():
            if TYPES[typ][1]:
                passable.add(t)
        for t, typ in self.outer.items():
            if TYPES["empty" if typ == "empty" else "wall"][1]:
                passable.add(t)
        self.passable = passable  # B-occupiable tiles

        # movement adjacency among passable tiles (cardinal)
        self.adj = {p: [] for p in passable}
        for p in passable:
            for d in CARD:
                q = (p[0] + d[0], p[1] + d[1])
                if q in passable:
                    self.adj[p].append(q)

        # live targets (a building with HP) and their max HP in half-units
        self.targets = [t for t in TARGET_TILES
                        if self.cardinals[t] in ("conv", "harv")]
        self.maxhalf = {t: TYPES[self.cardinals[t]][0] // 2 for t in self.targets}

        # for each B tile, which targets it can heal (cardinally adjacent target)
        self.can_heal = {p: [] for p in passable}
        for p in passable:
            for t in self.targets:
                if abs(p[0] - t[0]) + abs(p[1] - t[1]) == 1:
                    self.can_heal[p].append(t)

        # per-target: B's shortest #moves to a tile from which it can heal that
        # target (used for the cheap exact test).  inf if unreachable.
        self.heal_dist = self._heal_distances()

    def _heal_distances(self):
        # multi-source BFS backward: sources = tiles that can heal target t
        dist = {}
        for t in self.targets:
            sources = [p for p in self.passable if t in self.can_heal[p]]
            d = {s: 0 for s in sources}
            q = deque(sources)
            while q:
                cur = q.popleft()
                for nb in self.adj[cur]:
                    if nb not in d:
                        d[nb] = d[cur] + 1
                        q.append(nb)
            dist[t] = d
        return dist


# ----------------------------------------------------------------------------
# Solver: retrograde attractor over states (hp_half tuple, b_tile, turn)
# ----------------------------------------------------------------------------
# A state is (hp, b, turn):
#   hp   : tuple of half-HP for board.targets, in the same order (each 0..maxhalf)
#   b    : B's tile (in board.passable)
#   turn : 0 = A to move, 1 = B to move
# A win  = some hp entry == 0.  Otherwise B win (can avoid forever).

def solve(board):
    if isinstance(board, dict):
        board = Board(board)
    targets = board.targets
    if not targets:
        return board, {}, "no targets: A can never destroy anything (B wins all)"

    ranges = [range(board.maxhalf[t] + 1) for t in targets]
    tiles = sorted(board.passable)

    # --- enumerate states, tag terminals (any hp == 0) ---
    # win[state] = True means A-win. Unknown/absent-after-fixpoint => B-win.
    A_WIN = {}
    # forward successors are generated on the fly; we build reverse edges for the
    # attractor and out-degree counters for B-nodes.
    preds = {}          # state -> list of predecessor states
    bcount = {}         # B-node state -> number of successors still not A-win
    terminals = []

    def succ(state):
        hp, b, turn = state
        out = []
        if turn == 0:  # A attacks one target for 2 (one half-unit)
            for i, t in enumerate(targets):
                if hp[i] == 0:
                    continue  # already dead (terminal); no move needed
                nhp = list(hp)
                nhp[i] -= 1
                out.append((tuple(nhp), b, 1))
        else:  # B moves or heals
            for nb in board.adj[b]:
                out.append((hp, nb, 0))
            for t in board.can_heal[b]:
                i = targets.index(t)
                if hp[i] < board.maxhalf[t]:
                    nhp = list(hp)
                    nhp[i] = min(hp[i] + 2, board.maxhalf[t])  # +4hp = +2 half
                    out.append((tuple(nhp), b, 0))
        return out

    # First pass: create every state, classify terminals, record reverse edges.
    all_states = []
    for hp in product(*ranges):
        is_term = any(h == 0 for h in hp)
        for b in tiles:
            for turn in (0, 1):
                s = (hp, b, turn)
                all_states.append(s)
                if is_term:
                    A_WIN[s] = True
                    terminals.append(s)

    for s in all_states:
        if s in A_WIN:
            continue  # terminal: no outgoing needed
        outs = succ(s)
        if s[2] == 1:
            bcount[s] = len(outs)
            if len(outs) == 0:
                # B has no legal response -> A wins (B is stuck)
                A_WIN[s] = True
                terminals.append(s)
        for o in outs:
            preds.setdefault(o, []).append(s)

    # --- attractor: propagate A-win backward ---
    q = deque(terminals)
    while q:
        x = q.popleft()
        for p in preds.get(x, ()):
            if p in A_WIN:
                continue
            if p[2] == 0:                       # A-node: one winning move suffices
                A_WIN[p] = True
                q.append(p)
            else:                               # B-node: needs ALL moves losing
                bcount[p] -= 1
                if bcount[p] == 0:
                    A_WIN[p] = True
                    q.append(p)

    # --- winner + a witnessing move for each non-terminal state ---
    result = {}
    for s in all_states:
        hp, b, turn = s
        if any(h == 0 for h in hp):
            continue  # terminal, not a queryable "situation"
        awin = s in A_WIN
        move = _witness(board, targets, s, A_WIN, succ)
        result[s] = ("A" if awin else "B", move)
    return board, result, None


def _witness(board, targets, state, A_WIN, succ):
    """A move that realizes the winner's guarantee, for readability/debugging."""
    hp, b, turn = state
    outs = succ(state)
    awin = state in A_WIN
    if turn == 0:  # A to move
        # A wants a successor that is A-win (or terminal). describe as target index.
        for i, t in enumerate(targets):
            if hp[i] == 0:
                continue
            nhp = list(hp); nhp[i] -= 1
            ns = (tuple(nhp), b, 1)
            if nhp[i] == 0 or ns in A_WIN:
                return ("attack", t)
        return ("attack", targets[0]) if targets else None
    else:  # B to move; if B-win, find a successor that is not A-win
        for nb in board.adj[b]:
            ns = (hp, nb, 0)
            if (ns not in A_WIN) == (not awin):
                if not awin:  # B-win: pick an escaping move
                    if ns not in A_WIN:
                        return ("move", nb)
        for t in board.can_heal[b]:
            i = targets.index(t)
            if hp[i] < board.maxhalf[t]:
                nhp = list(hp); nhp[i] = min(hp[i] + 2, board.maxhalf[t])
                ns = (tuple(nhp), b, 0)
                if not awin and ns not in A_WIN:
                    return ("heal", t)
        # A-win B-node (all moves lose) or nothing better
        return None


# ----------------------------------------------------------------------------
# Analysis API for the GUI: per-state winner + distance-to-forced-kill + optimal
# move selection.  (Same game as solve(), but exposes the raw structures.)
# ----------------------------------------------------------------------------
def successors(board, targets, state):
    """All legal successor states of `state=(hp, b, turn)`."""
    hp, b, turn = state
    out = []
    if turn == 0:                                  # A attacks one live target
        for i, t in enumerate(targets):
            if hp[i] == 0:
                continue
            nhp = list(hp); nhp[i] -= 1
            out.append((tuple(nhp), b, 1))
    else:                                          # B moves or heals
        for nb in board.adj[b]:
            out.append((hp, nb, 0))
        for t in board.can_heal[b]:
            i = targets.index(t)
            if hp[i] < board.maxhalf[t]:
                nhp = list(hp); nhp[i] = min(hp[i] + 2, board.maxhalf[t])
                out.append((tuple(nhp), b, 0))
    return out


def analyze(board):
    """Return (board, targets, tiles, a_win, rank).
    a_win[s]  = True iff A can force destroying a target from s.
    rank[s]   = plies until the forced kill under optimal play (A minimises, B
                maximises); absent for B-win states (B survives forever).
    """
    if isinstance(board, dict):
        board = Board(board)
    targets = board.targets
    tiles = sorted(board.passable)
    a_win, rank, preds, bcount, terminals = {}, {}, {}, {}, []
    if not targets:
        return board, targets, tiles, a_win, rank

    ranges = [range(board.maxhalf[t] + 1) for t in targets]
    all_states = []
    for hp in product(*ranges):
        term = any(h == 0 for h in hp)
        for b in tiles:
            for turn in (0, 1):
                s = (hp, b, turn)
                all_states.append(s)
                if term:
                    a_win[s] = True; rank[s] = 0; terminals.append(s)
    for s in all_states:
        if s in a_win:
            continue
        outs = successors(board, targets, s)
        if s[2] == 1:
            bcount[s] = len(outs)
            if not outs:
                a_win[s] = True; rank[s] = 0; terminals.append(s)
        for o in outs:
            preds.setdefault(o, []).append(s)
    q = deque(terminals)
    while q:                                        # FIFO -> rank = plies-to-kill
        x = q.popleft()
        for p in preds.get(x, ()):
            if p in a_win:
                continue
            if p[2] == 0:                           # A: first (min-rank) win found
                a_win[p] = True; rank[p] = rank[x] + 1; q.append(p)
            else:                                   # B: forced only when last succ falls
                bcount[p] -= 1
                if bcount[p] == 0:
                    a_win[p] = True; rank[p] = rank[x] + 1; q.append(p)
    return board, targets, tiles, a_win, rank


def optimal_move(board, targets, a_win, rank, state):
    """The successor an optimal mover picks: A drives toward the fastest kill, B
    toward survival (or, if losing, the longest delay). Returns a successor state
    or None if there are no moves."""
    outs = successors(board, targets, state)
    if not outs:
        return None
    INF = float("inf")
    r = lambda o: rank.get(o, INF)
    if state[2] == 0:                               # A to move
        if a_win.get(state):
            return min(outs, key=lambda o: (o not in a_win, r(o)))  # fastest kill
        return min(outs, key=lambda o: min(o[0]))   # can't win: focus the weakest
    else:                                           # B to move
        if a_win.get(state):
            return max(outs, key=r)                 # losing: delay as long as possible
        safe = [o for o in outs if o not in a_win]  # survive; among safe moves,
        return max(safe or outs, key=lambda o: sum(o[0]))  # defend (keep HP high)


def describe_move(targets, state, nxt):
    """Human string for the transition state -> nxt."""
    hp, b, turn = state
    nhp, nb, _ = nxt
    if turn == 0:
        for i, t in enumerate(targets):
            if nhp[i] < hp[i]:
                return f"A attacks {t}"
        return "A idles"
    if nb != b:
        return f"B moves to {nb}"
    for i, t in enumerate(targets):
        if nhp[i] > hp[i]:
            return f"B heals {t}"
    return "B waits"


# ----------------------------------------------------------------------------
# Best-move precompute (D4-symmetric) for O(1) optimal play at runtime.
# For every config (cardinals + diagonal-wall mask) we store, per (alive-HP,
# healer-cluster-tile), which target A should attack for the fastest forced kill
# (canonical target index; 255 = B-win). D4 symmetry folds the table ~8x.
# ----------------------------------------------------------------------------
_D4 = [
    lambda x, y: (x, y),   lambda x, y: (-y, x),  lambda x, y: (-x, -y), lambda x, y: (y, -x),
    lambda x, y: (x, -y),  lambda x, y: (-x, y),  lambda x, y: (y, x),   lambda x, y: (-y, -x),
]


def _perm(tiles, g):
    idx = {t: i for i, t in enumerate(tiles)}
    return [idx[g(*t)] for t in tiles]


_DIAG_OFF = [(1, 1), (-1, 1), (1, -1), (-1, -1)]   # == DIAGONALS (defined later)
_CPERM = [_perm(TARGET_TILES, g) for g in _D4]     # cardinal-position permutation per g
_DPERM = [_perm(_DIAG_OFF, g) for g in _D4]        # diagonal-position permutation per g
_TYPE_NAME = ["empty", "wall", "conv", "harv"]


def _xform_config(codes, dmask, gi):
    nc = [0, 0, 0, 0]
    for p in range(4):
        nc[_CPERM[gi][p]] = codes[p]
    nd = 0
    for j in range(4):
        if (dmask >> j) & 1:
            nd |= 1 << _DPERM[gi][j]
    return tuple(nc), nd


def canonicalize(codes, dmask):
    """Return (canon_codes, canon_dmask, gi) where gi maps the input to canonical."""
    best, bestg = None, 0
    for gi in range(8):
        key = _xform_config(tuple(codes), dmask, gi)
        if best is None or key < best:
            best, bestg = key, gi
    return best[0], best[1], bestg


def _board_from_codes(codes, dmask):
    cards = {t: _TYPE_NAME[codes[i]] for i, t in enumerate(TARGET_TILES)}
    outer = {t: "empty" for t in OUTER_TILES}
    for j, dg in enumerate(DIAGONALS):
        if (dmask >> j) & 1:
            outer[dg] = "wall"
    return Board(make_terrain(cardinals=cards, outer=outer))


def _bestmove_for_config(args):
    codes, dmask = args
    board, targets, tiles, a_win, rank = analyze(_board_from_codes(codes, dmask))
    if not targets:
        return None
    tlist = sorted(board.passable)
    maxhalfs = tuple(board.maxhalf[t] for t in targets)
    k = len(targets)
    nt = len(tlist)
    size = 1
    for m in maxhalfs:
        size *= m
    buf = bytearray(size * nt)
    for hp in product(*[range(1, m + 1) for m in maxhalfs]):
        hidx = 0
        for h, m in zip(hp, maxhalfs):
            hidx = hidx * m + (h - 1)
        base = hidx * nt
        for ti, t in enumerate(tlist):
            if (hp, t, 0) not in a_win:
                buf[base + ti] = 255
                continue
            best_i, best_v = 0, 1 << 30
            for i in range(k):
                nhp = list(hp)
                nhp[i] -= 1
                if nhp[i] == 0:
                    v = 0
                else:
                    v = rank.get((tuple(nhp), t, 1), 1 << 30)
                if v < best_v:
                    best_v, best_i = v, i
            buf[base + ti] = best_i
    return ((codes, dmask), (maxhalfs, tlist, bytes(buf)))


def build_bestmove_table(processes=None):
    from multiprocessing import Pool
    canon = set()
    for combo in product(range(4), repeat=4):
        if not any(c in (2, 3) for c in combo):
            continue
        for dmask in range(16):
            cc, cd, _ = canonicalize(combo, dmask)
            canon.add((cc, cd))
    tasks = sorted(canon)
    table = {}
    with Pool(processes) as pool:
        for r in pool.imap_unordered(_bestmove_for_config, tasks, chunksize=4):
            if r is not None:
                table[r[0]] = r[1]
    return table


def bestmove_lookup(table, codes, dmask, hp, htile):
    """Optimal target to attack. codes: 4 cardinal codes; hp: list in my-target order
    (targets are cardinals with code 2/3, ascending position); htile: healer's cluster
    offset (x,y) from the stand tile. Returns my-target-order index, or None."""
    cc, cd, gi = canonicalize(tuple(codes), dmask)
    entry = table.get((cc, cd))
    if entry is None:
        return None
    maxhalfs, tlist, buf = entry
    tidx = {t: i for i, t in enumerate(tlist)}
    gx, gy = _D4[gi](*htile)
    if (gx, gy) not in tidx:
        return None
    ti = tidx[(gx, gy)]
    my_T = [p for p in range(4) if codes[p] in (2, 3)]
    canon_T = [q for q in range(4) if cc[q] in (2, 3)]
    canon_hp = [0] * len(canon_T)
    for mi, p in enumerate(my_T):
        canon_hp[canon_T.index(_CPERM[gi][p])] = hp[mi]
    hidx = 0
    for h, m in zip(canon_hp, maxhalfs):
        if h < 1:
            h = 1
        elif h > m:
            h = m
        hidx = hidx * m + (h - 1)
    bm = buf[hidx * len(tlist) + ti]
    if bm == 255:
        return None
    canon_pos = canon_T[bm]
    for mi, p in enumerate(my_T):
        if _CPERM[gi][p] == canon_pos:
            return mi
    return None


# ----------------------------------------------------------------------------
# Convenience constructors
# ----------------------------------------------------------------------------
def make_terrain(cardinals=None, outer=None):
    """cardinals: dict tile->type (default all empty). outer: dict tile->type."""
    c = {t: "empty" for t in TARGET_TILES}
    if cardinals:
        c.update(cardinals)
    o = {t: "empty" for t in OUTER_TILES}
    if outer:
        o.update(outer)
    return {"cardinals": c, "outer": o}


# ----------------------------------------------------------------------------
# Guaranteed-win filter (the O(1) runtime check)
# ----------------------------------------------------------------------------
# For each cardinal config (the 4 neighbour types, in TARGET_TILES order) we
# precompute, over an OPEN outer ring, the set of HP vectors for which A wins
# from EVERY B position with A to move. Two soundness facts make this an exact
# lower bound for real board positions:
#   * outer walls only remove B options (heal spots / paths) -> only help A, so
#     "wins with open surroundings" implies "wins with any real surroundings";
#   * quantifying over all B positions covers wherever the enemy healer actually
#     is. So a tile that passes is a guaranteed kill; the full on-arrival solve
#     may additionally find wins this conservative filter misses.
# Stored per config as a bitset over a mixed-radix HP index (half-HP 1..maxhalf).

TYPE_CODE = {"empty": 0, "wall": 1, "conv": 2, "harv": 3}
CODE_TYPE = {v: k for k, v in TYPE_CODE.items()}


def _hp_index(hp_halves, maxhalfs):
    idx = 0
    for h, m in zip(hp_halves, maxhalfs):
        idx = idx * m + (h - 1)          # h in 1..m  ->  h-1 in 0..m-1
    return idx


def build_guaranteed_filter():
    """{config_codes: (maxhalfs, bitset_bytes)} over all cardinal configs."""
    filt = {}
    for combo in product(("empty", "wall", "conv", "harv"), repeat=4):
        cards = {t: typ for t, typ in zip(TARGET_TILES, combo)}
        board, result, _ = solve(make_terrain(cardinals=cards))
        if not board.targets:
            continue
        tiles = sorted(board.passable)
        maxhalfs = tuple(board.maxhalf[t] for t in board.targets)
        size = 1
        for m in maxhalfs:
            size *= m
        bits = bytearray((size + 7) // 8)
        for hp in product(*[range(1, m + 1) for m in maxhalfs]):
            if all(result[(hp, b, 0)][0] == "A" for b in tiles):
                idx = _hp_index(hp, maxhalfs)
                bits[idx >> 3] |= 1 << (idx & 7)
        filt[tuple(TYPE_CODE[c] for c in combo)] = (maxhalfs, bytes(bits))
    return filt


# --- Barrier-aware filter -------------------------------------------------------
# A builder may convert up to N empty tiles in the 3x3 around its stand tile into
# walls (barriers).  Those 8 tiles are the 4 cardinals + the 4 diagonals, and the
# 4 diagonals ARE 4 of the 8 heal spots.  So a barrier is just "set a cardinal or
# a diagonal to wall" -- a config transform.  We therefore precompute the
# guaranteed-win filter over ALL (cardinal config x diagonal-wall pattern), keeping
# the far heal spots (+-2,0)/(0,+-2) and the knight tiles OPEN (sound lower bound:
# real walls there only help A).  At runtime any barrier placement maps to a lookup
# on the transformed (cardinal_codes, diagonal_mask) key.
DIAGONALS = [(1, 1), (-1, 1), (1, -1), (-1, -1)]   # diagonal-mask bit order


def _solve_barrier_config(args):
    combo, dmask = args
    cards = {t: typ for t, typ in zip(TARGET_TILES, combo)}
    outer = {t: "empty" for t in OUTER_TILES}
    for j, dg in enumerate(DIAGONALS):
        if dmask & (1 << j):
            outer[dg] = "wall"
    board, result, _ = solve(make_terrain(cardinals=cards, outer=outer))
    if not board.targets:
        return None
    tiles = sorted(board.passable)
    maxhalfs = tuple(board.maxhalf[t] for t in board.targets)
    size = 1
    for m in maxhalfs:
        size *= m
    bits = bytearray((size + 7) // 8)
    for hp in product(*[range(1, m + 1) for m in maxhalfs]):
        if all(result[(hp, b, 0)][0] == "A" for b in tiles):
            idx = _hp_index(hp, maxhalfs)
            bits[idx >> 3] |= 1 << (idx & 7)
    return ((tuple(TYPE_CODE[c] for c in combo), dmask), (maxhalfs, bytes(bits)))


def build_barrier_filter(processes=None):
    """{(cardinal_codes, diagonal_mask): (maxhalfs, bitset)} over all 256x16 configs."""
    from multiprocessing import Pool
    tasks = [(combo, dmask)
             for combo in product(("empty", "wall", "conv", "harv"), repeat=4)
             for dmask in range(16)]
    filt = {}
    with Pool(processes) as pool:
        for r in pool.imap_unordered(_solve_barrier_config, tasks, chunksize=8):
            if r is not None:
                filt[r[0]] = r[1]
    return filt


def wins_barrier(filt, cardinal_codes, dmask, halfhp):
    """O(1): does A guarantee a kill for config (cardinal_codes, diagonal_mask=dmask)
    at the given half-HPs (one per conv/harv neighbour, in cardinal order)?"""
    entry = filt.get((tuple(cardinal_codes), dmask))
    if entry is None:
        return False
    maxhalfs, bits = entry
    hp = []
    j = 0
    for code in cardinal_codes:
        if code in (2, 3):
            h = halfhp[j]; j += 1
            if h < 1:
                return True
            hp.append(min(h, maxhalfs[len(hp)]))
    idx = _hp_index(hp, maxhalfs)
    return bool(bits[idx >> 3] & (1 << (idx & 7)))


# ---- pickle-free serialization for the chip tables --------------------------
# The competition sandbox runs the pure-Python unpickler with `memoryview` removed
# from builtins, so pickle.load raises "name 'memoryview' is not defined" for these
# files regardless of protocol. Both tables contain only {dict, tuple, list, int,
# bytes}, so we hand-roll a tiny tagged binary format that touches none of that --
# just int.to_bytes/from_bytes and byte slicing. Tags: 0 int (len-prefixed, signed),
# 1 bytes, 2 tuple, 3 dict, 4 list; 2/3/4 are 4-byte-count-prefixed.
def _enc(o, out):
    if isinstance(o, int):                       # NB: no bool in these tables
        b = o.to_bytes((o.bit_length() + 8) // 8 or 1, "little", signed=True)
        out.append(0); out.append(len(b)); out += b
    elif isinstance(o, (bytes, bytearray)):
        out.append(1); out += len(o).to_bytes(4, "little"); out += o
    elif isinstance(o, tuple):
        out.append(2); out += len(o).to_bytes(4, "little")
        for e in o:
            _enc(e, out)
    elif isinstance(o, dict):
        out.append(3); out += len(o).to_bytes(4, "little")
        for k, v in o.items():
            _enc(k, out); _enc(v, out)
    elif isinstance(o, list):
        out.append(4); out += len(o).to_bytes(4, "little")
        for e in o:
            _enc(e, out)
    else:
        raise TypeError(f"chip table has unsupported type {type(o).__name__}")


def _dec(buf, i):
    tag = buf[i]; i += 1
    if tag == 0:
        n = buf[i]; i += 1
        return int.from_bytes(buf[i:i + n], "little", signed=True), i + n
    if tag == 1:
        n = int.from_bytes(buf[i:i + 4], "little"); i += 4
        return bytes(buf[i:i + n]), i + n
    if tag in (2, 4):
        n = int.from_bytes(buf[i:i + 4], "little"); i += 4
        out = []
        for _ in range(n):
            e, i = _dec(buf, i)
            out.append(e)
        return (tuple(out) if tag == 2 else out), i
    if tag == 3:
        n = int.from_bytes(buf[i:i + 4], "little"); i += 4
        d = {}
        for _ in range(n):
            k, i = _dec(buf, i)
            v, i = _dec(buf, i)
            d[k] = v
        return d, i
    raise ValueError(f"bad chip-table tag {tag} at offset {i - 1}")


def save_filter(filt, path):
    out = bytearray()
    _enc(filt, out)
    with open(path, "wb") as f:
        f.write(bytes(out))


def load_filter(path):
    with open(path, "rb") as f:
        buf = f.read()
    obj, _ = _dec(buf, 0)
    return obj


def wins(filt, neighbour_codes, neighbour_halfhp):
    """O(1) query. neighbour_codes: 4 codes (0/1/2/3) for E,W,N,S in TARGET_TILES
    order. neighbour_halfhp: dict/list giving current half-HP for the conv/harv
    neighbours (same order); values clamped into 1..maxhalf by the caller or here.
    Returns True iff A is guaranteed to destroy some target standing here."""
    key = tuple(neighbour_codes)
    entry = filt.get(key)
    if entry is None:
        return False                     # no targets -> nothing to destroy
    maxhalfs, bits = entry
    # collect the alive-target half-HPs in TARGET_TILES order
    hp = []
    j = 0
    for code in neighbour_codes:
        if code in (2, 3):               # conv or harv = a target
            h = neighbour_halfhp[j]
            j += 1
            m = maxhalfs[len(hp)]
            if h < 1:
                return True              # already (nearly) dead
            hp.append(min(h, m))
    idx = _hp_index(hp, maxhalfs)
    return bool(bits[idx >> 3] & (1 << (idx & 7)))


# ----------------------------------------------------------------------------
# Self-tests / demo
# ----------------------------------------------------------------------------
def _demo():
    print("cells:", len(ALL_CELLS), "outer:", len(OUTER_TILES),
          "heal spots:", HEAL_SPOTS)
    assert len(HEAL_SPOTS) == 8, HEAL_SPOTS

    # Terrain: a single conveyor target at (1,0), everything else open.
    terr = make_terrain(cardinals={(1, 0): "conv"})
    board, table, note = solve(terr)
    print("single open conveyor -> states:", len(table))

    tg = board.targets            # [(1,0)]
    maxh = board.maxhalf[(1, 0)]  # 10 (=20hp)

    def look(hp_half, b, turn):
        return table[((hp_half,), b, turn)][0]

    # hp=2 (1 half), A to move, B anywhere: A attacks -> 0 -> A wins.
    for b in board.passable:
        assert look(1, b, 0) == "A", (b, look(1, b, 0))

    # Full-HP conveyor, B parked on a heal spot adjacent to it, B to move:
    # B heals forever, net +2/round -> B wins.
    assert (1, 1) in board.passable
    assert look(maxh, (1, 1), 1) == "B", look(maxh, (1, 1), 1)
    assert look(maxh, (1, 1), 0) == "B", look(maxh, (1, 1), 0)

    # Full-HP conveyor but B stuck far away with no heal reach in time: sanity that
    # at least some low-hp A-to-move states are A wins.
    a_wins = sum(1 for v in table.values() if v[0] == "A")
    b_wins = len(table) - a_wins
    print(f"  A-win states: {a_wins}   B-win states: {b_wins}")
    print("  OK: single-conveyor sanity checks passed")

    # Two adjacent conveyors, open board.
    terr2 = make_terrain(cardinals={(1, 0): "conv", (0, 1): "conv"})
    board2, table2, _ = solve(terr2)
    print("two open conveyors -> states:", len(table2))
    a2 = sum(1 for v in table2.values() if v[0] == "A")
    print(f"  A-win {a2} / {len(table2)}")


if __name__ == "__main__":
    _demo()
