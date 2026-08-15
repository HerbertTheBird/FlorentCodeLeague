"""Builder state: economic warfare on the enemy's conveyor network.

Two jobs, in priority order.

**Steal a line end.** A conveyor line under construction ends at a tile its last
segment already points at but nothing occupies yet. If that tile falls in our
half, we put *our* conveyor on it aimed at our core: conveyors accept input from
either team, so their harvester keeps pushing and the titanium now arrives at our
core. 3 Ti, one action, no demolition, and the route state finishes the chain.

**Cut their core feed.**

Titanium only reaches a core through a conveyor that is *cardinally* adjacent to
it and facing into it. A 2x2 core therefore has exactly eight tiles through which
every resource it will ever receive must pass — two per side. Deny those eight
tiles and the enemy's entire economy stops, however many harvesters they own.

Two ways to deny one, and the cheap one is much better:

  * **Barrier an empty feed tile** — 3 Ti, one turn, permanent until they break
    it. A barrier has 30 HP and a builder does 2 damage a turn, so clearing one
    costs them fifteen turns and 30 Ti of attacks against our 3.
  * **Destroy a conveyor already sitting on one** — 20 HP at 2 damage a turn is
    ten turns of a builder's life, and they can rebuild it for 3 Ti. Only worth
    doing once the empty tiles are sealed, because otherwise they simply re-route
    around us.

So this state seals first and demolishes second. Sealing also compounds: every
barrier we place is a tile they cannot re-route through, which makes the
remaining conveyors worth attacking.

Deliberately opportunistic rather than a dedicated rush. A builder only takes
this job when it is already near the enemy core — which the attack and explore
states send builders toward anyway — so cutting costs no extra travel and never
strands the economy.
"""

import map_info
import pathing
import units.defense as defense
import units.builder
from fcode import Controller, Direction, EntityType, Position
from log import log
from pathing import Pathing

rc: Controller = None
nav: Pathing = None

# Above attack (9): once a builder is standing next to the enemy's supply line,
# shutting it off beats whatever else it was going to do out there.
CUT_SCORE = 13
# Contesting the open end of an enemy line is the cheapest economic action in the
# bot — 3 Ti, one turn, no demolition — so it outranks cutting at their core.
LINE_SCORE = 11
# Tapping an enemy harvester is the cheapest economic action available: their
# harvester is already built and running, so we pay only for the conveyor, and
# every stack that comes to us is one that does not reach them. One 3 Ti
# conveyor is worth roughly a whole harvester of *relative* income.
TAP_SCORE = 12
# CUT_SCORE is 13 and TAP_SCORE is 12, so declaring TAP_SCORE here understated
# what score() can actually return. Inert today only because defense.ENABLED is
# False collapses defend.score() to {SIEGE_SCORE, 0} and nothing else declares
# 12 -- but re-enable the block/trap family and defend returning BLOCK_SCORE=12
# would stop cut being *scored at all* on exactly the turns it would have
# returned 13, which is the premise this whole module rests on.
MAX_SCORE = CUT_SCORE

# How far a builder will travel to contest an enemy line end.
LINE_RANGE = 7

# Tried and rejected: clearing enemy buildings off our own ore. It reads like the
# obvious counter to their denial, but the barrier trade only ever favours the
# side placing them — chipping a 30 HP barrier at 2 damage a turn is fifteen
# builder-turns and 30 Ti of attacks to undo something they rebuild for 3. It
# cost 3.1 points over the full suite (61.4% -> 58.3%, and 10 points against
# Khaos alone). Losing an ore tile is cheaper than fighting for it: the harvest
# state simply picks another.

# Map-wide: builders travel to the enemy core to seal it rather than only doing
# it when they happen to be nearby. Their core has exactly eight tiles through
# which every resource must pass, a barrier on each is 3 Ti, and a builder
# standing there is not needed afterwards. With the block/trap defence switched
# off we have the bodies to spare.
CUT_RANGE = 99

