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
CUT_SCORE = 10
# Contesting the open end of an enemy line is the cheapest economic action in the
# bot — 3 Ti, one turn, no demolition — so it outranks cutting at their core.
LINE_SCORE = 11
MAX_SCORE = LINE_SCORE

# How far a builder will travel to contest an enemy line end.
LINE_RANGE = 7

# Tried and rejected: clearing enemy buildings off our own ore. It reads like the
# obvious counter to their denial, but the barrier trade only ever favours the
# side placing them — chipping a 30 HP barrier at 2 damage a turn is fifteen
# builder-turns and 30 Ti of attacks to undo something they rebuild for 3. It
# cost 3.1 points over the full suite (61.4% -> 58.3%, and 10 points against
# Khaos alone). Losing an ore tile is cheaper than fighting for it: the harvest
# state simply picks another.

# Only builders already this close to the enemy core take the cut job.
CUT_RANGE = 8

# Tried and rejected twice, and the second attempt settles the question. Passive
# *stealing* -- the same has_op-gated helper, but splicing our conveyor onto the
# line end (income) instead of a barrier (denial), which is worth several times
# more when it lands -- never fired at all across four maps. The bottleneck is
# not which action we take when adjacent to one of these tiles; it is that
# builders are essentially never adjacent to one. Do not retry the passive family
# without first changing where builders go.
#
# Tried and rejected: sealing as a *passive* action. A helper run after the
# chosen state, gated on has_op so it only fired when the turn's move and action
# were both still unspent, barriering any enemy feed tile or conveyor line end we
# happened to be standing next to. Free by construction, and it did fire -- but
# only 0-3 times a game, because builders are rarely cardinally adjacent to those
# tiles. 63.6% against 64.4%: noise, and code for nothing.
#
# Tried and rejected: chipping down isolated enemy turrets instead of answering
# them with our own (2 Ti a turn against a build, a unit slot, ammo, and +10% on
# every future turret). Fired plenty once the isolation test was loosened to 2
# tiles -- 37 times in one game against Khaos -- and still lost: 62.5% against
# 64.4%. Twenty builder-turns to remove one turret is not worth it on average,
# though the split is interesting (Heimdall v3 54.5% -> 60.6%, Khaos 65.2% ->
# 57.6%), so it may be worth revisiting as an opponent-conditional behaviour.

# Targets we stood next to and could not act on, and the round each becomes
# eligible again.
RETRY_COOLDOWN = 30
_blocked_until: dict = {}

_cached_target: Position | None = None
_cached_kind = ""




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
    out = []
    for x in (core.x, core.x + 1):
        out.append(Position(x, core.y - 1))
        out.append(Position(x, core.y + 2))
    for y in (core.y, core.y + 1):
        out.append(Position(core.x - 1, y))
        out.append(Position(core.x + 2, y))
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
    global _cached_target, _cached_kind
    _cached_target = None
    _cached_kind = ""

    my_pos = map_info._my_pos
    w = map_info._width
    my_bit = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)

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
            if mine:
                best = min(map_info.iter_mask(mine),
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
        now = rc.get_current_round()
        usable = [p for p in map_info.iter_mask(mine)
                  if _blocked_until.get(p, -1) <= now]
        if not usable:
            continue
        best = min(usable, key=lambda p: (my_pos.distance_squared(p), p.x + p.y * w))
        _cached_target = best
        _cached_kind = kind
        return CUT_SCORE
    return 0


def run():
    global _blocked_until
    target = _cached_target
    my_pos = map_info._my_pos
    adjacent = abs(target.x - my_pos.x) + abs(target.y - my_pos.y) == 1

    if adjacent:
        if _cached_kind == "steal":
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
        elif _cached_kind == "seal":
            if rc.can_build_barrier(target) and \
                    rc.get_global_resources() >= rc.get_barrier_cost() + map_info.ti_reserve():
                log(f"CUT: sealing enemy feed tile {target}")
                rc.build_barrier(target)
                map_info.update_at(target)
                return
        elif rc.can_fire(target) and rc.get_global_resources() >= 2:
            log(f"CUT: demolishing enemy supply conveyor {target}")
            rc.fire(target)
            return
    if adjacent:
        # Standing next to the target with nothing legal to do. Moving is a
        # no-op from here, so stand down for a while and let another state have
        # the turn rather than re-picking this tile every round.
        _blocked_until[target] = rc.get_current_round() + RETRY_COOLDOWN
        return
    nav.move_adjacent(target)
