"""The lone opening builder's job: a little economy, then a sentinel siege.

Three phases, driven by what the builder can see:

  HARVEST  Up to OPENING_HARVESTERS harvesters near home -- "a few (possibly none
           or one)". Not an economy; just enough income to keep the siege's
           ammunition paid for. Implemented by scoring 0 so harvest/route win.
  TRAVEL   Walk to the enemy core. Once committed the builder never goes home.
  SIEGE    Build sentinels on PURPLE (class C) tiles -- see siege.py -- until we
           hold `target_sentinels()` of them, then rebuild whatever dies.

Why sentinels rather than a builder chipping the core: a builder attack is 2 dmg
for 2 Ti and puts a 40 HP body next to their defenders; a sentinel is 9 dmg/round
for 5 Ti/round, outranges every builder, and its shot ignores walls entirely.
rush.py prices that exchange against an assumed heal rate and picks the cheapest n.

The real constraint is that SENTINELS BLOCK. Each one removes a tile from our own
walkable set, so a nearest-first build order walls the builder away from the sites
it has not built yet. `_next_build` builds the FARTHEST reachable site first and
then verifies, by simulating the placement, that every site still wanted keeps a
reachable stand tile.
"""
from _config import (OPENING_HARVESTERS, RUSH_ASSUMED_ENEMY_HEALERS,
                     RUSH_MAX_SENTINELS, RUSH_SENTINEL_HP, RUSH_COMMIT_ROUND,
                     RUSH_COMMIT_TI_FRACTION)
from main import has_op
import map_info
import pathing
from pathing import Pathing
import siege
import rush
import comms
import units.builder
from fcode import *
from log import log

rc: Controller = None
nav: Pathing = None

_PHASE_HARVEST, _PHASE_TRAVEL, _PHASE_SIEGE = 0, 1, 2
phase = _PHASE_HARVEST
harvesters_built = 0

# TOP priority -- above block (10), heal (9.5) and attack (9).
#
# 8.5 was tried first, on the reasoning that the rusher should still defend
# itself. Measured on duel: the rusher spent its titanium on SEVEN gunners and
# built ONE sentinel, because attack (9) outranked the rush on every turn an
# enemy was visible, which near their core is every turn. The rush is the plan;
# nothing may outrank it.
#
# This is safe for the rest of the fleet because `score()` returns 0 immediately
# for any builder that is not the rusher, so the only unit this reorders is the
# one that is supposed to be reordered. The rusher still heals: builder.run()
# calls heal._do_best_heal() unconditionally after the state runs, so a damaged
# sentinel it is standing next to is topped off for free.
MAX_SCORE = 10.5

_cached = None          # ("build", Position, delta) | ("walk", Position) | None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def am_rusher() -> bool:
    """The round-0 builder is the rusher. comms derives pair 0 from its spawn
    round, so this is stable all game and costs no extra broadcast."""
    return comms._my_pair == 0


def _enemy_core_anchor():
    core = map_info._their_core
    if core is not None:
        return (core.x, core.y)
    pred = map_info._predicted_enemy_core
    return None if pred is None else (pred.x, pred.y)


def _sentinel_sites() -> int:
    """Tiles a sentinel of ours could legally occupy: on the board, not a wall,
    nothing already there. Ore stays eligible -- the engine allows a sentinel on
    ore (probe-verified in the scratchpad note), and denying them an ore tile in
    passing is a bonus rather than a cost."""
    return (map_info._board_mask
            & ~map_info._bm_env[map_info._IDX_ENV_WALL]
            & ~map_info._bm_any_building)