# Targets this builder has proved it makes no progress on, and the round each
# becomes eligible again. Two ways to earn an entry:
#
#   * We stood cardinally next to it and every action was illegal — a bot parked
#     on the tile, no titanium spare, a turret covering it.
#   * We were *not* adjacent, asked nav to close the gap, and did not move. A
#     builder boxed in by walls, its own buildings or other bots gets stay-put
#     back from the pathfinder every single turn, and score() cannot tell that
#     apart from a builder halfway through a legitimate long walk.
#
# Only the first was recorded before, and it is the rare one. Instrumenting the
# champion over the first 200 rounds of antler: 312 turns spent approaching a
# seal target, 247 of them (79%) moving the builder nowhere, one builder
# re-picking the same tile 142 rounds running — against 8 firings of the
# adjacent-only guard. saga was 111 of 155 with a 109-round streak.
#
# Those are not merely idle turns. select_best_state() walks states in MAX_SCORE
# order and breaks the moment the running best reaches the next state's ceiling,
# so a builder frozen at cut's 11-13 stops attack (9), route (5), harvest (4),
# heal (3), disrupt (2) and explore (1) from being *scored at all* for the length
# of the streak. cut is about a tenth of every builder-turn, so most of that
# tenth was being spent standing still at the highest non-emergency priority in
# the bot.
#
# The cooldown is deliberately coarse: a single stay-put buys 30 rounds off, even
# though some of those failures are a teammate blocking a corridor for one turn.
# The cost of being wrong is that this builder falls through to the economy for a
# while and then retries; the cost of being right and not acting is a builder
# lost for a hundred rounds, which is what the streaks above measure.
RETRY_COOLDOWN = 30
_blocked_until: dict = {}

_cached_target: Position | None = None
_cached_kind = ""
_pending_seal: Position | None = None
_pending_seal_until = -1
PENDING_SEAL_TTL = 12




def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _toward(src: Position, dst: Position):
    """Cardinal direction from `src` that most reduces the gap to `dst`."""
    dx, dy = dst.x - src.x, dst.y - src.y
    if dx == 0 and dy == 0:
        return None
    if abs(dx) >= abs(dy):
        return Direction.EAST if dx > 0 else Direction.WEST
    return Direction.SOUTH if dy > 0 else Direction.NORTH


def _enemy_core() -> Position | None:
    """Top-left corner of the enemy 2x2, observed if possible, else predicted."""
    if map_info._their_core is not None:
        return map_info._their_core
    return map_info._predicted_enemy_core


def feed_tiles(core: Position) -> list[Position]:
    """The eight tiles a conveyor could deliver into this 2x2 core from.

    Only cardinal neighbours count: a conveyor outputs to the single tile it
    faces, so the four diagonal corners of the ring can never feed the core and
    are not worth spending barriers on.
    """
    # Full 12-tile ring rather than the 8 cardinal feed tiles. Corners cannot
    # deliver into the core, but walling them still denies the enemy somewhere to
    # stand and build beside their own core.
    out = []
    for x in range(core.x - 1, core.x + 3):
        for y in range(core.y - 1, core.y + 3):
            if core.x <= x <= core.x + 1 and core.y <= y <= core.y + 1:
                continue
            out.append(Position(x, y))
    return [t for t in out if map_info.in_bounds(t)]


def protected_upstream() -> int:
    """Enemy conveyors that now feed *us*, and must not be touched.

    Once we splice our own conveyor onto the end of an enemy line, everything
    upstream of it is carrying titanium to our core — it has stopped being their
    supply and become ours. Without this, the rest of the bot promptly wrecks its
    own new income: the choke logic sees an enemy conveyor pointing at a gap
    further up the same line and barriers it, and the demolish logic sees enemy
    conveyors and chews them.

    Derived locally by walking `map_info._conv_reverse` upstream from our own
    conveyors, so it needs no comms slot. The builders that could sabotage a line
    are the ones within LINE_RANGE of it, and those can see it.
    """
    my_idx = map_info._my_team_idx
    mine = map_info._bm_conveyors & map_info._bm_team[my_idx]
    enemy = map_info._bm_conveyors & map_info._bm_team[1 - my_idx]
    if not mine or not enemy:
        return 0
    reverse = map_info._conv_reverse
    w = map_info._width
    protected = 0
    frontier = mine
    # Chains are short; the bound just stops a cycle from spinning.
    for _ in range(12):
        feeders = 0
        for p in map_info.iter_mask(frontier):
            n = p.x + p.y * w
            if n < len(reverse):
                feeders |= reverse[n]
        feeders &= enemy & ~protected
        if not feeders:
            break
        protected |= feeders
        frontier = feeders
    return protected


