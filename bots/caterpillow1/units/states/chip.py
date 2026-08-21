import map_info
from pathing import Pathing
import units.builder
from fcode import *

from log import log

# chip_lookup (the four table-reading functions), chip_tables (the tables), and
# chip_solve (the live solver). Both generated modules come from build/, which is
# not imported at runtime and so is not uploaded.
#
# Imported at MODULE scope, NOT lazily inside functions, so the submission bundler
# -- which ships the bot by following top-level imports -- actually includes them
# ("No module named 'chip_precompute'" was a lazy import it couldn't see). The
# sandbox blocks open() and file I/O, so nothing here may read from disk.
import chip_lookup
import chip_solve
import chip_tables

rc: Controller = None
nav: Pathing = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


# ============================================================================
# Chip: stand next to an enemy conveyor/harvester and destroy it by attacking
# (2 dmg/turn) despite the enemy healing (+4). We may pre-place up to N barriers
# on empty tiles of the 3x3 around the stand tile to make more spots winnable.
#
# Everything is an O(1) table lookup (10ms/turn budget -- no runtime solving).
# The offline table `chip_barrier_filter.pkl` (built by chip_precompute) answers,
# for a config (4 cardinal types + a 4-bit diagonal-wall mask, far heal spots and
# knights kept OPEN = sound lower bound) and the targets' HP, whether A is
# guaranteed to destroy one target vs an optimally-placed healer. A barrier just
# transforms the config (empty cardinal -> wall, or empty diagonal -> mask bit).
# ============================================================================

_CARD = [(1, 0), (-1, 0), (0, 1), (0, -1)]          # E, W, N, S  (== TARGET_TILES)
_DIAG = [(1, 1), (-1, 1), (1, -1), (-1, -1)]        # == chip_lookup.DIAGONALS
N_BARRIER = 2
_VIS_CAP = 18             # BFS cap for the healer-distance field
_REGION_CAP = 80         # max defense-zone tiles we'll solve (keeps it within ~10ms)
_CLAIM_RADIUS = 5        # BFS moves within which a parked (not-moving) teammate owns the chip spot

_cached_valid = 0
_cached_T = None                     # the winning tile score() validated as reachable
_plans = {}                          # tile index -> [barrier positions to place]
# Was 6, which sat ABOVE ordinary routing (5) and harvesting (4) and so froze
# the economy once the opening plan was done. 3.9 puts chipping below both and
# below chase (3.5 is chase's, so chip still outranks it) -- harassment is what
# a builder does when it has no economy work, not instead of economy work.
MAX_SCORE = 3.9

# Both tables are built by the templates in build/templates: the barrier table is
# plain literals, and the best-move table is literal metadata over one packed blob.
# The recursive codec that used to walk 26 MB at every import is gone.
_BFILTER = chip_tables.BFILTER
_BESTMOVE = chip_tables.BESTMOVE


# ---- shared terrain + the ONE definition of "chippable" ----------------------
def _terrain():
    """Every board mask chip needs, computed once and shared by validity AND the
    run-time attack choice. In particular `chippable` is defined in exactly ONE place
    here -- an enemy titanium-carrying conveyor that is not stuck, or an enemy
    harvester. valid_targets() (which decides a stand tile is a win) and every
    run-time target scan (which decides what to fire at) read this same mask, so they
    can never disagree about what counts as a target. (They used to: a tile was
    certified a win via a belt that the run-time solve had filtered out, so the
    attacker parked forever pecking a healable belt it could never kill.)"""
    conv = map_info._bm_et[map_info._IDX_CONVEYOR]
    splitter = map_info._bm_et[map_info._IDX_SPLITTER]
    harv = map_info._bm_et[map_info._IDX_HARVESTER]
    walls = map_info._bm_env[map_info._IDX_ENV_WALL]
    board = map_info._board_mask
    any_b = map_info._bm_any_building
    blockers = (any_b & ~conv & ~splitter) | walls      # physically impassible
    chippable = map_info._bm_team[1 - map_info._my_team_idx] & (
        (map_info._bm_ti_carrying & ~map_info.conv_stuck) | harv)
    return {
        "w": map_info._width, "h": map_info._height,
        "harv": harv, "board": board,
        "blockers": blockers,
        "passable": board & ~blockers,
        "empty": board & ~any_b & ~walls,               # truly empty (barrierable)
        "chippable": chippable,
    }