def target_sentinels() -> int:
    """How many sentinels this siege wants, from rush.py's cost model.

    Their heal rate is the one input we cannot observe, so we assume
    RUSH_ASSUMED_ENEMY_HEALERS builders (the brief's default of 2 -> 8 HP/round)
    and take the n minimising total titanium. `a` is read from our LIVE sentinel
    price, so the model prices our next sentinels rather than a fresh scale.
    """
    a = rc.get_sentinel_cost() / rush.BASE_PRICE
    r = rush.HEAL_PER_BUILDER * RUSH_ASSUMED_ENEMY_HEALERS
    n, _cost = rush.best_n(RUSH_SENTINEL_HP, r, a)
    if n is None:
        n = RUSH_MAX_SENTINELS       # cannot out-damage the assumed heal: bring the cap
    return max(1, min(n, RUSH_MAX_SENTINELS))


def my_sentinels_near(anchor) -> int:
    """Our sentinels that can actually shoot the enemy Core -- class B, from
    siege.hit_mask.

    Counted off the live map rather than remembered, so a sentinel shot down
    stops counting and gets rebuilt on the next turn. That rebuild loop is the
    whole answer to "sentinels can be shot down": we never track what we built,
    only what is standing.

    Measured against a BOX around their core first, which was wrong: defensive
    sentinels the attack state placed near our own core fell inside the box on
    small maps, `have` reached `want` without the rusher building anything, and
    the rush silently cancelled itself. Class B is the honest test -- a sentinel
    counts iff some facing of it reaches a Core tile.
    """
    mine = (map_info._bm_et[map_info._IDX_SENTINEL]
            & map_info._bm_team[map_info._my_team_idx])
    if not mine:
        return 0
    return (mine & siege.hit_mask(anchor)).bit_count()


def _threat_mask() -> int:
    """Tiles where a sentinel of ours would be under enemy turret fire.

    Enemy SENTINELS contribute their current-facing ray only -- `rotate()` is
    gunner-only, so that facing is frozen for the rest of the game.
    Enemy GUNNERS contribute the union of ALL EIGHT rotations: a gunner turns for
    10 Ti and one turn of cooldown, so a tile it merely *could* face is not safe
    ground for a 30 Ti building that can never move.
    """
    return (map_info._bm_enemy_sentinel_threat
            | map_info.enemy_gunner_threat_any_rotation())


def rush_affordable():
    """(ok, n, cost) -- does the bank hold what the model says the rush costs?

    `rush.best_n` returns C_build + C_op: the sentinels AND the ammunition to
    fire them until the core dies. Committing without it is the worst outcome
    available -- the builder crosses the map, plants what it can afford, and the
    sentinels then stand there unable to shoot, having handed the enemy free
    titanium and a free target.

    C_op is spent as ammunition OVER the kill, not up front, so demanding 100% of
    it before leaving is stricter than the model requires; RUSH_COMMIT_TI_FRACTION
    is the knob for that.
    """
    a = rc.get_sentinel_cost() / rush.BASE_PRICE
    r = rush.HEAL_PER_BUILDER * RUSH_ASSUMED_ENEMY_HEALERS
    n, cost = rush.best_n(RUSH_SENTINEL_HP, r, a)
    if n is None:
        return False, None, cost
    # Split the model's two terms, because they are paid on different clocks.
    #
    # C_build is due UP FRONT -- the sentinels have to exist before any of this
    # works, so it must be in the bank now.
    #
    # C_op is ammunition, spent over the kill (H / (9n - R) rounds), and our
    # harvesters keep paying during it. Requiring it up front measured as a hard
    # veto: on saga the gate read ti=463 against need=521 at round 2 and never
    # opened all game -- and `need` climbed to 614 as our own home turrets pushed
    # the sentinel price scale up. So credit the income the siege will actually
    # earn while it runs, and require only the shortfall.
    build = rush.c_build(n, a)
    op = rush.c_op(n, RUSH_SENTINEL_HP, r)
    kill_rounds = RUSH_SENTINEL_HP / max(1, rush.DAMAGE * n - r)
    raw = comms.core_income()
    # 127 is comms' "no income word broadcast yet" sentinel, not an income of 127.
    # Taken literally it projects 317 Ti/round and opens the gate on round 0 for
    # free -- the exact opposite of the check. Unknown income counts as zero, so
    # an unproven economy has to fund the whole operating cost from the bank.
    income = 0.0 if raw >= 127 else raw * 10.0 / 4.0   # stacks/4-rounds -> Ti/round
    shortfall = max(0.0, op - income * kill_rounds)
    need = (build + shortfall) * RUSH_COMMIT_TI_FRACTION
    return rc.get_global_resources() >= need, n, need