def _usable(claimed: int) -> list[Position]:
    """Our claimed tiles minus the ones we have already proved we cannot work.

    Filtered *after* claim_subset, never before, and that ordering is load
    bearing. The claim is a Voronoi partition of the candidate mask that every
    builder recomputes independently from shared board state, and agreement
    between builders depends on all of them feeding it the same mask.
    `_blocked_until` is private to one builder — nobody else can see which tiles
    it gave up on — so subtracting it from the mask we hand in would give us a
    different partition to our teammates and two builders would start claiming
    the same tile. Trimming our own share afterwards leaves everyone's partition
    identical, and still lets us fall back to another tile inside our own zone
    rather than dropping the whole state.
    """
    now = rc.get_current_round()
    return [p for p in map_info.iter_mask(claimed)
            if _blocked_until.get(p, -1) <= now]


def _enemy_harvester_taps() -> int:
    """Free tiles beside an enemy harvester where we could put a conveyor.

    A harvester feeds whatever sits beside it and conveyors accept input from
    either team, so our conveyor next to theirs takes a share of the output
    straight to our core -- no harvester of our own required.
    """
    enemy = map_info._bm_team[1 - map_info._my_team_idx]
    harvesters = map_info._bm_et[map_info._IDX_HARVESTER] & enemy
    if not harvesters:
        return 0
    adjacent = map_info.expand_manhattan(harvesters) & ~harvesters
    free = (map_info._bm_seen
            & ~map_info._bm_any_building
            & ~map_info._bm_env[map_info._IDX_ENV_WALL]
            & ~map_info._bm_friendly_bots
            & ~map_info._bm_enemy_bots
            & ~map_info._bm_enemy_turret_threat)
    candidates = adjacent & free
    if not candidates:
        return 0
    # Do not cap distance to our network. Tyr treats every known enemy
    # harvester as routable economic territory; route.py will extend the new
    # dead end home over subsequent turns.
    return candidates


def _open_enemy_line_ends() -> int:
    """Empty tiles an enemy conveyor is currently pouring into.

    A conveyor line under construction ends at a tile its last segment already
    points at but nothing occupies yet. That tile is the cheapest point on the
    whole line to contest, because one 3 Ti building there settles it
    permanently and we never have to chew through 20 HP of conveyor to do it.
    """
    enemy = map_info._bm_team[1 - map_info._my_team_idx]
    # Skip conveyors that are already feeding our core: choking their open end
    # would cut off our own stolen income.
    sources = (map_info._bm_conveyors & enemy) & ~protected_upstream()
    ends = map_info._conveyor_target_tiles(sources)
    return (ends
            & map_info._bm_seen
            & ~map_info._bm_any_building
            & ~map_info._bm_env[map_info._IDX_ENV_WALL]
            & ~map_info._bm_friendly_bots
            & ~map_info._bm_enemy_bots
            & ~map_info._bm_enemy_turret_threat)


