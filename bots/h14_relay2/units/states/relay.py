"""Builder state: launcher-relay to the enemy core ring, from turn one (#36).

Read off Tyr_Jython, whose replays show the whole crossing in fifteen turns of a
30x30 game: t1 build a launcher beside yourself, t3 be thrown and build the next
where you land, ... t13 beside the enemy core, t15 first barrier on its ring.
Two turns a hop, ~5.8 tiles a hop -- nearly three tiles a turn against a walking
builder's one, and a throw ignores walls entirely.

WHY THE FIRST TURNS AND NOT LATER. Build cost is floor(scale * base) and scale
climbs with everything built. A launcher is LAUNCHER_BASE_COST 20 at the start
and was measured at 58 Ti by round ~30-80 -- three harvesters for one hop. An
earlier attempt claimed its trekker at round 30 and never afforded a single
launcher, so it walked and died; that measured nothing about this strategy.

WHY THE RING IS THE WHOLE GAME. A 2x2 core has exactly twelve tiles at chebyshev
1, and CORE_SPAWNING_RADIUS_SQ = 2 makes that same set the core's ENTIRE spawn
ring -- it is also the only place a conveyor can stand and deliver into the core.
Take all twelve and the enemy can neither spawn a builder nor be paid.

NO HARDCODED MAPS. Tyr reads their core from a per-map table; CLAUDE.md forbids
that because tournaments run on fresh maps. We reflect OUR core through whichever
symmetry map_info has not yet eliminated (_hor_sym / _ver_sym / _rot_sym), which
is available from turn one and self-corrects the moment the real core is seen.
"""
import map_info
import units.builder
from fcode import *
from log import log

rc: Controller = None
nav = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


MAX_SCORE = 9.6          # above heal(9.5): the relay must not be interrupted
LAUNCHER_RANGE_SQ = 26   # throw disk
ARRIVE_DIST = 3          # close enough; hand over to siege/block/chip
MAX_HOPS = 5             # a crossing should take 1-5 hops, never more
CLAIM_BEFORE_ROUND = 12  # the first builder does not act until ~r4; 3 never fired
SIEGE_BOTS = 1
_CARDINALS = ((0, -1), (1, 0), (0, 1), (-1, 0))

# IDENTITY BY BOT ID -- no comms, no shared state needed.
# The first builder each side spawns has a fixed id: GOLD 3, SILVER 4. The next
# pair is 5 and 6. So "am I a siege builder" is just an id test, which every unit
# can answer for itself AND which a launcher can answer about an adjacent builder,
# since rc.get_tile_builder_bot_id gives it the id directly.
# (Module globals cannot do this job: each unit runs its OWN interpreter, so a
# per-unit set had every builder claim the role -- measured, 20 launchers and 5
# different builders thrown in one game.)
SIEGE_ID_MAX = 6
                         # 6 = two per side (adds 5 and 6)


def is_siege_id(uid) -> bool:
    return uid is not None and uid <= SIEGE_ID_MAX


def am_siege() -> bool:
    return is_siege_id(rc.get_id())


_arrived = set()
_BAND_ROUND = -1
_BANDS = ()
_hop = None              # (site, landing) chosen this turn
_hops_used = {}
_last_pos = {}


def their_core_area() -> int:
    """Enemy core tiles: observed if we have seen them, else reflected through the
    surviving symmetry. Available from turn one, and self-correcting the moment the
    real core comes into view. This is what replaces Tyr's hardcoded map tables."""
    seen = map_info._bm_their_core_area
    if seen:
        return seen
    mine = map_info._bm_my_core_area
    if not mine:
        return 0
    w, h = map_info._width, map_info._height
    out = 0
    m = mine
    while m:
        b = m & -m
        n = b.bit_length() - 1
        m ^= b
        x, y = n % w, n // w
        if map_info._hor_sym:
            fx, fy = w - 1 - x, y
        elif map_info._ver_sym:
            fx, fy = x, h - 1 - y
        else:
            fx, fy = w - 1 - x, h - 1 - y
        out |= 1 << (fx + fy * w)
    return out


def _ring() -> int:
    """All TWELVE tiles at chebyshev 1 of the 2x2 core -- spawn ring and the only
    delivery tiles both. Not just the eight cardinals."""
    core = their_core_area()
    if not core:
        return 0
    w = map_info._width
    out = 0
    m = core
    while m:
        b = m & -m
        n = b.bit_length() - 1
        m ^= b
        x, y = n % w, n // w
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < map_info._height:
                    out |= 1 << (nx + ny * w)
    return out & ~core & map_info._board_mask


def _open() -> int:
    """Ground a launcher or a landing may use: not wall, not a building, not a
    core. Enemy bots do not matter here -- the plan is recomputed every turn."""
    return (map_info._board_mask
            & ~map_info._bm_env[map_info._IDX_ENV_WALL]
            & ~map_info._bm_any_building
            & ~map_info._bm_my_core_area
            & ~their_core_area())


def _bands():
    """bands[d] = tiles exactly d steps from the enemy ring, over open ground."""
    global _BAND_ROUND, _BANDS
    r = rc.get_current_round()
    if r == _BAND_ROUND:
        return _BANDS
    _BAND_ROUND = r
    ring = _ring()
    if not ring:
        _BANDS = ()
        return _BANDS
    walk = _open() | ring
    bands = [ring]
    seen = ring
    for _ in range(80):
        nxt = map_info.manhattan(seen) & walk & ~seen
        if not nxt:
            break
        bands.append(nxt)
        seen |= nxt
    _BANDS = tuple(bands)
    return _BANDS