def _choose_plan(anchor, want, buildable):
    """Which `want` (tile, facing) pairs to build, as a greedy set cover.

    Ranked, per placement, in this order:
      1. NOT under enemy turret fire. The overriding term: a sentinel is immobile
         and 40 HP, and one placed inside a gunner's rotation arc is dead titanium.
      2. Most NEW delivery-ring tiles covered by this exact line. The 8 tiles
         cardinally adjacent to the core are the only ones that can feed it, so
         covering all 8 strangles delivery outright -- and no single line covers
         more than 2, which is why this has to be a cover and not a per-tile score.
      3. Purple (class C) -- the same line takes the core AND its quota of ring.
      4. Closest to the core, to break ties toward a shorter walk.

    Greedy rather than exact: with <= 10 picks over ~88 options the optimum is not
    worth a turn budget, and greedy set cover is within 1 - 1/e of it. Recomputed
    every turn, so a sentinel shot down simply re-enters the plan.
    """
    opts = siege.placement_options(anchor, buildable)
    if not opts:
        return [], 0
    threat = _threat_mask()
    chosen, covered, used = [], 0, set()
    for _ in range(want):
        best = best_key = None
        for opt in opts:
            n, d, bits, is_siege, dsq = opt
            if n in used:
                continue
            key = (1 if (threat >> n) & 1 else 0,
                   -bin(bits & ~covered).count("1"),
                   0 if is_siege else 1,
                   dsq)
            if best_key is None or key < best_key:
                best_key, best = key, opt
        if best is None:
            break
        chosen.append((best[0], best[1], best[3], bin(best[2]).count("1")))
        covered |= best[2]
        used.add(best[0])
    return chosen, covered


def _stand_tiles(n: int) -> int:
    """Cardinal neighbours of tile n a builder could stand on to build it."""
    w, h = map_info._width, map_info._height
    x, y = n % w, n // w
    m = 0
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < w and 0 <= ny < h:
            m |= 1 << (nx + ny * w)
    return m


def _reachable(start_bit: int, passable: int, cap: int = 30) -> int:
    """Flood fill from `start_bit` over `passable`, step-capped so a large open
    map cannot eat the turn budget."""
    seen = start_bit
    frontier = start_bit
    for _ in range(cap):
        frontier = map_info.expand_manhattan(frontier) & passable & ~seen
        if not frontier:
            break
        seen |= frontier
    return seen


def _next_build(sites, want: int):
    """(tile_index, delta) of the next sentinel to place, or None.

    `sites` is siege.placements() output -- purple first. The first `want` of
    them are the intended set. Among those we can reach a stand tile of, build
    the FARTHEST first (a sentinel is a wall; near-first is what seals us out),
    and only if simulating the placement leaves every other intended site with a
    reachable stand tile.
    """
    w = map_info._width
    my = map_info._my_pos
    my_bit = 1 << (my.x + my.y * w)
    passable = map_info.passable() | my_bit
    intended = list(sites[:want])
    if not intended:
        return None
    reach = _reachable(my_bit, passable)
    built = map_info._bm_any_building

    scored = []
    for (n, d, _is_siege, _hits) in intended:
        if built & (1 << n):
            continue                                  # something is already there
        if not (_stand_tiles(n) & ~built & reach):
            continue                                  # cannot get beside it
        dist = abs(n % w - my.x) + abs(n // w - my.y)
        scored.append((dist, n, d))
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])                  # farthest first

    for dist, n, d in scored:
        sim_reach = _reachable(my_bit, passable & ~(1 << n))
        ok = True
        for (m, _md, _ms, _mh) in intended:
            if m == n or (built & (1 << m)):
                continue
            if not (_stand_tiles(m) & ~built & sim_reach):
                ok = False
                break
        if ok:
            return (n, d)
    # No order preserves the whole set (a corridor site, say). Take the farthest
    # rather than stalling the siege forever on a corner case.
    return (scored[0][1], scored[0][2])