def score():
    global _cached_target, _cached_kind, _pending_seal, _pending_seal_until
    _cached_target = None
    _cached_kind = ""

    if units.builder._econ_only or units.builder._stay_near_core:
        return 0

    my_pos = map_info._my_pos
    w = map_info._width
    my_bit = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)

    # Finish a cut before accepting another economic job. Every attack on an
    # enemy supply conveyor records the breach. While the conveyor survives we
    # keep demolishing it; on the first following turn that it is empty, wall
    # the gap so the opponent cannot replace a 3-Ti conveyor immediately. If a
    # friendly conveyor appears there, another state chose to route the line
    # back to us and no barrier is wanted.
    pending = _pending_seal
    if pending is not None:
        if rc.get_current_round() > _pending_seal_until or not map_info.in_bounds(pending):
            _pending_seal = None
        else:
            n = pending.x + pending.y * w
            bit = 1 << n
            my_team = map_info._bm_team[map_info._my_team_idx]
            enemy_team = map_info._bm_team[1 - map_info._my_team_idx]
            if bit & map_info._bm_conveyors & my_team:
                _pending_seal = None       # the breach became our route
            elif bit & map_info._bm_conveyors & enemy_team:
                _cached_target = pending
                _cached_kind = "demolish"
                return CUT_SCORE
            elif (bit & map_info._bm_seen
                  and not (bit & map_info._bm_any_building)
                  and not (bit & (map_info._bm_friendly_bots | map_info._bm_enemy_bots))
                  and not (bit & map_info._bm_env[map_info._IDX_ENV_WALL])
                  and defense.may_wall()
                  and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.ti_reserve()):
                _cached_target = pending
                _cached_kind = "pending_seal"
                return CUT_SCORE
            elif bit & map_info._bm_any_building:
                _pending_seal = None

    # Tap an enemy harvester first: cheapest swing per titanium in the bot.
    if rc.get_global_resources() >= rc.get_conveyor_cost() + map_info.ti_reserve():
        taps = _enemy_harvester_taps()
        if taps:
            mine = pathing.claim_subset(my_bit, map_info._bm_friendly_bots, taps, tie_self=True)
            # Taps and steals wrote `_blocked_until` in run() and then never read
            # it, so a tap tile we could not reach or could not build on stayed
            # top of the list every round. The seal loop below has always
            # filtered; these two now do the same.
            usable = _usable(mine)
            if usable:
                home = map_info._my_core
                # Prefer stealing harvesters closer to our base, regardless of
                # which builder happens to be closest. Builder distance remains
                # the secondary tiebreak so claims still divide the work well.
                best = min(usable, key=lambda p: (
                    p.distance_squared(home) if home is not None else 0,
                    my_pos.distance_squared(p),
                    p.x + p.y * w,
                ))
                _cached_target = best
                _cached_kind = "tap"
                return TAP_SCORE

    # Steal the open end of an enemy conveyor line that reaches into our half.
    # Put *our* conveyor on it, pointed home: conveyors accept input from either
    # team, so their harvester keeps pushing titanium and it now arrives at our
    # core instead of theirs. The route state picks the new segment up as a dead
    # end and extends the chain the rest of the way on its own. 3 Ti, one action,
    # no demolition.
    #
    # Restricted to our own harvest zone on purpose. The same machinery can
    # *barrier* line ends out in their half instead ("choke"), and that reads
    # like the same idea, but it measured 2.7 points worse over the full suite
    # (62.5% -> 59.8%): denial out there is a pure cost, while a steal in our own
    # half is income. Take the resource, do not just break it.
    if defense.may_wall():
        ends = _open_enemy_line_ends() & units.builder._harvest_zone
        if ends:
            mine = pathing.claim_subset(my_bit, map_info._bm_friendly_bots, ends, tie_self=True)
            usable = _usable(mine)
            if usable:
                best = min(usable,
                           key=lambda p: (my_pos.distance_squared(p), p.x + p.y * w))
                if my_pos.distance_squared(best) <= LINE_RANGE * LINE_RANGE:
                    _cached_target = best
                    _cached_kind = "steal"
                    return LINE_SCORE

    # Cutting their supply is worthless if we have not built ours: see
    # `defense.may_wall`.
    if not defense.may_wall():
        return 0
    core = _enemy_core()
    if core is None:
        return 0
    # Restricting the rush to builders already on their half was tried and
    # reverted. It won locally by a wide margin (83.3% against Ladder_v36 versus
    # 78.8%) and lost on the field: unrated games against the top five, grouped
    # by submission version, gave 64.2% game win without it and 55.0% with. It
    # did what it was designed to do -- sporks went 6-9 to 5-5 -- but Jython fell
    # 12-8 to 6-9 and team lazy 24-6 to 9-6. Fixing the worst matchup by holding
    # builders back cost more against the ones we were already beating.
    if my_pos.distance_squared(core) > CUT_RANGE * CUT_RANGE:
        return 0

    enemy_bm = map_info._bm_team[1 - map_info._my_team_idx]
    _protected = protected_upstream()
    # A tile with a bot standing on it can be neither built on nor fired at, and
    # the enemy is happy to leave one parked on its own feed tile. Without this
    # the bot walks adjacent, finds every action illegal, and run() falls through
    # to move_adjacent -- which is a no-op because it is already adjacent. score()
    # then re-picks the same tile next turn, forever: replays show builders
    # wedged this way for 400+ turns at a state that outranks the entire economy.
    occupied = map_info._bm_friendly_bots | map_info._bm_enemy_bots
    seal = 0        # empty feed tiles we can wall off
    demolish = 0    # enemy conveyors feeding the core
    for tile in feed_tiles(core):
        bit = 1 << (tile.x + tile.y * w)
        if map_info._bm_env[map_info._IDX_ENV_WALL] & bit or bit & occupied:
            continue
        if map_info._bm_conveyors & enemy_bm & bit:
            if not (bit & _protected):
                demolish |= bit
        elif not (map_info._bm_any_building & bit) and (map_info._bm_seen & bit):
            seal |= bit

    for kind, claims in (("seal", seal), ("demolish", demolish)):
        if not claims:
            continue
        mine = pathing.claim_subset(my_bit, map_info._bm_friendly_bots, claims, tie_self=True)
        if not mine:
            continue
        usable = _usable(mine)
        if not usable:
            continue
        best = min(usable, key=lambda p: (my_pos.distance_squared(p), p.x + p.y * w))
        _cached_target = best
        _cached_kind = kind
        return CUT_SCORE
    return 0