def _dist_at(x, y) -> int:
    bands = _bands()
    bit = 1 << (x + y * map_info._width)
    for d, band in enumerate(bands):
        if band & bit:
            return d
    return 999


def best_landing(sx, sy, cur_dist):
    """Best tile a launcher at (sx,sy) can throw someone to, given their current
    distance-to-ring. Shared by the builder (planning) and the launcher (throwing)
    so both derive the same answer with NO shared state -- units run in separate
    interpreters and cannot see each other's globals."""
    w, h = map_info._width, map_info._height
    opn = _open()
    best = None
    for ldx in range(-5, 6):
        for ldy in range(-5, 6):
            dd = ldx * ldx + ldy * ldy
            if dd < 1 or dd > LAUNCHER_RANGE_SQ:
                continue
            lx, ly = sx + ldx, sy + ldy
            if not (0 <= lx < w and 0 <= ly < h):
                continue
            if not (opn & (1 << (lx + ly * w))):
                continue
            d = _dist_at(lx, ly)
            if d >= cur_dist:
                continue
            key = (d, -dd, lx, ly)
            if best is None or key < best:
                best = key
    if best is None:
        return None
    return Position(best[2], best[3])


def dist_at(x, y):
    return _dist_at(x, y)


def best_hop(x, y):
    """(site, landing): where to put the next launcher, and where it throws us.

    Same rule as Tyr's chain, but recomputed live rather than precomputed from a
    map table -- so it adapts as terrain is revealed instead of needing it up front.
    """
    bands = _bands()
    if len(bands) < 2:
        return None
    cur = _dist_at(x, y)
    if cur >= 999:
        return None
    w, h = map_info._width, map_info._height
    opn = _open()
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    best = None
    for dx, dy in _CARDINALS:
        sx, sy = x + dx, y + dy
        if not (0 <= sx < w and 0 <= sy < h):
            continue
        sbit = 1 << (sx + sy * w)
        if not (opn & sbit):
            continue
        # An ore tile is a harvester site; spending it on a launcher we walk past
        # once is the worst trade on the board.
        if ore & sbit:
            continue
        for ldx in range(-5, 6):
            for ldy in range(-5, 6):
                dd = ldx * ldx + ldy * ldy
                if dd < 1 or dd > LAUNCHER_RANGE_SQ:
                    continue
                lx, ly = sx + ldx, sy + ldy
                if not (0 <= lx < w and 0 <= ly < h):
                    continue
                if not (opn & (1 << (lx + ly * w))):
                    continue
                d = _dist_at(lx, ly)
                if d >= cur:
                    continue
                key = (d, -dd, sx, sy, lx, ly)
                if best is None or key < best:
                    best = key
    if best is None:
        return None
    return Position(best[2], best[3]), Position(best[4], best[5])


def _ride_in_range() -> bool:
    """Is one of our launchers within pickup range (r2 <= 2)?"""
    my = map_info._my_pos
    w = map_info._width
    mbit = 1 << (my.x + my.y * w)
    mine = map_info._bm_team[map_info._my_team_idx]
    return bool(map_info.manhattan(mbit, 2)
                & map_info._bm_et[map_info._IDX_LAUNCHER] & mine)


def score(can_move=True):
    global _hop
    _hop = None
    uid = rc.get_id()
    if uid in _arrived:
        return 0
    if not am_siege():
        return 0
    my = map_info._my_pos
    if _dist_at(my.x, my.y) <= ARRIVE_DIST:
        _arrived.add(uid)
        return 0
    if _hops_used.get(uid, 0) >= MAX_HOPS:
        return 0
    # A ride is already in pickup range: HOLD, whatever best_hop says.
    # This must not depend on best_hop, because once our launcher occupies the
    # site, _open() excludes it as a building and best_hop can return None -- at
    # which point score() used to return 0, another state took the turn, and the
    # builder WALKED AWAY FROM ITS OWN LAUNCHER. Measured in game 12228: build at
    # t1, idle 3, then six more builds and three walks before the first throw.
    if _ride_in_range():
        _hop = None
        return MAX_SCORE
    hop = best_hop(my.x, my.y)
    if hop is None:
        return 0
    _hop = hop
    return MAX_SCORE


def run(can_move=True):
    if _ride_in_range():
        log("RELAY wait for ride")
        return                      # stand still; the launcher throws us
    if _hop is None:
        return
    site, landing = _hop
    uid = rc.get_id()
    w = map_info._width
    mine = map_info._bm_team[map_info._my_team_idx]
    # Wait if ANY of our launchers is already in pickup range (r2 <= 2), not just
    # one on the exact site we recomputed this turn. best_hop can pick a different
    # site each turn as terrain is revealed, and the site-only test made us build a
    # fresh launcher every time it moved -- measured 9 launchers for 2 throws.
    sbit = 1 << (site.x + site.y * w)
    cost = rc.get_launcher_cost()
    if rc.get_global_resources() >= cost and rc.can_build_launcher(site):
        rc.build_launcher(site)
        # Budget is spent per LAUNCHER BUILT. Counting per throw instead was tried
        # and was worse: it let the builder keep planting launchers that never
        # fired (midgard: ARRIVED with 5 -> cheb 24 with 7).
        _hops_used[uid] = _hops_used.get(uid, 0) + 1
        log("RELAY build", site, "->", landing)
        return
    log("RELAY blocked", site, "cost", cost, "bank", rc.get_global_resources())