# ---- one pass over the 3x3 around a stand tile -------------------------------
def _classify(pos, ctx, want_heal=False):
    """Single classification of the 3x3 around `pos`, shared by validity, the barrier
    search, and every run-time target chooser. Returns:
      codes[4]    -- per cardinal (E,W,N,S): 0 empty / 1 wall / 2 conv / 3 harv
      dmask       -- 4-bit mask of blocked-or-OOB diagonals
      targets     -- one record per chippable cardinal neighbour, in E,W,N,S order:
                     {p, pos, n, harv, halfhp, maxhalf, heal_mask}. heal_mask is the
                     passable cardinal neighbours of the target other than `pos` (the
                     tiles a healer can heal it from); only filled when want_heal
                     (the run-time choosers need it; validity does not).
      barrierable -- ('card', i, Position) | ('diag', j, Position) for truly-empty
                     tiles we may turn into walls
    """
    w, h = ctx["w"], ctx["h"]
    chippable, harv, blockers, empty, passable = (
        ctx["chippable"], ctx["harv"], ctx["blockers"], ctx["empty"], ctx["passable"])
    my_n = pos.x + pos.y * w
    codes = [0, 0, 0, 0]
    targets, barrierable = [], []
    for i, (dx, dy) in enumerate(_CARD):
        x, y = pos.x + dx, pos.y + dy
        if not (0 <= x < w and 0 <= y < h):
            codes[i] = 1
            continue
        n = x + y * w
        b = 1 << n
        if chippable & b:
            is_h = bool(harv & b)
            mh = 15 if is_h else 10
            codes[i] = 3 if is_h else 2
            hm = 0
            if want_heal:
                for ddx, ddy in _CARD:
                    sx, sy = x + ddx, y + ddy
                    if 0 <= sx < w and 0 <= sy < h:
                        sn = sx + sy * w
                        if sn != my_n and (passable >> sn) & 1:
                            hm |= 1 << sn
            targets.append({
                "p": i, "pos": Position(x, y), "n": n, "harv": is_h,
                "halfhp": min(mh, (map_info._building_hp[n] + 1) // 2),
                "maxhalf": mh, "heal_mask": hm,
            })
        elif blockers & b:
            codes[i] = 1
        else:
            codes[i] = 0
            if empty & b:
                barrierable.append(("card", i, Position(x, y)))
    dmask = 0
    for j, (dx, dy) in enumerate(_DIAG):
        x, y = pos.x + dx, pos.y + dy
        if not (0 <= x < w and 0 <= y < h):
            dmask |= 1 << j
            continue
        n = x + y * w
        if (blockers >> n) & 1:
            dmask |= 1 << j
        elif (empty >> n) & 1:
            barrierable.append(("diag", j, Position(x, y)))
    return codes, dmask, targets, barrierable


# ---- barrier search: fewest barriers, cardinals preferred --------------------
def _barrier_plan(codes, base_dmask, halfhp, barrierable, ctx, max_barriers=N_BARRIER):
    """Fewest-barrier (<= max_barriers) subset that makes the config a guaranteed win,
    or None. Returns a list of barrier descriptors (possibly empty = win with 0
    barriers). max_barriers caps the search at what the caller can afford this turn, so
    chip never certifies a stand tile whose plan it can't pay for (which would strand
    the builder adjacent to a barrier it can never place)."""
    filt = ctx["filt"]
    if chip_lookup.wins_barrier(filt, codes, base_dmask, halfhp):
        return []
    order = sorted(barrierable, key=lambda b: 0 if b[0] == "card" else 1)

    def apply(sel):
        c = list(codes)
        dm = base_dmask
        for b in sel:
            if b[0] == "card":
                c[b[1]] = 1
            else:
                dm |= 1 << b[1]
        return tuple(c), dm

    for k in range(1, max_barriers + 1):
        # enumerate size-k subsets in cardinal-preferred order; take first winner
        import itertools
        for sel in itertools.combinations(order, k):
            c, dm = apply(sel)
            if chip_lookup.wins_barrier(filt, c, dm, halfhp):
                return list(sel)
    return None


# ---- run-time target selection (standing on the tile, about to fire) ---------
# These decide WHICH adjacent target to attack; validity already guaranteed a win
# exists. Vision is used only to locate enemy BUILDER positions (a hidden builder
# could be just outside vision); all terrain comes from persistent map_info.
def _healer_field(ctx):
    """Multi-source GRAPH BFS over passable tiles from every known enemy builder AND
    every non-visible passable tile. reached[k] = tiles within k healer-moves."""
    passable = ctx["passable"]
    sources = ((ctx["board"] & ~map_info._bm_visible) & passable) | (map_info._bm_enemy_bots & passable)
    reached = [sources]
    cur = sources
    for _ in range(_VIS_CAP):
        cur = (map_info.expand_manhattan(cur) & passable) | cur
        reached.append(cur)
    return reached


def _free_kill_target(reached, targets):
    """A target A can destroy before any healer can heal it -- H < d + 2, where H is
    hits-to-kill and d is the fastest healer's graph distance to a usable heal spot
    (off-by-one: A kills on hit H; the healer's first heal is turn d+1, so A wins iff
    H <= d+1). A target with no usable heal spot is always free. Position or None."""
    ncap = len(reached)
    for t in targets:
        H = (map_info._building_hp[t["n"]] + 1) // 2      # hits to kill
        hm = t["heal_mask"]
        if hm == 0:                                       # no usable heal spot: free
            return t["pos"]
        d = ncap + 1
        m = hm
        while m:
            lsb = m & -m
            sn = lsb.bit_length() - 1
            m ^= lsb
            for k in range(ncap):
                if (reached[k] >> sn) & 1:
                    if k < d:
                        d = k
                    break
        if H < d + 2:
            return t["pos"]
    return None


def _closest_healer(allspots, passable):
    """(b0 tile, d0): nearest enemy builder / non-visible passable tile to the heal
    spots. b0=-1 if none reachable within the cap."""
    healer_src = (map_info._bm_enemy_bots | (map_info._board_mask & ~map_info._bm_visible)) & passable
    frontier = allspots
    visited = allspots
    for d0 in range(0, 31):
        cand = frontier & healer_src
        if cand:
            return (cand & -cand).bit_length() - 1, d0
        frontier = map_info.expand_manhattan(frontier) & passable & ~visited
        if not frontier:
            break
        visited |= frontier
    return -1, 31


def _lowest_hp_target(targets):
    """Last resort (the solve/table bailed on their caps): the adjacent chippable
    target with the fewest hits to kill."""
    best, best_h = None, 999
    for t in targets:
        if t["halfhp"] < best_h:
            best_h, best = t["halfhp"], t["pos"]
    return best


def _table_best_target(pos, ctx, codes, dmask, targets):
    """O(1) optimal-winning target from the precomputed best-move table -- for the
    heavy (3-4 target) cases the live solver skips, when the healer sits in the local
    cluster. Position or None."""
    if not targets:
        return None
    allspots = 0
    for t in targets:
        allspots |= t["heal_mask"]
    if allspots == 0:
        return None
    b0, _d0 = _closest_healer(allspots, ctx["passable"])
    if b0 < 0:
        return None
    w = ctx["w"]
    off = (b0 % w - pos.x, b0 // w - pos.y)
    if off not in chip_lookup.ALL_CELLS:            # healer not in the cluster -> table N/A
        return None
    hp = [t["halfhp"] for t in targets]
    mi = chip_lookup.bestmove_lookup(_BESTMOVE, codes, dmask, hp, off)
    if mi is None:
        return None
    return targets[mi]["pos"]


def _solve_best_target(ctx, targets):
    """Exact min-turns solve: which adjacent target to attack so A destroys one in the
    fewest turns against the single closest healer. None if unsolvable within the
    region/state caps (then the far-healer free-kill path already covered the fast
    cases). Operates on the real board geometry (not the cluster abstraction)."""
    if not targets:
        return None
    w, h, passable = ctx["w"], ctx["h"], ctx["passable"]
    maxhalf = [t["maxhalf"] for t in targets]
    hp0 = [t["halfhp"] for t in targets]
    heal_masks = [t["heal_mask"] for t in targets]
    allspots = 0
    for hm in heal_masks:
        allspots |= hm
    k = len(targets)
    if allspots == 0:
        return None

    # Healer b0 = the closest enemy builder OR non-visible passable tile (a hidden
    # builder could be just outside vision) to the heal spots -- and its distance d0.
    b0, d0 = _closest_healer(allspots, passable)
    if b0 < 0:
        return None

    # Defense zone = passable tiles within max(d0, 3) of the heal spots: enough for
    # the healer's maneuvering between spots AND its approach corridor from b0.
    reg_r = d0 if d0 > 3 else 3
    region = allspots
    fr = allspots
    for _ in range(reg_r):
        fr = map_info.expand_manhattan(fr) & passable & ~region
        region |= fr
    region |= 1 << b0
    tiles = [p.x + p.y * w for p in map_info.iter_mask(region)]
    if len(tiles) > _REGION_CAP:
        return None
    idx = {n: i for i, n in enumerate(tiles)}
    nt = len(tiles)
    # Upfront state-count estimate: prod(hp ranges) * tiles * 2. Bail (before doing any
    # work) when it's too large to solve well inside the 10ms turn budget. Measured:
    # est ~= 20000 took ~12.6ms and est ~= 8000 ~7ms (both risk the 10ms turn budget once
    # the rest of the builder's turn is added), so cap at 5000 (~4.5ms). Bailed cases are
    # handled by the precomputed table (tried first) or the lowest-HP fallback -- and
    # free-kill already covered the fast wins.
    est = nt * 2
    for mh in maxhalf:
        est *= (mh + 1)
    if est > 5000:
        return None
    adj = [[] for _ in range(nt)]
    heal_by_tile = [[] for _ in range(nt)]
    for i, n in enumerate(tiles):
        x, y = n % w, n // w
        for dx, dy in _CARD:
            nn = (x + dx) + (y + dy) * w
            if 0 <= x + dx < w and 0 <= y + dy < h and nn in idx:
                adj[i].append(idx[nn])
    for j in range(k):
        m = heal_masks[j]
        while m:
            lsb = m & -m
            sn = lsb.bit_length() - 1
            m ^= lsb
            if sn in idx:
                heal_by_tile[idx[sn]].append(j)

    _plies, bi = chip_solve.solve(maxhalf, hp0, adj, heal_by_tile, idx[b0])
    if bi is None:
        return None
    return targets[bi]["pos"]


# ---- valid targets -----------------------------------------------------------
def _stationary_claim_region(passable):
    """Tiles within _CLAIM_RADIUS BFS moves (over passable) of a friendly builder bot
    that is NOT moving. A parked teammate is presumably already working the chip spot
    beside it, so we invalidate stand tiles inside this region and look elsewhere --
    keeping two builders from converging on the same target. (_bm_friendly_stationary
    already excludes us, and persists a teammate through it leaving our vision.)"""
    seed = map_info._bm_friendly_stationary
    if not seed:
        return 0
    region = seed
    cur = seed
    for _ in range(_CLAIM_RADIUS):
        cur = map_info.expand_manhattan(cur) & passable & ~region
        region |= cur
    return region


def valid_targets() -> int:
    global _plans
    ctx = _terrain()
    ctx["filt"] = _BFILTER
    units.builder.draw_mask(map_info.conv_stuck, 255, 0, 0)
    w = ctx["w"]
    possible = (map_info.manhattan(ctx["chippable"])
                & ~map_info._bm_enemy_turret_threat & ~map_info._bm_enemy_launch_adj
                & ~ctx["blockers"] & ~map_info._bm_enemy_bots)
    # Cede spots a parked teammate is already working: drop any stand tile within
    # _CLAIM_RADIUS BFS moves of a friendly builder that is not moving.
    possible &= ~_stationary_claim_region(ctx["passable"])
    _plans = {}
    if not possible:
        return 0

    # How many barriers we can pay for THIS turn (0..N_BARRIER). Barrier cost scales,
    # so this uses the current cost as a floor: afford N when the balance left after
    # the ti reserve covers N of them. Plans needing more than this are never
    # certified, so chip won't commit to a stand tile it can't finish. Already-placed
    # barriers read as walls in _classify, so a plan shrinks as it is built and this
    # keeps tracking only the REMAINING barriers.
    cost = rc.get_barrier_cost()
    budget = rc.get_global_resources() - map_info.ti_reserve()
    afford = 0
    while afford < N_BARRIER and budget >= (afford + 1) * cost:
        afford += 1

    # Barrier-win only -- the win certificate is a pure persistent building/barrier
    # lookup, so the valid set never shifts as the builder moves. A tile is valid iff,
    # placing up to `afford` barriers on empty 3x3 tiles, A is guaranteed to destroy a
    # target vs an optimally-placed healer. (The "kill it before it can be healed"
    # shortcut is a run-time attack choice, not a validity criterion.)
    valid = 0
    for T in map_info.iter_mask(possible):
        codes, dmask, targets, barrierable = _classify(T, ctx)
        if not targets:                      # no live target adjacent
            continue
        halfhp = [t["halfhp"] for t in targets]
        bp = _barrier_plan(codes, dmask, halfhp, barrierable, ctx, afford)
        if bp is not None:
            valid |= 1 << (T.x + T.y * w)
            _plans[T.x + T.y * w] = [d[2] for d in bp]   # barrier positions to place
    return valid


# ---- state API ---------------------------------------------------------------
def score(can_move=True):
    global _cached_valid, _cached_T
    _cached_valid = valid_targets()
    _cached_T = None
    if not _cached_valid:
        return 0
    my = map_info._my_pos
    if (_cached_valid >> (my.x + my.y * map_info._width)) & 1:
        _cached_T = my                       # already standing on a winning tile
        return MAX_SCORE
    if not can_move:
        return 0                             # in-place retry, not on a winning tile
    # Require a REACHABLE winning tile, like harvest/route validate reachability in
    # score(). Otherwise an unreachable valid tile keeps chip selected every turn
    # while run() can never path to it -- freezing the builder in place forever.
    T, _ = nav.closest(_cached_valid, to_adjacent=False)
    if T is None:
        return 0
    _cached_T = T
    return MAX_SCORE


def _fire(tpos):
    if rc.get_global_resources() >= 2 and rc.can_fire(tpos):
        rc.fire(tpos)


def run(can_move=True):
    log("CHIP")
    rc.draw_indicator_line(Position(-100, -100), map_info._my_pos, 255, 255, 0)
    T = _cached_T                            # reachable winning tile chosen in score()
    if T is None:
        return
    w = map_info._width
    barriers = _plans.get(T.x + T.y * w)
    if barriers is None:
        return
    my = map_info._my_pos

    # Place any barriers this tile still needs (already-built ones are skipped),
    # moving adjacent to each in turn.
    walls = map_info._bm_env[map_info._IDX_ENV_WALL]
    any_b = map_info._bm_any_building
    remaining = [p for p in barriers
                 if not ((any_b | walls) >> (p.x + p.y * w)) & 1]
    if remaining:
        if not can_move:
            return
        rem_mask = 0
        for p in remaining:
            rem_mask |= 1 << (p.x + p.y * w)
        bt, _ = nav.closest(rem_mask)
        if bt is None:
            bt = remaining[0]
        if nav.move_adjacent(bt):            # not yet adjacent -> keep moving
            return
        need = rc.get_barrier_cost() + map_info.ti_reserve()
        if rc.get_global_resources() >= need and rc.can_build_barrier(bt):
            rc.build_barrier(bt)
            map_info.update_at(bt)
        return

    # Walk ONTO the stand tile.
    if my != T:
        if not can_move:
            return
        nav.move_to(T)
        my = map_info._my_pos
        if my != T:
            return

    # Standing on the tile, choose which target to attack (one shared classification):
    #  1. free-kill: a target that dies before any possible healer can reach it (O(1));
    #  2. else the exact min-turns solve vs the closest healer (don't peck a healable
    #     target); 3. the precomputed table for the heavy 3-4 target cases; 4. lowest
    #  HP only if the solve/table bailed on their region/state caps.
    ctx = _terrain()
    codes, dmask, targets, _ = _classify(my, ctx, want_heal=True)
    if not targets:
        return
    reached = _healer_field(ctx)
    tgt = _free_kill_target(reached, targets)                 # 1. far healer: O(1)
    if tgt is None:
        # Precomputed table FIRST: it's O(1) and already covers 1-4 targets whenever the
        # healer sits in the local cluster (the common case). Only fall to the exact live
        # solver when the healer is outside the cluster, where the table doesn't apply --
        # this keeps chip off the ~6ms live solve on nearly every turn.
        tgt = _table_best_target(my, ctx, codes, dmask, targets)  # 2. precomputed table: O(1)
    if tgt is None:
        tgt = _solve_best_target(ctx, targets)                # 3. exact live solve (healer far)
    if tgt is None:
        tgt = _lowest_hp_target(targets)                      # 4. last resort
    if tgt is not None:
        _fire(tgt)