def run():
    global _blocked_until, _pending_seal, _pending_seal_until
    target = _cached_target
    my_pos = map_info._my_pos
    adjacent = abs(target.x - my_pos.x) + abs(target.y - my_pos.y) == 1

    if adjacent:
        if _cached_kind == "tap":
            home = map_info._my_core
            d = _toward(target, home) if home is not None else None
            if d is not None and rc.can_build_conveyor(target, d) \
                    and rc.get_global_resources() >= rc.get_conveyor_cost() + map_info.ti_reserve():
                log(f"TAP: conveyor beside enemy harvester at {target}")
                rc.build_conveyor(target, d)
                map_info.update_at(target)
                return
        elif _cached_kind == "steal":
            if rc.get_global_resources() >= rc.get_conveyor_cost() + map_info.ti_reserve():
                home = map_info._my_core
                d = _toward(target, home) if home is not None else None
                if d is not None and rc.can_build_conveyor(target, d):
                    log(f"STEAL: redirecting enemy line at {target} toward our core")
                    rc.build_conveyor(target, d)
                    map_info.update_at(target)
                    return
            # If we cannot point it home from here, deny it instead.
            if rc.can_build_barrier(target) and \
                    rc.get_global_resources() >= rc.get_barrier_cost() + map_info.ti_reserve():
                log(f"CUT: barriering unstealable line end {target}")
                rc.build_barrier(target)
                map_info.update_at(target)
                return
        elif _cached_kind in ("seal", "pending_seal"):
            if rc.can_build_barrier(target) and \
                    rc.get_global_resources() >= rc.get_barrier_cost() + map_info.ti_reserve():
                log(f"CUT: sealing enemy feed tile {target}")
                rc.build_barrier(target)
                map_info.update_at(target)
                if target == _pending_seal:
                    _pending_seal = None
                return
        else:
            n = target.x + target.y * map_info._width
            hp = map_info._building_hp[n]
            # On the killing blow, retain enough titanium to seal the empty
            # tile next turn. Earlier attacks need only their normal 2 Ti.
            needed = 2
            if 0 < hp <= 2:
                needed += rc.get_barrier_cost() + map_info.ti_reserve()
            if rc.can_fire(target) and rc.get_global_resources() >= needed:
                log(f"CUT: demolishing enemy supply conveyor {target}")
                rc.fire(target)
                _pending_seal = target
                _pending_seal_until = rc.get_current_round() + PENDING_SEAL_TTL
                return
    if adjacent:
        # Standing next to the target with nothing legal to do. Moving is a
        # no-op from here, so stand down for a while and let another state have
        # the turn rather than re-picking this tile every round.
        _blocked_until[target] = rc.get_current_round() + RETRY_COOLDOWN
        return

    # Not adjacent: walk. The same stand-down applies when the walk itself gets
    # nowhere, which is the far more common failure — see `_blocked_until`. Read
    # `map_info._my_pos` either side of the call rather than trusting the return
    # value: `update_move()` rewrites `_my_pos` on every successful step, so the
    # comparison is a direct observation of whether we actually left the tile,
    # whereas move_adjacent's boolean lies in both directions. It returns False
    # for a target we are already standing on, and True for the stuck-turns
    # escape hatch inside move_to, which fires a move in an arbitrary legal
    # direction that need not be toward the target at all.
    #
    # This is not the turret-avoidance detour it looks like. Re-running the same
    # call with avoid_turret=False over the 247 antler failures and the 111 saga
    # ones returned stay-put in every single case, so the builder is genuinely
    # enclosed and no amount of patience will get it there.
    before = map_info._my_pos
    nav.move_adjacent(target)
    if map_info._my_pos == before:
        _blocked_until[target] = rc.get_current_round() + RETRY_COOLDOWN
