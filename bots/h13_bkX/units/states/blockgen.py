"""Builder state: body-block enemy builders (#29).

Movement is 4-WAY and bots collide team-agnostically, so a body on the tile an
enemy needs is a real wall. Under 4-way movement a runner heading for a point has
at most TWO productive moves and exactly one once axis-aligned, so the cut is
small. We compute BFS bands outward from a protected set; an enemy in band d can
only close by stepping into band d-1, and those tiles are the cut.

NO ZUGZWANG. Nobody is forced to move, so this is a STANDOFF, not a chess squeeze
-- we do not win the tile, we simply keep it. That is enough: the enemy cannot
pass while we hold, and builds require exactly Manhattan-1, so a pinned builder
can extend at most one tile and then stalls.

COST ASYMMETRY -- the reason CUT STABILITY matters more than cut size. Moving and
acting are mutually exclusive (the engine needs BOTH cooldowns at 0 for either):
  * terrain-enforced cut (corridor): the cut tile does not move, we stand still,
    and our whole action stays free every turn.
  * open-ground cut: the cut shifts when they sidestep, so we must MIRROR, and
    mirroring forfeits our action every single turn.
So a corridor block is nearly free and an open-ground block is expensive. We
prefer stable cuts and cap how much open-ground mirroring we will pay for.

Builders can only damage BUILDINGS, never other bots ("Builder Bots can attack
the building on any orthogonally adjacent tile"), so while pinned neither side
can hurt the other -- a pure standoff of builder-turns, no chip race.
"""
import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import *
from log import log

rc: Controller = None
nav: Pathing = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


# Which situation this build blocks for (#29 factors):
#   "defence" F2 -- our core and harvesters; attackers walking at us.
#   "healcut" F3 -- enemy buildings WE have damaged; deny them the repair walk.
#   "routing" F1 -- their core; deny the builder extending a line toward its sink.
SITUATION = "defence"
MAX_SCORE = 9.9
PIN_RADIUS = 20
AVOID_SPAWN_RING = False
# Pay to mirror an unstable (open-ground) cut? False = corridor blocks only,
# which keeps our action free every turn.
ALLOW_UNSTABLE = True

_DIAG=[0,0,0,0,0]
_cached_target = None
_BAND_ROUND = -1
_BANDS = ()


def _protected() -> int:
    mine = map_info._bm_team[map_info._my_team_idx]
    theirs = map_info._bm_team[1 - map_info._my_team_idx]
    if SITUATION == "defence":
        return map_info._bm_my_core_area | (
            map_info._bm_et[map_info._IDX_HARVESTER] & mine)
    if SITUATION == "healcut":
        # Their damaged buildings: standing between a damaged building and the
        # builder walking to heal it denies the heal outright, because heal needs
        # exact adjacency.
        return map_info._bm_damaged & theirs
    if SITUATION == "routing":
        return map_info._bm_their_core_area
    return 0


def _bands():
    """bands[d] = passable tiles exactly d steps from the protected set.

    Walls are respected (each dilation is intersected with passable()), so a
    corridor collapses the cut to a single tile automatically -- which is exactly
    where blocking is cheapest.
    """
    global _BAND_ROUND, _BANDS
    r = rc.get_current_round()
    if r == _BAND_ROUND:
        return _BANDS
    _BAND_ROUND = r
    prot = _protected()
    if not prot:
        _BANDS = ()
        return _BANDS
    # NOT passable(): that is sourced from get_avoid and EXCLUDES tiles holding
    # enemy bots, so an enemy would never appear inside its own band structure
    # and the cut was empty on every one of ~1000 measured calls. We want raw
    # TERRAIN walkability here -- walls and blocking buildings only. The final
    # cut is still filtered through passable() before we try to stand on it.
    walk = map_info._board_mask & ~map_info._bm_blocked
    bands = [prot]
    seen = prot
    for _ in range(PIN_RADIUS):
        nxt = map_info.manhattan(seen) & ~seen & walk
        if not nxt:
            break
        bands.append(nxt)
        seen |= nxt
    _BANDS = tuple(bands)
    return _BANDS


def _cut_tiles():
    bands = _bands()
    if len(bands) < 2:
        return 0, 0
    enemies = map_info._bm_enemy_bots
    if not enemies:
        return 0, 0
    cut = 0
    stable = 0
    for d in range(1, len(bands)):
        here = enemies & bands[d]
        if not here:
            continue
        inner = bands[d - 1]
        m = here
        while m:
            b = m & -m
            m ^= b
            step = map_info.manhattan(b) & ~b & inner
            cut |= step
            # A cut of exactly one tile is terrain-enforced: they have a single
            # way forward, so holding it needs no mirroring and our action stays
            # free. Anything wider shifts when they sidestep.
            if step and (step & (step - 1)) == 0:
                stable |= step
    return cut, stable


def score(can_move=True):
    global _cached_target
    _cached_target = None
    _DIAG[0]+=1
    cut, stable = _cut_tiles()
    if not ALLOW_UNSTABLE:
        cut = stable
    if not cut:
        return 0
    _DIAG[1]+=1
    cut &= map_info.passable()
    cut &= ~map_info._bm_enemy_turret_threat
    if AVOID_SPAWN_RING:
        core = map_info._bm_my_core_area
        cut &= ~(map_info.manhattan(core) & ~core)
    if not cut:
        return 0
    w = map_info._width
    claims = pathing.claim_subset(
        1 << (map_info._my_pos.x + map_info._my_pos.y * w),
        map_info._bm_friendly_bots, cut, tie_self=True)
    if not claims:
        return 0
    _DIAG[2]+=1
    my = map_info._my_pos
    if (claims >> (my.x + my.y * w)) & 1:
        _cached_target = my
        _DIAG[3]+=1
        return MAX_SCORE
    if not can_move:
        return 0
    target, dist = nav.closest(claims, to_adjacent=False)
    if target is None:
        return 0                       # arrive late and the tile is already past
    _cached_target = target
    _DIAG[4]+=1
    return MAX_SCORE


def run(can_move=True):
    target = _cached_target
    if target is None:
        return
    log("BLOCK", SITUATION, target)
    my = map_info._my_pos
    if target.x != my.x or target.y != my.y:
        nav.move_to(target, can_move=can_move)
        return
    # Holding: standing still IS the block, so the action is unspent. Spend it on
    # an adjacent enemy BUILDING if there is one (bots cannot be damaged).
    if rc.get_global_resources() < GameConstants.BUILDER_BOT_ATTACK_COST:
        return
    w = map_info._width
    adj = map_info.manhattan(1 << (my.x + my.y * w))
    m = adj & map_info._bm_any_building & map_info._bm_team[1 - map_info._my_team_idx]
    while m:
        b = m & -m
        n = b.bit_length() - 1
        m ^= b
        p = Position(n % w, n // w)
        if rc.can_fire(p):
            rc.fire(p)
            return


def diag():
    print("BLOCKDIAG scored=%d cut_nonempty=%d claimed=%d holding=%d stepping=%d" % tuple(_DIAG), flush=True)
