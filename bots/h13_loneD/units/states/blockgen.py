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
MAX_SCORE = 6.5
PIN_RADIUS = 6
AVOID_SPAWN_RING = True
# Pay to mirror an unstable (open-ground) cut? False = corridor blocks only,
# which keeps our action free every turn.
ALLOW_UNSTABLE = True
# F4 GIVE-UP: stop holding after this many CONSECUTIVE turns on the same tile.
# Measured on the permissive diagnostic arm: one builder held for 893 turns of a
# 900-turn game. A standoff is only worth it while it denies something; past that
# it is just a builder we have taken off the board ourselves. 0 = no cap.
HOLD_LIMIT = 0
# #31 RARITY GATES. Blocking costs a builder-turn EVERY turn, so the only way it
# pays is to fire almost never, on a near-certain win.
#   LONE_ONLY -- refuse to block a router that has SUPPORT. The engine permits
#     laying a conveyor on a bot-occupied tile (conveyor/splitter only), so our
#     body stops the router ADVANCING but a teammate of theirs already past our
#     block can continue the line from the far side and the pin achieves nothing.
#     A supporter is any other enemy builder strictly CLOSER to their goal than
#     the router is, within SUPPORT_RADIUS.
#   REQUIRE_ROUTING -- only block a bot that is demonstrably extending a line,
#     i.e. standing next to one of their own conveyors.
LONE_ONLY = True
SUPPORT_RADIUS = 6
REQUIRE_ROUTING = False

_cached_target = None
_held = {}          # unit id -> [last_round, consecutive_holds]
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


def _worth_blocking(bot_bit, d, bands, enemies):
    """#31 rarity gates: is this specific router worth a builder-turn per turn?"""
    if REQUIRE_ROUTING:
        theirs = map_info._bm_team[1 - map_info._my_team_idx]
        if not (map_info.manhattan(bot_bit) & map_info._bm_conveyors & theirs):
            return False
    if LONE_ONLY:
        inner = 0
        for i in range(d):
            inner |= bands[i]
        near = map_info.manhattan(bot_bit, SUPPORT_RADIUS)
        if (enemies & ~bot_bit) & inner & near:
            return False               # supported: they bridge past us for free
    return True


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
            if (LONE_ONLY or REQUIRE_ROUTING) and not _worth_blocking(b, d, bands, enemies):
                continue
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
    cut, stable = _cut_tiles()
    if not ALLOW_UNSTABLE:
        cut = stable
    if not cut:
        return 0
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
    my = map_info._my_pos
    if (claims >> (my.x + my.y * w)) & 1:
        if HOLD_LIMIT:
            uid = rc.get_id()
            r = rc.get_current_round()
            st = _held.get(uid)
            if st is not None and st[0] == r - 1:
                st[0] = r
                st[1] += 1
                if st[1] > HOLD_LIMIT:
                    return 0          # given up: this standoff has stopped paying
            else:
                _held[uid] = [r, 1]
        _cached_target = my
        return MAX_SCORE
    if not can_move:
        return 0
    target, dist = nav.closest(claims, to_adjacent=False)
    if target is None or dist > 1:
        return 0                       # arrive late and the tile is already past
    _cached_target = target
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
