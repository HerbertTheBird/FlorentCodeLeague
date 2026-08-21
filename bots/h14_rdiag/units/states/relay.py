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
MAX_HOPS = 12
CLAIM_BEFORE_ROUND = 12  # the first builder does not act until ~r4; 3 never fired
SIEGE_BOTS = 1
_CARDINALS = ((0, -1), (1, 0), (0, 1), (-1, 0))

_RD=[0,0,0,0,0,0,0]
siege_ids = set()        # read by turret_launcher to know who to throw
_arrived = set()
_BAND_ROUND = -1
_BANDS = ()
_hop = None              # (site, landing) chosen this turn


def their_core_area() -> int:
    """Enemy core tiles: observed if we have seen them, else reflected through
    the surviving symmetry. Available from turn one, and self-correcting."""
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


def score(can_move=True):
    global _hop
    _hop = None
    _RD[0]+=1
    uid = rc.get_id()
    if uid in _arrived:
        return 0
    if uid not in siege_ids:
        if rc.get_current_round() <= CLAIM_BEFORE_ROUND and len(siege_ids) < SIEGE_BOTS:
            siege_ids.add(uid)
            _RD[1]+=1
        else:
            return 0
    my = map_info._my_pos
    if _dist_at(my.x, my.y) <= ARRIVE_DIST:
        _arrived.add(uid)
        siege_ids.discard(uid)
        return 0
    if len(siege_ids) and _hops_used.get(uid, 0) >= MAX_HOPS:
        return 0
    if not _ring():
        _RD[3]+=1
        return 0
    hop = best_hop(my.x, my.y)
    if hop is None:
        _RD[4]+=1
        return 0
    _hop = hop
    return MAX_SCORE


_hops_used = {}


def run(can_move=True):
    _RD[5]+=1
    if _hop is None:
        return
    site, landing = _hop
    uid = rc.get_id()
    w = map_info._width
    mine = map_info._bm_team[map_info._my_team_idx]
    sbit = 1 << (site.x + site.y * w)
    # Our launcher is already on the site: hold still, it throws us on its turn.
    if map_info._bm_et[map_info._IDX_LAUNCHER] & mine & sbit:
        log("RELAY wait", site, landing)
        return
    cost = rc.get_launcher_cost()
    if rc.get_global_resources() >= cost and rc.can_build_launcher(site, Direction.NORTH):
        rc.build_launcher(site, Direction.NORTH)
        _hops_used[uid] = _hops_used.get(uid, 0) + 1
        _RD[6]+=1
        log("RELAY build", site, "->", landing)
        return
    log("RELAY blocked", site, "cost", cost, "bank", rc.get_global_resources())


def rd_report():
    import map_info as _mi
    my = _mi._my_pos
    print("RELAYDIAG r=%d score=%d claimed=%d no_ring=%d no_hop=%d run=%d BUILT=%d ids=%s bank=%d lcost=%d dist=%d"
          % (rc.get_current_round(), _RD[0],_RD[1],_RD[3],_RD[4],_RD[5],_RD[6],
             sorted(siege_ids), rc.get_global_resources(), rc.get_launcher_cost(),
             _dist_at(my.x, my.y)), flush=True)