def score(can_move=True):
    global _cached, phase
    _cached = None
    if not am_rusher():
        return 0
    anchor = _enemy_core_anchor()
    if anchor is None:
        return 0                                      # nowhere to go yet

    ok, _model_n, _model_cost = rush_affordable()
    if phase == _PHASE_HARVEST and not ok:
        # Not yet able to pay for the rush the model prescribes -- keep doing
        # economy. Once TRAVELLING we do NOT re-check: turning back halfway is
        # strictly worse than arriving poor, and the balance dips every time a
        # home builder spends.
        return 0

    if phase == _PHASE_HARVEST:
        # Leave the opening to harvest/route until the quota is met -- OR until
        # the deadline. The quota alone deadlocks: `harvesters_built` only rises
        # when THIS builder lays a harvester, so a rusher that spawns with no
        # reachable ore (or loses the race for it to a teammate) sits in the
        # opening phase for the whole game and the rush never happens. The
        # deadline makes the economy phase best-effort, which is what "a few
        # (possibly none)" means.
        if (harvesters_built < OPENING_HARVESTERS
                and rc.get_current_round() < RUSH_COMMIT_ROUND):
            return 0
        phase = _PHASE_TRAVEL

    w = map_info._width
    have = my_sentinels_near(anchor)
    want = target_sentinels()
    comms.set_siege_sentinels(have)

    sites, _covered = _choose_plan(anchor, want, _sentinel_sites())
    if not sites:
        return 0

    if (have < want
            and rc.get_global_resources() >= rc.get_sentinel_cost() + map_info.ti_reserve()):
        nxt = _next_build(sites, want)
        if nxt is not None:
            n, d = nxt
            tile = Position(n % w, n // w)
            my = map_info._my_pos
            if not can_move and abs(tile.x - my.x) + abs(tile.y - my.y) != 1:
                return 0
            _cached = ("build", tile, d)
            phase = _PHASE_SIEGE
            return MAX_SCORE

    if not can_move:
        return 0
    # Either out of position or waiting on titanium: either way, close on the ring.
    n = sites[0][0]
    _cached = ("walk", Position(n % w, n // w))
    return MAX_SCORE


def run(can_move=True):
    if _cached is None:
        return
    # avoid_turret=False on both legs. The default pathing refuses to enter a
    # tile an enemy turret covers -- correct for an economy builder, fatal here:
    # every good siege tile next to their core is covered by their own turrets,
    # so the rusher walked in place for 35 turns and never built anything
    # (measured on saga: phase 2 from round 20, still have=0 at round 55). Taking
    # fire on the way in is the price of the plan, and the sentinel it plants
    # outranges the builders shooting back.
    if _cached[0] == "walk":
        log("RUSH-TRAVEL", _cached[1])
        nav.move_adjacent(_cached[1], allow_bots=True, can_move=can_move,
                          avoid_turret=False)
        return
    _, tile, d = _cached
    log("RUSH-SIEGE", tile, d)
    if nav.move_adjacent(tile, allow_bots=True, can_move=can_move,
                         avoid_turret=False):
        return                                        # still walking into position
    facing = siege.facing_for(d)
    if (rc.get_global_resources() >= rc.get_sentinel_cost() + map_info.ti_reserve()
            and rc.can_build_sentinel(tile, facing)):
        rc.build_sentinel(tile, facing)
        map_info.update_at(tile)
