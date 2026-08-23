"""The four behaviours: rusher, economy builder, sentinel, launcher.

Each unit runs in its own interpreter, so these are written as free functions
over a per-unit `State` rather than as anything shared. The only information that
crosses between units is the comms store, and the only things on it are the three
words in config.

Role assignment goes through the store: the core writes the rusher's entity id to
SLOT_RUSHER_ID on the turn it spawns it, and every builder compares that against
its own id on its first run. A builder spawned on round N does not run until
round N+1, so the one-round write delay costs nothing -- the value is already
there when the unit looks.
"""

import config
import geom
from geom import SENTINEL_CARDINAL_REACH
from fcode import Direction, EntityType, Environment, GameConstants, Position


class State:
    """Per-unit memory. Rebuilt from nothing if the unit is a fresh spawn."""

    def __init__(self, ct):
        self.board = geom.Board(ct)
        # A builder spawned on round N does NOT run on round N -- the core spawns
        # it during its own turn and the new unit's first run() is round N+1. So
        # the rusher's first round is 1, not 0, which is worth writing down
        # because getting it wrong silently turns the rush into a no-op.
        self.spawn_round = ct.get_current_round()
        self.is_rusher = False
        self.is_healer = False
        self.healers = 0
        self.core_max = 0
        self.last_spawn_id = None
        self.raider_noted = False
        self.saw_defender = False
        self.blocked = 0
        self.dead_turns = 0
        self.stalled = 0
        self.grip = set()
        self.thrown = 0
        self.known_grip = set()
        self.last_seen_at = None
        self.last_placed = 0
        self.base_empty = False
        self.ring_built = set()
        self.ring_done = False
        self.arrived = False
        self.placed = 0
        self.launcher_built = False
        self.launcher_pos = None
        # Tiles where a sentinel of ours has already been destroyed. A
        # sentinel is 30+ titanium and rebuilding one on the tile that just
        # ate it is how the whole bank disappears into the same killzone --
        # observed four times on one tile in a single game.
        self.dead_sites = set()
        # Tiles a sentinel of ours died on, plus every tile on the eight rays out
        # of them within DEAD_RAY_SPAN. The killer is a fixed-facing turret whose
        # line covers a whole row, so the tile next door is no safer than the one
        # that just lost a turret.
        self.dead_rays = set()
        self.built_sites = set()
        # When each sentinel went up, and how many have died young. Two dying
        # within FAST_DEATH_TURNS means the face we are on is covered by
        # something we cannot shoot back at, and every further turret placed
        # there is donated. Traced on two ladder games: five sentinels built six
        # or seven turns apart, peak alive ONE, 240 titanium spent, with 179 and
        # 229 still sitting in the bank.
        self.built_at = {}
        self.fast_deaths = 0
        # Sentinels ever built, as distinct from sentinels alive. The
        # live count decides whether to rebuild; this is the backstop that
        # stops rebuilding forever. Without it, moonrise built and lost 54
        # sentinels over 1000 turns, feeding the whole bank one turret at a
        # time into a tile the defender had already proved it could reach.
        self.ever_built = 0
        # Turns spent neither building nor moving. herbert19 body-blocks:
        # it parks a builder on the tile ahead and mirrors every sideways
        # step, and because a builder may only move cardinally that closes
        # the axis completely. Traced on ragnarok, the rusher stood at
        # (28,24) -- gap 3, in range, four legal sites around it -- from
        # turn 55 to the end of the game, because the spot it preferred was
        # on the far side of the blocker. A rush that will not settle for a
        # good tile loses to one bot standing in a doorway.
        self.idle = 0
        # Best gap to the enemy core ever reached, and turns since it improved.
        # Counting only turns spent STANDING STILL misses the way a body-block
        # actually presents: the blocker mirrors us, we step aside, it steps
        # across, we step back -- and a two-cycle looks like movement. Traced on
        # helheim, the rusher oscillated (6,7)<->(6,8) from turn 12 to the end of
        # the game at gap 3 with a defender shadowing it at (5,7)/(5,8), and the
        # idle counter never once incremented.
        self.best_gap = None
        self.since_progress = 0
        self.last_pos = None
        self.prev_pos = None
        self.eco_target = None       # ore tile this economy builder owns
        self.harvested = False
        self.my_id = ct.get_id()
        # Core-only bookkeeping.
        self.rusher_named = False
        self.eco_spawned = 0


# =============================================================================
# shared helpers
# =============================================================================
def _cardinal_neighbours(p):
    for d in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
        dx, dy = d.delta()
        yield Position(p.x + dx, p.y + dy), d


def _read_enemy_core(ct, st):
    """Our own guess, falling back to whatever another unit has published.

    A unit that has been thrown across the map, or one that spawned late, may
    have seen too little terrain to settle the symmetry itself. Slot 2 carries
    the first settled answer anybody reached.
    """
    mine = st.board.enemy_core()
    if st.board.settled() and mine is not None:
        return mine
    packed = ct.read_store(config.SLOT_ENEMY_CORE)
    if packed:
        n = packed - 1
        return Position(n % st.board.w, n // st.board.w)
    return mine


def _publish_enemy_core(ct, st, core):
    if core is not None and st.board.settled():
        ct.write_store(config.SLOT_ENEMY_CORE,
                       core.x + core.y * st.board.w + 1)


def _note_visible_cores(ct, st):
    """Latch our own core, and the enemy's if we ever actually see it.

    Sight beats symmetry: a confirmed core needs no hypothesis, and on a map
    where two candidates survive to the end this is what settles it.
    """
    me = ct.get_team()
    for bid in ct.get_nearby_buildings():
        try:
            if ct.get_entity_type(bid) != EntityType.CORE:
                continue
            p = ct.get_position(bid)
        except Exception:
            continue
        # get_position returns the core's reference tile, its top-left corner.
        if ct.get_team(bid) == me:
            st.board.my_core = p
        else:
            st.board.their_core = p


# =============================================================================
# the rusher
# =============================================================================
def rusher_turn(ct, st):
    core = _read_enemy_core(ct, st)
    _publish_enemy_core(ct, st, core)
    ct.write_store(config.SLOT_SENTINELS, st.placed)
    # Heartbeat. The core holds titanium back for sentinels this unit has
    # not built yet; if it dies on the way that reserve would be held for
    # the rest of the game while the sentinels already down run dry.
    ct.write_store(config.SLOT_HEARTBEAT, ct.get_current_round() + 1)
    if core is None:
        _wander_toward_centre(ct, st)
        return

    me = ct.get_position()
    if me.distance_squared(core) <= config.ARRIVE_DIST_SQ:
        st.arrived = True
    if st.arrived:
        ct.write_store(config.SLOT_ARRIVED, 1)

    gap = min(abs(me.x - t.x) + abs(me.y - t.y)
              for t in st.board.core_tiles(core))

    # Is anyone home to punish a slow approach? Latched, and only once we are
    # close enough that a builder in sight is a DEFENDER rather than their
    # rusher passing us on its way to our core.
    if not st.saw_defender and _opposition(ct, st, gap):
        st.saw_defender = True

    if (not st.base_empty and gap <= config.ARRIVED_GAP
            and _enemy_base_empty(ct, st, core)):
        # We are at their core and nothing is home: no conveyor, no harvester,
        # no turret, no builder. Their whole army is the one bot walking at us.
        ct.write_store(config.SLOT_BASE_EMPTY, 1)
        st.base_empty = True

    # Watchdog. Counted before any of the early returns below, so it sees a
    # turn in which we decided to do nothing just as well as one in which we
    # threw. Twelve turns of a builder neither moving nor building is not a
    # tactical choice, it is a bug -- see panic().
    if (st.last_seen_at == (me.x, me.y) and st.placed == st.last_placed):
        st.dead_turns += 1
    else:
        st.dead_turns = 0
    st.last_seen_at = (me.x, me.y)
    st.last_placed = st.placed
    # NOT once the battery is up: standing still is the terminal state's whole
    # job -- it parks on the hub and heals its turrets, and the reference bot
    # spends 74% of those turns deliberately doing nothing. Only an UNFINISHED
    # battery makes a frozen builder a bug.
    unfinished = (st.placed < config.SENTINEL_TARGET
                  and st.ever_built < config.MAX_EVER_BUILT)
    if unfinished and st.dead_turns >= config.STUCK_LIMIT:
        st.dead_turns = 0
        if panic(ct, st):
            return

    # Only fear a launcher that has actually thrown us.
    #
    # A throw is the one thing that moves a builder more than one tile in a
    # turn, so it is trivially detectable after the fact -- and detecting it is
    # much better than avoiding every launcher on the board. ph's launcher sits
    # beside its core and threw us twenty times on glacierkeep; herbert19 builds
    # launchers too and never throws us once, and detouring around those cost
    # 81.1% -> 74.4%. So: walk in normally, and if something picks us up, from
    # then on route around anything that could do it again.
    if (st.last_seen_at is not None
            and abs(me.x - st.last_seen_at[0])
            + abs(me.y - st.last_seen_at[1]) > 1):
        st.thrown += 1
    # REMEMBERED, not looked up. Vision is about four tiles, so backing away
    # from a launcher takes it out of sight, empties the grip, and walks us
    # straight back into it -- which is why avoidance changed nothing at first:
    # 25 launches on glacierkeep with the feature on. A launcher is a building;
    # it does not move; once seen it is known.
    _note_launchers(ct, st)
    st.grip = (st.known_grip
               if st.thrown >= config.THROWS_BEFORE_AVOID else set())
    _note_lone_raider(ct, st)
    _note_dead_sentinels(ct, st)
    if (st.placed >= config.SENTINEL_TARGET
            or st.ever_built >= config.MAX_EVER_BUILT):
        # Full battery. This is a TERMINAL state: park on the hub and heal, never
        # move, never go looking for anything. The reference bot spends 74% of
        # its post-battery turns doing literally nothing and holds one tile for
        # the whole window in 12 of 15 games -- its park tile has a mean 1.87 of
        # its four orthogonal neighbours occupied by its own turrets, so standing
        # still is what keeps it in heal range of them.
        #
        # Healing is the job, and it is a fight we win: +4 HP a turn for 1
        # titanium against an enemy builder's 2 damage. It only loses to enemy
        # TURRETS (7 or 18 a shot), which is the real threat model.
        # A gunner shooting one of our sentinels is answerable for 3 titanium:
        # gunner shots ARE stopped by obstacles (sentinel shots are not), so a
        # barrier anywhere on its ray turns off a 20-titanium turret completely,
        # and when they shoot the barrier down that is 30 HP of their ammunition
        # against 3 of ours to replace it. Nothing in fifteen reference games
        # ever did this, and turrets are the ONLY thing that kills a rush
        # sentinel -- across those games enemy builder attacks dealt exactly zero
        # damage to the battery.
        # The battery is up and firing, and this state is idle: the reference
        # bot spends 74% of its post-battery turns doing literally nothing. So
        # wall their core in with the turns we were going to waste. Eight
        # barriers at 3 titanium are the cheapest thing in the game and they
        # permanently deny the one counter that beats us -- a builder repairing
        # their core at 4 HP per titanium against the 1.8 our shots buy.
        if (config.BARRIER_RING and not config.BARRIER_BEFORE_SENTINELS
                and not st.ring_done and _rush_mirror(ct, st)
                and _enemy_base_empty(ct, st, core)
                and _try_barrier_ring(ct, st, core,
                                      geom.flood(st.board, ct.get_position()))):
            return

        if _screen_gunner(ct, st):
            return
        if _heal_neighbour(ct, st):
            return
        _chip_adjacent_turret(ct, st)
        return
    # One flood per turn, shared by the ranking (which stand tiles can we get to)
    # and by the walk (which way is the first step). Recomputed rather than
    # cached because the terrain we know, and the buildings blocking it, change
    # on every turn of the approach.
    # NOT geom.flood(st.board, me, core). Routing the flood to prefer staircase
    # paths over L-shaped ones is a real diagnosis -- our walk costs +12, +5 and
    # +31 turns of overhead on maps where not adgato pays +0, +2 and +17 -- and
    # it measured 81.1% -> 75.6% against herbert19. It fixed glacierkeep (1->0)
    # and midgard (3->0) and got us to the core on longhouse where we previously
    # never arrived, but cost 3->14 on icefloe, and the win rate says the losses
    # outweigh the wins. The `toward` argument is left in geom.flood for whoever
    # picks this up: the overhead gap is real and still unclaimed.
    # PATHFIND around a launcher that has thrown us.
    #
    # The step-level rule only refuses the next tile, so it walks the boundary
    # of the grip while the route it is following still runs through the middle
    # of it -- 23-26 launches on glacierkeep with avoidance armed. Taking those
    # tiles out of the flood is what makes the whole route go round.
    #
    # Falls back to an unblocked flood when that would leave no path to the core
    # at all: being thrown is bad, never arriving is worse.
    reach = geom.flood(st.board, me, blocked=st.grip) if st.grip else None
    if not reach or not any((t.x, t.y) in reach for t in _ring(st.board, core)):
        reach = geom.flood(st.board, me)
    # Wall their core in before shooting it, if nobody is home to stop us.
    if (config.BARRIER_RING and config.BARRIER_BEFORE_SENTINELS
            and not st.ring_done and st.placed == 0
            and gap <= config.ARRIVED_GAP and _rush_mirror(ct, st)
            and _try_barrier_ring(ct, st, core, reach)):
        return

    stand = _pick_stand(ct, st, core, reach)
    if stand is None:
        # Nowhere reachable has a free site beside it.
        #
        # If we have ALREADY ARRIVED, walking at the core ring is useless -- we
        # are on it. Measured over 305 ladder games, 31 of them reach the core
        # and never build a single sentinel, and in those games 45% of the four
        # tiles the rusher could build on were FREE. They simply were not firing
        # positions: nothing adjacent had a line to the core, and the fallback
        # kept walking at a ring we were standing on.
        #
        # So once arrived, walk at the stand tiles of any free site instead. The
        # gate is deliberately narrow -- an earlier version fired on every
        # `stand is None`, including during the approach, and cost 0.75
        # sentinels a game by pulling the rusher off good hubs to chase distant
        # sites.
        if gap <= config.ARRIVED_GAP:
            targets = []
            for q, _d in geom.sentinel_sites(st.board, core):
                if (q.x, q.y) in st.dead_sites or not _tile_free(ct, st, q):
                    continue
                targets.extend(_stand_tiles(q))
            if targets:
                _step_toward(ct, st, targets, reach)
                return

        # Not arrived yet: close on the core -- its own tiles are solid, so aim
        # at the ring around it.
        #
        # The obvious improvement is to walk at the stand tiles of any free site
        # anywhere instead, and it is a REGRESSION: it pulls the rusher off a
        # good hub to chase distant sites and finishes with fewer turrets.
        # Measured on ladder replays, sentinels built per game 4.25 -> 3.50, and
        # games with an incomplete battery 1/20 -> 15/40.
        _step_toward(ct, st, _ring(st.board, core), reach)
        return

    _score, spot, sites = stand
    # Track being stuck, two ways. `idle` is turns spent on the same tile;
    # `since_progress` is turns since we last got closer to the core than we have
    # ever been. The second is what catches a body-block, because a mirrored
    # two-cycle keeps moving without ever getting anywhere.
    if st.last_pos == (me.x, me.y):
        st.idle += 1
    else:
        st.idle = 0
        st.prev_pos = st.last_pos
    st.last_pos = (me.x, me.y)
    if st.best_gap is None or gap < st.best_gap:
        st.best_gap = gap
        st.since_progress = 0
    else:
        st.since_progress += 1

    # Stalled in range: stop shopping and build from here. `here` is whatever
    # this tile can offer, which is worth more than a better tile we have been
    # unable to reach for COMMIT_AFTER_STALL turns.
    # The no-progress trigger applies only BEFORE the battery is started. Once
    # the rusher has arrived it stops advancing on purpose, so the gap stops
    # improving and the counter would climb forever -- which is exactly what it
    # did: it overrode the stand-tile choice and dumped sentinels on whatever
    # tile happened to be adjacent, for -13 points on the pool.
    if (st.idle >= config.COMMIT_AFTER_STALL
            or (st.placed == 0
                and st.since_progress >= config.COMMIT_AFTER_NO_PROGRESS)):
        here = _sites_around(ct, st, core, me)
        if here and _try_build_sentinel(ct, st, here):
            st.idle = 0
            return

    # Once the battery is OPEN, never walk away from a site we could build on.
    #
    # This is the whole assembly span. Traced on valkyrie against not adgato:
    # build (26,17), step east, build (27,17), step east, build (28,17), step
    # three times, build (29,15) -- eight turns for four sentinels, and the tile
    # it stepped ONTO at t37 was itself a site it could have built from where it
    # already stood. It re-shops for a better hub every turn and pays a turn of
    # tempo each time. not adgato plants its four in three turns, t32-t35, and
    # that five-turn difference is most of the nine turns we lose the race by.
    #
    # Choosing the first hub carefully is still right -- that decision is made
    # above, while placed == 0 -- but after that the best tile is the one we are
    # standing on, because it costs no turn to be there.
    # ...but only when nobody is contesting the spot. Against a defender the
    # careful hub is worth its tempo (-6.7 points against herbert19 with this
    # on); against a rusher, who never contests anything, tempo IS the game
    # (+13.3 in the mirror). Same precondition as the far face, for the same
    # reason, and measured the same way.
    if (config.BUILD_BEFORE_MOVING and st.placed > 0
            and not st.saw_defender):
        here = _sites_around(ct, st, core, me)
        if here and _try_build_sentinel(ct, st, here):
            return

    if (spot.x, spot.y) != (me.x, me.y):
        _step_toward(ct, st, [spot], reach)
        return

    # Do not OPEN the battery on a spot that cannot finish it.
    #
    # This is what the reference bot's long loiter actually is. Measured over
    # 15 games it won and 31 it lost, arrival and loiter are identical either
    # way (t22.7 / 13.5 turns against t21.9 / 14.4) and the ONLY thing that
    # differs is how long the battery takes to assemble: 4.3 turns in the wins,
    # 20.2 in the losses. Ours is 7.3, with a tail out to 22 -- we loiter less
    # than it does and pay for it at the other end.
    #
    # A battery that goes up in four turns is at its full 36 damage a round
    # before the defender has anything that shoots; one that dribbles out gets
    # farmed a turret at a time. So the first sentinel waits for a hub that can
    # produce a whole battery, and after that we build every turn regardless.
    if (st.placed == 0 and len(sites) < config.MIN_SITES_TO_OPEN
            and st.since_progress < config.OPEN_ANYWAY_AFTER
            and _defenders_near(ct, st)):
        _step_toward(ct, st, _ring(st.board, core), reach)
        return

    # Do not OPEN the battery under a defender's nose. Circle until it leaves.
    #
    # This is what the reference bot is doing during the long approach that
    # looks like dithering. Measured over five of its games against ph, the
    # nearest enemy builder AT THE MOMENT IT PLANTS ITS FIRST SENTINEL is 5, 6,
    # 6, 5 and 3 tiles away, and it builds 2.5x faster per turn on turns with no
    # builder within four. Ours were 2, 1, 2 and 3, with 96% of every sentinel
    # we built going up with a defender inside four tiles -- and they died at
    # t47, t91, t106, t126, t150, one at a time, fed in singly.
    #
    # A turret is 30+ titanium and takes a turn to place; a builder standing
    # next to it destroys it for 2 titanium a hit. Waiting for the neighbourhood
    # to clear costs turns, but opening early costs the whole battery.
    # ever_built, NOT placed: `placed` falls back to zero when a turret dies, so
    # gating on it made every REBUILD wait for a clearing too, and against a bot
    # whose builders never leave that pushed the fourth sentinel from t51 to
    # t92. Waiting is worth it to place a battery into open ground; it is not
    # worth it to replace one turret in a fight already under way.
    # Bounded by its OWN counter, not by since_progress. since_progress resets
    # whenever we get closer to the core than ever before, and circling does
    # that by accident, so the wait was effectively unbounded: against ph the
    # first sentinel slipped to t119, t128, t127 where not adgato opens at
    # t30-44, and one game never opened at all. A stall that never ends is just
    # a bot that does not attack.
    if (config.STALL_UNTIL_CLEAR and st.ever_built == 0
            and st.stalled < config.STALL_MAX_TURNS):
        foes = _enemy_builder_positions(ct)
        crowd = sum(1 for f in foes
                    if max(abs(me.x - f[0]), abs(me.y - f[1]))
                    < config.OPEN_CLEAR_DIST)
        # A CROWD is worth waiting out; one loiterer is not.
        #
        # Measured over the rusher's whole approach: against ph there are a mean
        # 2.34 enemy builders within four tiles and 51% of turns have two or
        # more, drawn from 26 distinct builders -- a turret placed there is
        # dismantled at 2 titanium a hit before it fires. Against herbert19 the
        # mean is 0.94, only 9% of turns have two, and there are four builders
        # in the entire game: nothing there can take a sentinel down quickly, so
        # every turn spent waiting is simply a turn not shooting. Same rule,
        # opposite behaviour, which is why gating on the crowd keeps both.
        if crowd >= config.STALL_MIN_DEFENDERS:
            st.stalled += 1
            if (spot.x, spot.y) != (me.x, me.y):
                _step_toward(ct, st, [spot], reach)
                return
            if _circle_step(ct, st, core, foes):
                return

    # Standing on the spot: spend every turn building, never moving.
    #
    # The launcher goes up FIRST, before any sentinel, once the spot is worth
    # several of them. That ordering is the reference bot's -- in 5 of 15 games
    # it builds one 1-7 turns AHEAD of the first sentinel and never after one --
    # and it is the ordering that pays, because of how a turn interleaves: the
    # launcher has a HIGHER entity id than the builder that made it, so it acts
    # LATER in the same round. The builder spends its turn building a sentinel;
    # the launcher then picks it up and drops it beside the next site. The
    # builder never spends a turn moving.
    #
    # That is exactly why a launcher HERE is free and the same launcher used as a
    # cross-map relay is not: a relay hop needs the builder to stand still to be
    # collected, which costs the very turn it was meant to save. Measured over
    # the 22-game subset, relay hops allowed: 0 -> 63.6%, 1 -> 40.9%,
    # 2 -> 18.2%, 3 -> 18.2%.
    #
    # It does the other half of the job for free: an enemy builder that walks up
    # to take the battery down gets picked up and thrown away, and that is the
    # ONLY way anything removes a builder in this game -- fire() hits buildings,
    # never bots.
    # The launcher goes up once we have COMMITTED to this hub -- that is, after
    # the first sentinel -- and not before.
    #
    # The reference bot builds its launcher first, but it has already spent
    # thirteen turns settling on a spot; we have not. Building it first put the
    # launcher where the builder happened to be STANDING rather than where it
    # was about to work: traced on jotunheim, launcher on (19,17) at t26 and
    # then the builder walked off and built its whole cluster around
    # (20,19)-(20,20), two tiles out of pickup range. The taxi fired zero times
    # in eight games, so every previous measurement of "the launcher" was really
    # a measurement of 24 titanium spent on an ornament.
    #
    # After the first sentinel the hub is settled, and the launcher lands in it.
    if (config.USE_LAUNCHER and not st.launcher_built
            and 1 <= st.placed <= config.SENTINEL_TARGET - 2
            and _try_build_launcher(ct, st, sites)):
        return
    if _try_build_sentinel(ct, st, sites):
        return

    # Screen a gunner even mid-build. Against Pivot, 364 of the 382 points that
    # killed our battery came from GUNNERS -- and a gunner's shot is stopped by
    # any obstacle, so 3 Ti of barrier on its ray turns off a 20 Ti turret
    # outright. Their counter-turret goes up 0-2 turns after our first sentinel
    # (t29 ours / t31 theirs on one map, t15 / t15 on another), so waiting for
    # the battery to be finished before screening is waiting until it is dead.
    if _screen_gunner(ct, st):
        return

    # Vacate and backfill. Having spent this tile's neighbours, the tile we are
    # STANDING on is very often a legal site itself -- and the only thing in the
    # way is us. Step off it and the next turn builds it from next door. The
    # reference bot does exactly this: in 440f game 1 it built (14,6), (15,7) and
    # (14,8) from (14,7) on three consecutive turns, stepped to (13,7), and then
    # built (14,7) -- the tile it had just left. It is a fourth sentinel two
    # turns earlier than walking to a fresh spot.
    if _vacate_step(ct, st, core, reach):
        return

    # Nothing to build and nowhere useful to go: keep what we have alive. A
    # turret we let die costs a full rebuild at a scaled price.
    _heal_neighbour(ct, st)


def _tour(st, start, by_tile, reach, taken):
    """How many sentinels can be placed from around `start`, and in how many turns.

    A stand tile is scored today by the sites on its four cardinal neighbours,
    which treats a two-site tile as a fine place to begin. It is not: we build
    two, then have to relocate, and relocation is where the battery falls apart.
    Measured, we put up 4.25 sentinels a game and never have more than 2.57 alive
    at once, against the reference bot's 3.90 -- four turrets' worth of titanium
    delivering 1.7 turrets' worth of damage.

    So plan a short TOUR instead: from `start`, repeatedly step to the adjacent
    stand tile that exposes the most sites we have not counted yet. Worst case
    that is build, move, build, move, build, move, build -- seven turns for four
    sentinels -- and that is still far better than what unplanned relocation
    produces.

    Returns (sites reachable, turns to place them). Turns = one per build plus
    one per step.
    """
    cur = start
    got = set()
    moves = 0
    for _ in range(config.TOUR_MAX_MOVES + 1):
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            k = (cur[0] + dx, cur[1] + dy)
            if k in by_tile and k not in taken:
                got.add(k)
        if len(got) >= config.SENTINEL_TARGET:
            break
        # Step to whichever neighbour opens the most new sites.
        best = None
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            n = (cur[0] + dx, cur[1] + dy)
            if n not in reach or n in by_tile:
                continue
            gain = sum(1 for ex, ey in ((0, -1), (1, 0), (0, 1), (-1, 0))
                       if (n[0] + ex, n[1] + ey) in by_tile
                       and (n[0] + ex, n[1] + ey) not in got
                       and (n[0] + ex, n[1] + ey) not in taken)
            if gain and (best is None or gain > best[0]):
                best = (gain, n)
        if best is None:
            break
        cur = best[1]
        moves += 1
    return len(got), len(got) + moves


def _pick_stand(ct, st, core, reach):
    """Choose the tile to STAND on, not the site to build.

    This is the single thing that separates a working rush from a losing one.
    Ranking sites individually and walking to the best of them spreads the
    battery out over the turns it takes to walk between them: measured over the
    eleven-map subset, the first sentinel went down on turn 28 and the last on
    turn 84, and the defender picked them off one at a time -- ten of twenty-two
    games ended with every sentinel built also destroyed.

    A builder builds on a CARDINAL NEIGHBOUR of the tile it stands on, and
    building costs its whole turn. So a tile with three valid sites around it is
    worth three sentinels in three consecutive turns without a single step, and
    that is exactly what the reference bot does: in game 1 it stood on (4,0) for
    turns 34-36 and put up a launcher on (4,1), a sentinel on (5,0) and a
    sentinel on (3,0) -- south, east and west of one tile.

    Returns (score, stand tile, [(site, facing), ...]) or None.
    """
    me = ct.get_position()
    here = (me.x, me.y)
    ctiles = st.board.core_tiles(core)
    threat = _threat_tiles(ct, st)
    thrown = st.grip
    # Index the sites that hit the core by tile, keeping one facing per tile.
    by_tile = {}
    for p, d in geom.sentinel_sites(st.board, core):
        key = (p.x, p.y)
        if key in st.dead_sites or key in by_tile:
            continue
        # Once two sentinels have died young, treat the whole neighbourhood of
        # every grave as covered and go to another face of the core. Refusing
        # only the exact tile is not enough: the killer is a fixed-facing turret
        # whose line covers a row, so the tile next door is no safer, and
        # rebuilding nearby just donates another turret. Traced on two ladder
        # games -- five sentinels, six or seven turns apart, peak alive ONE.
        if (st.fast_deaths >= config.FAST_DEATHS_BEFORE_MOVE and st.dead_sites
                and min(abs(key[0] - dx) + abs(key[1] - dy)
                        for dx, dy in st.dead_sites) <= config.LETHAL_AREA):
            continue
        # Hard gap cap. Measured over 80 of our own sentinels on the live pool,
        # a site more than SITE_MAX_GAP from the core footprint is killed 75% of
        # the time and killed WITHIN FIFTEEN TURNS every one of those times,
        # against 19% at gap 0-2 and 24% at gap 3. It is not a preference to be
        # traded against site count -- past this it is simply not a site.
        if min(abs(p.x - t.x) + abs(p.y - t.y)
               for t in ctiles) > config.SITE_MAX_GAP:
            continue
        if not _tile_free(ct, st, p):
            continue
        by_tile[key] = (p, d)
    if not by_tile:
        return None

    # Walking round to the far face is a SAFETY measure, and safety is only
    # worth its tempo when there is somebody to be safe from.
    #
    # Measured both ways on the pool: against herbert19, which defends, the far
    # face is worth +7 points (70.0% at 14.0 against 63.3% at 0.0); against a
    # rush mirror, which never contests the spot at all, it COSTS 31 (36.7% at
    # 14.0 against 68.3% at 0.0). The detour is real either way -- traced on
    # helheim, the rusher's first two steps took it FURTHER from the target core
    # (distance 10 -> 11 -> 12) so it could come round the outside -- and in five
    # unrated games against not adgato not one sentinel on either side was ever
    # destroyed. Paying for protection nobody is threatening loses the race.
    far_bonus = (config.FAR_FACE_BONUS if st.saw_defender
                 else config.FAR_FACE_BONUS_CLEAR)
    best = None
    for key, (dist, _first) in reach.items():
        if dist > config.STAND_MAX_WALK:
            continue
        sites = []
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            hit = by_tile.get((key[0] + dx, key[1] + dy))
            if hit is not None:
                sites.append(hit)
        if not sites:
            continue
        # Order the sites this spot can reach by how good each one is, so the
        # turns are spent on the best of them first -- the spot may well be
        # taken from us before we have built all of them.
        sites.sort(key=lambda pd: _site_cost(st, pd[0], pd[1], ctiles, threat))
        d_core = min(abs(key[0] - t.x) + abs(key[1] - t.y) for t in ctiles)
        # Sentinels per visit dominates; then the distance band; then the walk.
        # A spot is worth what a short TOUR from it can place, not what stands
        # next to it -- see _tour. Ties broken toward fewer total turns.
        reach_n, turns = _tour(st, key, by_tile, reach, st.built_sites)
        safe = sum(1 for q, _e in sites
                   if threat.get((q.x, q.y), 0) < config.THREAT_ON_LINE)
        reach_n = min(reach_n, max(safe, 1)) if safe else reach_n
        # Can this spot finish the battery, and if not, can we leave it to
        # finish elsewhere? Every site we build on is a neighbour we can never
        # walk through again -- see DEAD_END_PENALTY.
        need = max(0, config.SENTINEL_TARGET - st.placed)
        if len(sites) < need and len(sites) <= config.DEAD_END_MAX_SITES:
            site_tiles = {(q.x, q.y) for q, _e in sites}
            ways_out = 0
            for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                nxt = (key[0] + dx, key[1] + dy)
                if nxt in site_tiles or nxt in reach:
                    ways_out += nxt not in site_tiles
            if not ways_out:
                dead_end = config.DEAD_END_PENALTY
            else:
                dead_end = 0.0
        else:
            dead_end = 0.0
        score = (config.STAND_PER_SITE * min(reach_n, config.SENTINEL_TARGET)
                 - dead_end
                 - config.TOUR_TURN_COST * turns
                 - config.SITE_CORE_WEIGHT * abs(d_core - config.SITE_IDEAL_GAP)
                 - config.SITE_WALK_WEIGHT * dist
                 - config.EDGE_PENALTY * _edge_penalty(st.board, key)
                 + far_bonus * _far_face(st, core, key))
        # Never stand where a launcher can pick us up.
        #
        # ph keeps one beside its core, and on glacierkeep it threw our rusher
        # back three tiles TWENTY TIMES: we walked in at t28, were thrown at
        # t31, walked in again, thrown at t35, again, thrown at t39, and built
        # exactly zero sentinels in 149 turns. A launcher picks up a builder on
        # any of its eight neighbours, from either team, and it costs them no
        # ammunition to do it. Standing next to one is not a risk, it is a loop.
        if key in thrown:
            score -= config.LAUNCHER_GRIP_PENALTY
        if key == here:
            # Prefer where we already are, all else equal: a step is a turn, and
            # flapping between two equal spots builds nothing at all.
            score += config.STAND_STICKINESS
        if best is None or score > best[0]:
            best = (score, Position(key[0], key[1]), sites)
    return best


def _chip_adjacent_turret(ct, st):
    """Attack an enemy turret we already stand beside. 2 Ti for 2 damage.

    Only from where we stand: walking to one costs the turn that would otherwise
    heal ours, and the sums only work because this turn was going to be spent on
    nothing. A gunner is 25 HP -- 26 Ti of attacks removes it permanently, where
    26 Ti of healing buys back 104 HP once.
    """
    if ct.get_action_cooldown() != 0:
        return False
    if ct.get_global_resources() < (GameConstants.BUILDER_BOT_ATTACK_COST
                                    + config.TITANIUM_FLOOR):
        return False
    me = ct.get_position()
    my_team = ct.get_team()
    best = None
    for q, _d in _cardinal_neighbours(me):
        try:
            bid = ct.get_tile_building_id(q)
            if bid is None or ct.get_team(bid) == my_team:
                continue
            if ct.get_entity_type(bid) not in (EntityType.GUNNER,
                                               EntityType.SENTINEL,
                                               EntityType.LAUNCHER):
                continue
            hp = ct.get_hp(bid)
        except Exception:
            continue
        if best is None or hp < best[0]:
            best = (hp, q)
    if best is None:
        return False
    try:
        if ct.can_fire(best[1]):
            ct.fire(best[1])
            return True
    except Exception:
        pass
    return False


def _screen_gunner(ct, st):
    """Barrier the ray of a gunner that is shooting one of our sentinels.

    Only worth doing from where we stand -- the barrier has to go on a tile
    cardinally adjacent to us, and stepping to reach one costs the turn we would
    otherwise spend healing.
    """
    if ct.get_action_cooldown() != 0:
        return False
    cost = ct.get_barrier_cost()
    if ct.get_global_resources() < cost + config.TITANIUM_FLOOR:
        return False
    me = ct.get_position()
    my_team = ct.get_team()
    mine = st.built_sites
    if not mine:
        return False
    adjacent = {(q.x, q.y) for q, _d in _cardinal_neighbours(me)}
    try:
        buildings = ct.get_nearby_buildings()
    except Exception:
        return False
    for bid in buildings:
        try:
            if ct.get_team(bid) == my_team:
                continue
            if ct.get_entity_type(bid) != EntityType.GUNNER:
                continue
            q = ct.get_position(bid)
            facing = ct.get_direction(bid)
        except Exception:
            continue
        dx, dy = facing.delta()
        reach = config.GUNNER_REACH if (dx == 0 or dy == 0) else config.GUNNER_REACH - 1
        ray = [(q.x + dx * k, q.y + dy * k) for k in range(1, reach + 1)]
        if not any(t in mine for t in ray):
            continue
        # Screen as close to the gunner as we can: a barrier further down the ray
        # protects fewer of our tiles.
        for t in ray:
            if t in mine or t not in adjacent:
                continue
            pos = Position(t[0], t[1])
            try:
                if not ct.can_build_barrier(pos):
                    continue
                ct.build_barrier(pos)
            except Exception:
                continue
            return True
    return False


def _threat_tiles(ct, st):
    """Tiles an enemy turret can already shoot, computed once per turn.

    This is the only thing that kills a sentinel. Across 15 reference games the
    defender dealt 962 HP to the rush: 800 from gunners and 162 from sentinels,
    and builder attacks dealt EXACTLY ZERO. Every single sentinel death is a
    defender turret whose fixed firing line runs through the sentinel's tile. So
    the threat model is not "enemy builders are near", it is "this tile is on a
    turret's line" -- and unlike a builder, a turret cannot be dodged after the
    fact, because its facing is chosen when it is built and only gunners can
    rotate at all.

    A gunner's current ray is the immediate danger; the rest of its reach is
    counted too, at a lower weight, because 10 titanium turns it to face us.
    """
    out = {}
    my_team = ct.get_team()
    try:
        buildings = ct.get_nearby_buildings()
    except Exception:
        return out
    for bid in buildings:
        try:
            if ct.get_team(bid) == my_team:
                continue
            et = ct.get_entity_type(bid)
            if et not in (EntityType.GUNNER, EntityType.SENTINEL):
                continue
            q = ct.get_position(bid)
            facing = ct.get_direction(bid)
        except Exception:
            continue
        cardinal = (geom.SENTINEL_CARDINAL_REACH if et == EntityType.SENTINEL
                    else config.GUNNER_REACH)
        dx, dy = facing.delta()
        reach = cardinal if (dx == 0 or dy == 0) else cardinal - 1
        for k in range(1, reach + 1):
            t = (q.x + dx * k, q.y + dy * k)
            out[t] = max(out.get(t, 0), config.THREAT_ON_LINE)
        if et == EntityType.GUNNER:
            # It can pay 10 Ti to turn. Every tile it could ever cover is worth
            # avoiding, just not as much as the one it is aimed at now.
            for d2 in Direction:
                if d2 == Direction.CENTRE:
                    continue
                ex, ey = d2.delta()
                r2 = cardinal if (ex == 0 or ey == 0) else cardinal - 1
                for k in range(1, r2 + 1):
                    t = (q.x + ex * k, q.y + ey * k)
                    out.setdefault(t, config.THREAT_ROTATABLE)
    return out


def _vacate_step(ct, st, core, reach):
    """If our own tile is a sentinel site, step off it so we can build it.

    Prefers a neighbour that is itself worth standing on, so the step is not
    wasted: after the backfill we want to be somewhere with more sites.
    """
    me = ct.get_position()
    here = (me.x, me.y)
    if not any((p.x, p.y) == here
               for p, _d in geom.sentinel_sites(st.board, core)):
        return False
    if ct.get_global_resources() < ct.get_sentinel_cost() + config.TITANIUM_FLOOR:
        return False
    by_tile = {(p.x, p.y) for p, _d in geom.sentinel_sites(st.board, core)
               if _tile_free(ct, st, p)}
    best = None
    for q, _d in _cardinal_neighbours(me):
        if not st.board.passable_guess(q.x, q.y):
            continue
        if (q.x, q.y) == st.prev_pos:
            # Never step back onto the tile we were on two turns ago. The
            # reference bot loses 6-20 turns a game to exactly this two-cycle,
            # flapping between two tiles while an enemy builder sits beside it.
            continue
        nearby = sum(1 for r, _e in _cardinal_neighbours(q)
                     if (r.x, r.y) in by_tile)
        if best is None or nearby > best[0]:
            best = (nearby, q)
    if best is None:
        return False
    _step_toward(ct, st, [best[1]], reach)
    return True


def _sites_around(ct, st, core, me):
    """Every legal sentinel site cardinally adjacent to `me`, best first.

    The unconditional fallback: no reachability, no face preference, no distance
    band -- just what can be built from this tile right now.
    """
    ctiles = st.board.core_tiles(core)
    threat = _threat_tiles(ct, st)
    out = []
    for p, d in geom.sentinel_sites(st.board, core):
        if abs(p.x - me.x) + abs(p.y - me.y) != 1:
            continue
        if not _tile_free(ct, st, p):
            continue
        out.append((_site_cost(st, p, d, ctiles, threat), p, d))
    out.sort(key=lambda t: t[0])
    return [(p, d) for _c, p, d in out]


def _far_face(st, core, key):
    """+1 on the face of the enemy core directly away from our own core, -1 on
    the near face, 0 on the flanks.

    Cosine of the angle between (this tile - their core) and (our core - their
    core), negated. The reference bot walks to the NEAR face -- arrival angle is
    20 degrees off the line between the cores in all 15 games, at gap exactly 5 --
    and then spends a mean 13.5 turns swinging roughly 70 degrees round to a
    flank before it builds anything. Its first sentinel goes down at a median 96
    degrees. Pathing straight to the far face instead was measured at only +7.2
    BFS steps, which is half the price of the swing, so this aims where it ends
    up rather than where it starts.
    """
    mine = st.board.my_core
    if mine is None:
        return 0.0
    ax, ay = key[0] - core.x, key[1] - core.y
    bx, by = mine.x - core.x, mine.y - core.y
    na = (ax * ax + ay * ay) ** 0.5
    nb = (bx * bx + by * by) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return -((ax * bx + ay * by) / (na * nb))


def _site_cost(st, p, d, ctiles, threat=None):
    """Rank sites available from one stand tile. Lower is better."""
    d_core = min(abs(p.x - t.x) + abs(p.y - t.y) for t in ctiles)
    cost = config.SITE_CORE_WEIGHT * abs(d_core - config.SITE_IDEAL_GAP)
    if threat:
        cost += threat.get((p.x, p.y), 0)
    if (p.x, p.y) in st.dead_rays:
        # A tile whose sentinel was already shot off it, or one on the same ray.
        # The reference bot rebuilds straight back into these: in 440f72b2 g2 it
        # built (5,0) on t35, t44 and t51 and one gunner at (6,0) facing west
        # killed all three -- ~225 titanium of scaled sentinels donated to a 20
        # titanium turret. Diverging here deliberately.
        cost += config.DEAD_RAY_PENALTY
    # Never put two sentinels on the same firing line -- each one would stand in
    # the next one's shot. On antler that produced four sentinels in a row that
    # lived seventy turns and left the enemy core on 500 of 500 HP.
    if _ray_crosses(p, d, st.built_sites, config.SENTINEL_CARDINAL_REACH):
        cost += config.SAME_LINE_PENALTY
    return cost


def _edge_penalty(board, key):
    """Dislike the outermost ring, and ONLY the outermost ring.

    Measured on both bots and they agree exactly. Percentage of sentinels
    destroyed, by distance from the map edge:

                    on the edge   one tile in   two or more
        adgato          47.6%          0.0%        26.9%
        rushdown        40.6%         20.0%        21.4%

    One tile in is the best place on the board for both of us, and the edge
    itself is the worst. So EDGE_COMFORT is 1, not 2 -- penalising edge-1 as
    well threw away the sweet spot along with the trap.
    """
    e = min(key[0], key[1], board.w - 1 - key[0], board.h - 1 - key[1])
    return max(0, config.EDGE_COMFORT - e)


def _ray_crosses(p, d, occupied, reach):
    """Does the shot from p facing d pass over a tile we have already built on?

    A sentinel ignores terrain but not entities in the sense that matters here:
    two of ours on the same line means the back one is shooting the front one's
    tile instead of the core. Keeping the battery on distinct lines is the whole
    reason it does 36 damage a round rather than 18.
    """
    if not occupied:
        return False
    dx, dy = d.delta()
    for k in range(1, reach + 1):
        if (p.x + dx * k, p.y + dy * k) in occupied:
            return True
    return False


def _tile_free(ct, st, p):
    """Buildable as far as we can tell, using sight when we have it.

    NOTE `is_tile_empty` means "no building and not a wall" -- a BOT is neither,
    so a tile with an enemy builder standing on it passes here and then fails
    `can_build_sentinel`. That is deliberate and was measured: excluding occupied
    tiles costs 6.7 points on the pool (63.3% against 70.0%), because a defender
    stepping across a site for one turn should not make the rusher abandon a spot
    it has walked twenty turns to reach. Bots move; buildings do not.

    The case it does cost us is two blockers holding both remaining sites at once
    -- traced on holmgang against Pivot, the rusher stood at (5,1) from turn 14
    to the end of the game with builders parked on (4,1) and (5,2). That wants
    eviction, not a different filter.
    """
    if not st.board.in_bounds(p.x, p.y) or st.board.is_wall(p.x, p.y):
        return False
    try:
        if ct.is_in_vision(p):
            return ct.is_tile_empty(p)
    except Exception:
        return False
    return True


def _exits_after(ct, st, site):
    """How many ways out we would still have after building on `site`.

    Our own sentinels are solid buildings, so every one placed on a cardinal
    neighbour is one fewer way out of the tile we are standing on -- the rusher
    walls itself in with its own battery. Measured over 305 ladder games, of the
    32 that stopped short of four sentinels, 21 had three or four exits blocked
    and 6 were completely entombed; of those that stopped at exactly three, 10 of
    14 were blocked by their OWN turret.

    Being entombed is fine once the battery is finished -- the terminal state is
    to stand still and heal -- but before that it costs the rest of the battery.
    """
    me = ct.get_position()
    n = 0
    for q, _d in _cardinal_neighbours(me):
        if (q.x, q.y) == (site.x, site.y):
            continue
        if not st.board.passable_guess(q.x, q.y):
            continue
        try:
            if ct.is_in_vision(q) and ct.get_tile_builder_bot_id(q) is not None:
                continue
        except Exception:
            pass
        n += 1
    return n


def _try_build_sentinel(ct, st, sites):
    if ct.get_action_cooldown() != 0:
        return False
    # Leave the floor behind so a heal is still affordable the turn after.
    if ct.get_global_resources() < ct.get_sentinel_cost() + config.TITANIUM_FLOOR:
        return False
    me = ct.get_position()
    last = st.placed + 1 >= config.SENTINEL_TARGET
    adjacent = [(p, d) for p, d in sites
                if abs(p.x - me.x) + abs(p.y - me.y) == 1]
    # Prefer any site that does not brick us in. Our own sentinels are solid, so
    # each one built on a cardinal neighbour is one fewer way out -- of the 32
    # ladder games that stopped short of four sentinels, 21 had three or four
    # exits blocked and 6 were entombed outright, mostly by their own turrets.
    #
    # But refusing outright is worse than the disease: if the entombing site is
    # the only one available, skipping it builds nothing at all, which measured
    # 66.7% against 71.1%. So this only reorders -- safe sites first, and the
    # blocking one still gets built if it is all there is.
    if not last and len(adjacent) > 1:
        adjacent.sort(key=lambda pd: 0 if _exits_after(ct, st, pd[0]) else 1)
    for p, d in adjacent:
        try:
            if not ct.can_build_sentinel(p, d):
                continue
            ct.build_sentinel(p, d)
        except Exception:
            continue
        st.ever_built += 1
        st.built_sites.add((p.x, p.y))
        st.built_at[(p.x, p.y)] = ct.get_current_round()
        st.placed = len(st.built_sites)
        return True
    return False


def _note_dead_sentinels(ct, st):
    """Blacklist a site whose sentinel has died. Do NOT count it as unbuilt.

    `st.placed` counts sentinels EVER built, not sentinels alive, and that
    distinction is the difference between a rush and a bankruptcy. Decrementing
    it on a death turns the rusher into a rebuild loop, and a sentinel costs
    +20% more every time: seven of them is 388 titanium instead of 138. Measured
    on drumlin, the bank hit 13 titanium by turn 40 and stayed there, so the
    sentinels that were standing had no ammunition and the enemy core finished
    the game on 498 of 500 HP. The reference bot builds four or five and never
    replaces one.

    Only judged for tiles we can currently see; a site we have walked away from
    is not evidence of anything.
    """
    for key in tuple(st.built_sites):
        p = Position(key[0], key[1])
        try:
            if not ct.is_in_vision(p):
                continue
            if ct.get_tile_building_id(p) is None:
                st.built_sites.discard(key)
                st.dead_sites.add(key)
                if (ct.get_current_round() - st.built_at.get(key, 0)
                        <= config.FAST_DEATH_TURNS):
                    st.fast_deaths += 1
                for dx, dy in ((0, -1), (1, -1), (1, 0), (1, 1),
                               (0, 1), (-1, 1), (-1, 0), (-1, -1)):
                    for k in range(0, config.DEAD_RAY_SPAN + 1):
                        st.dead_rays.add((key[0] + dx * k, key[1] + dy * k))
        except Exception:
            continue
    st.placed = len(st.built_sites)


def _defenders_near(ct, st):
    """Is anyone actually here to punish a slow battery?

    Waiting is only worth anything when the spot is contested. With no defender
    in sight there is nothing to be patient FOR: every turn spent looking for a
    better hub is a turn the battery is not firing, and the assembly span only
    matters because a defender gets to shoot the turrets as they go up one at a
    time. So the gate above is conditional on this.
    """
    me = ct.get_position()
    my_team = ct.get_team()
    try:
        bots = ct.get_nearby_units(config.DEFENDER_WATCH_SQ)
    except Exception:
        return True
    for bid in bots:
        try:
            if ct.get_team(bid) == my_team:
                continue
            if ct.get_entity_type(bid) == EntityType.BUILDER_BOT:
                return True
        except Exception:
            continue
    return False


def _orbit(centre):
    """The eight tiles a launcher at `centre` can pick a builder up from."""
    return [Position(centre.x + dx, centre.y + dy)
            for dx in (-1, 0, 1) for dy in (-1, 0, 1)
            if dx or dy]


def _orbit_sites(st, centre, sites):
    """Sites buildable from somewhere in the launcher's orbit.

    A builder standing on tile T builds on T's four CARDINAL neighbours, and T
    must be one of the launcher's eight, so the serviceable set is
    N4(N8(launcher)). This is what makes the launcher worth its 20 titanium: the
    builder never leaves pickup range, so it can be thrown again the turn after,
    and the turn after that.
    """
    reachable = set()
    for t in _orbit(centre):
        if not st.board.in_bounds(t.x, t.y):
            continue
        for q, _d in _cardinal_neighbours(t):
            reachable.add((q.x, q.y))
    return [(q, d) for q, d in sites if (q.x, q.y) in reachable]


def _try_build_launcher(ct, st, sites):
    """Put the launcher beside us, on a tile no sentinel wants.

    Two constraints that pull apart. It must not take a site -- 18 damage a
    round beats a taxi -- but it must stay inside the cluster, because both of
    its jobs are local: pickup reach is its eight neighbours, so it can only
    throw a builder standing next to it, and it can only evict a defender that
    has walked right up to the battery.

    So: our cardinal neighbours, excluding every tile the battery wants, ranked
    by how many of those sites the launcher would still be able to serve.
    """
    if ct.get_action_cooldown() != 0:
        return False
    if ct.get_global_resources() < (ct.get_launcher_cost()
                                    + ct.get_sentinel_cost()
                                    + config.TITANIUM_FLOOR):
        return False
    # Which tiles the battery still needs. Note the stand tile was CHOSEN for
    # having as many sites around it as possible, so on a good spot all four
    # neighbours are sites and there is no free tile left -- that is why an
    # earlier version of this never built a launcher at all, on any map, at any
    # threshold. When that happens the launcher takes the WORST site: the fourth
    # sentinel is 18 damage a round, and a taxi that lands the other three a turn
    # each earlier plus evicts defenders is worth more than one of them.
    need = max(0, config.SENTINEL_TARGET - st.placed)
    # Reserve the best sites, but ALWAYS leave the worst one available -- with
    # three sites and four sentinels wanted, reserving `need - 1` reserved all
    # three and the launcher had nowhere to go.
    wanted = {(p.x, p.y) for p, _d in sites[:max(0, min(len(sites), need) - 1)]}
    me = ct.get_position()
    best = None
    for p, _d in _cardinal_neighbours(me):
        if (p.x, p.y) in wanted or not _tile_free(ct, st, p):
            continue
        try:
            if not ct.can_build_launcher(p):
                continue
        except Exception:
            continue
        # Score it by what its ORBIT can build. The builder is thrown to one of
        # the launcher's own eight neighbours and builds on that tile's four
        # cardinal neighbours, so the reachable set is N4(N8(launcher)) -- and a
        # launcher placed at the hub of the site cluster can service all of it
        # without the builder ever leaving pickup range.
        serves = len(_orbit_sites(st, p, sites))
        if best is None or serves > best[0]:
            best = (serves, p)
    if best is None:
        return False
    try:
        ct.build_launcher(best[1])
    except Exception:
        return False
    st.launcher_built = True
    st.launcher_pos = best[1]
    return True


def _heal_neighbour(ct, st):
    """Heal the most damaged friendly building beside us. True if we acted."""
    if ct.get_action_cooldown() != 0 or ct.get_global_resources() < 1:
        return False
    me = ct.get_position()
    my_team = ct.get_team()
    best = None
    for p, _d in _cardinal_neighbours(me):
        if not st.board.in_bounds(p.x, p.y):
            continue
        try:
            bid = ct.get_tile_building_id(p)
            if bid is None or ct.get_team(bid) != my_team:
                continue
            missing = ct.get_max_hp(bid) - ct.get_hp(bid)
            if missing <= 0 or not ct.can_heal(p):
                continue
        except Exception:
            continue
        if best is None or missing > best[0]:
            best = (missing, p)
    if best is not None:
        try:
            ct.heal(best[1])
            return True
        except Exception:
            pass
    return False


# =============================================================================
# movement
# =============================================================================
def _step_toward(ct, st, targets, reach=None):
    """One cardinal step along the shortest path to the nearest target tile.

    Takes the flood rather than re-running a BFS per call, and picks the nearest
    REACHABLE target: handing it a target it cannot get to has to mean "stay
    put and try again", not "stand here forever".
    """
    if ct.get_move_cooldown() != 0 or ct.get_action_cooldown() != 0:
        return
    me = ct.get_position()
    if reach is None:
        reach = geom.flood(st.board, me)
    best = None
    goal = None
    for t in targets:
        hit = reach.get((t.x, t.y))
        if hit is None or hit[1] is None:
            continue
        if best is None or hit[0] < best[0]:
            best = hit
            goal = t
    if best is None:
        return
    # Two-cycle guard. A body-block presents as motion: the blocker mirrors us,
    # we sidestep, it steps across, we step back, and nothing ever advances --
    # traced on helheim, the rusher shuttled (6,7)<->(6,8) from turn 12 to the
    # end of the game with a defender shadowing it one tile away. Refusing the
    # tile we occupied two turns ago breaks the cycle and costs at most one
    # slightly longer route.
    d = best[1]
    dx, dy = d.delta()
    if st.prev_pos == (me.x + dx, me.y + dy):
        alt = None
        for t in targets:
            for q, e in _cardinal_neighbours(me):
                if (q.x, q.y) == st.prev_pos:
                    continue
                hit = reach.get((q.x, q.y))
                if hit is None:
                    continue
                far = reach.get((t.x, t.y))
                if far is None:
                    continue
                cand = abs(q.x - t.x) + abs(q.y - t.y)
                if alt is None or cand < alt[0]:
                    alt = (cand, e)
        if alt is not None:
            d = alt[1]
    # Take the best step we can ACTUALLY take, not just the best one.
    #
    # The flood plans through enemy builders, because they are units and not
    # buildings and nothing in the remembered terrain knows they are there. So
    # the ideal first step is routinely a tile with an enemy body standing on
    # it, `can_move` says no -- and this used to give up for the turn. Every
    # turn. Traced on helheim: our rusher stopped at (8,6) on turn 7 with the
    # enemy rusher one tile away at (9,6) and stood there until turn 1000 on a
    # map with NO WALLS AT ALL, which it could have walked around in two steps.
    # Three of the fifteen pool maps ended that way, against 17.7% of ladder
    # games that build no sentinel at all.
    #
    # So rank the other three directions by how much closer to the goal they
    # leave us and take the first legal one. Sidestepping a blocker costs one
    # turn; standing in front of it costs the game.
    # Stand firm first, then step around.
    #
    # Yielding a corridor hands the race to whoever stays put -- against
    # herbert19, which body-blocks deliberately, sidestepping immediately cost
    # 10 points (68.3% -> 58.3%). But never yielding is how the rusher stood at
    # (8,6) for 993 turns. So refuse for SIDESTEP_AFTER turns, which wins the
    # staring contest against a blocker that is merely passing, and only then
    # walk around the one that is not going to move.
    try:
        if not ct.can_move(d):
            st.blocked += 1
        else:
            st.blocked = 0
    except Exception:
        pass
    # Refuse to WALK INTO a launcher's reach, not merely to stand there.
    #
    # Penalising grip tiles as DESTINATIONS was not enough and made it worse --
    # 20 launches became 32 -- because the rusher never chose to stand there. It
    # was walking through on the way in and being thrown back before it ever
    # arrived, so the penalty never got a say. The route is what has to avoid it.
    grip = getattr(st, "grip", None)
    if grip and (me.x, me.y) not in grip:
        safe = []
        for q, e in _cardinal_neighbours(me):
            if (q.x, q.y) in grip:
                continue
            try:
                if ct.can_move(e):
                    safe.append(e)
            except Exception:
                pass
        if safe and d not in safe:
            if goal is not None:
                safe.sort(key=lambda e: abs(me.x + e.delta()[0] - goal.x)
                          + abs(me.y + e.delta()[1] - goal.y))
            d = safe[0]

    order = [d]
    if (goal is not None and config.SIDESTEP_BLOCKED
            and st.blocked >= config.SIDESTEP_AFTER):
        alts = []
        for q, e in _cardinal_neighbours(me):
            if e == d or (q.x, q.y) == st.prev_pos:
                continue
            if not st.board.passable_guess(q.x, q.y):
                continue
            alts.append((abs(q.x - goal.x) + abs(q.y - goal.y), e))
        alts.sort(key=lambda t: t[0])
        order += [e for _c, e in alts]
    for e in order:
        try:
            if ct.can_move(e):
                ct.move(e)
                return
        except Exception:
            pass


def _stand_tiles(p):
    return [Position(p.x, p.y - 1), Position(p.x + 1, p.y),
            Position(p.x, p.y + 1), Position(p.x - 1, p.y)]


def _ring(board, core):
    """The twelve tiles around a 2x2 core -- where a builder can actually be."""
    out = []
    for x in range(core.x - 1, core.x + 3):
        for y in range(core.y - 1, core.y + 3):
            if core.x <= x <= core.x + 1 and core.y <= y <= core.y + 1:
                continue
            if board.in_bounds(x, y):
                out.append(Position(x, y))
    return out


def _wander_toward_centre(ct, st):
    """Used only while the symmetry is unsettled.

    Every surviving candidate core is on the far side of the map, and the middle
    is on the way to all of them, so heading for the centre never wastes a step
    and it is exactly where a tile and its mirror image are both in vision --
    which is what settles the symmetry.
    """
    centre = Position(st.board.w // 2, st.board.h // 2)
    _step_toward(ct, st, [centre])


# =============================================================================
# economy builder (large maps only)
# =============================================================================
def eco_turn(ct, st):
    """Harvest one nearby ore tile, route it home, then heal. Never fight.

    Deliberately has no defensive behaviour at all. A builder that walks off to
    body-block or to chip an attacker is a builder not delivering titanium, and
    titanium is ammunition: on the maps where this role exists at all, the rush
    is short of ammunition, not short of defenders.
    """
    core = st.board.my_core
    if core is None:
        _heal_neighbour(ct, st)
        return

    if not st.harvested:
        if st.eco_target is None:
            st.eco_target = _pick_ore(ct, st, core)
        if st.eco_target is not None:
            if _try_build_harvester(ct, st):
                st.harvested = True
                return
            _step_toward(ct, st, [st.eco_target])
            return

    if _try_extend_route(ct, st, core):
        return
    _heal_core_or_neighbour(ct, st, core)


def _pick_ore(ct, st, core):
    """Nearest ore within ECON_MAX_ROUTE of the core, or None."""
    best = None
    for p in ct.get_nearby_tiles():
        if st.board.env.get((p.x, p.y)) != Environment.ORE_TITANIUM:
            continue
        d = abs(p.x - core.x) + abs(p.y - core.y)
        if d > config.ECON_MAX_ROUTE:
            continue
        try:
            if ct.is_in_vision(p) and ct.get_tile_building_id(p) is not None:
                continue
        except Exception:
            pass
        if best is None or d < best[0]:
            best = (d, p)
    return None if best is None else best[1]


def _try_build_harvester(ct, st):
    if ct.get_action_cooldown() != 0:
        return False
    if ct.get_global_resources() < ct.get_harvester_cost():
        return False
    me = ct.get_position()
    p = st.eco_target
    if abs(p.x - me.x) + abs(p.y - me.y) != 1:
        return False
    try:
        if not ct.can_build_harvester(p):
            return False
        ct.build_harvester(p)
    except Exception:
        return False
    return True


def _try_extend_route(ct, st, core):
    """Lay one conveyor on the tile beside us that is nearest the core.

    Delivery into a core is a directed cardinal push, so each conveyor is built
    facing the next step toward the core and the chain works from either end as
    it is completed.
    """
    if ct.get_action_cooldown() != 0:
        return False
    if ct.get_global_resources() < ct.get_conveyor_cost():
        return False
    me = ct.get_position()
    here = abs(me.x - core.x) + abs(me.y - core.y)
    if here <= 1:
        return False
    best = None
    for p, _d in _cardinal_neighbours(me):
        if not _tile_free(ct, st, p):
            continue
        d = abs(p.x - core.x) + abs(p.y - core.y)
        if d >= here:
            continue
        facing = p.cardinal_direction_to(core)
        if facing == Direction.CENTRE:
            continue
        if best is None or d < best[0]:
            best = (d, p, facing)
    if best is None:
        return False
    _d, p, facing = best
    try:
        if not ct.can_build_conveyor(p, facing):
            return False
        ct.build_conveyor(p, facing)
    except Exception:
        return False
    return True


def _heal_core_or_neighbour(ct, st, core):
    """Top the core up if we are beside it, else anything else that is hurt,
    else walk back to the core and wait there."""
    me = ct.get_position()
    for p, _d in _cardinal_neighbours(me):
        for tile in ((core.x, core.y), (core.x + 1, core.y),
                     (core.x, core.y + 1), (core.x + 1, core.y + 1)):
            if (p.x, p.y) != tile:
                continue
            try:
                bid = ct.get_tile_building_id(p)
                if (bid is not None and ct.get_hp(bid) < ct.get_max_hp(bid)
                        and ct.can_heal(p) and ct.get_global_resources() >= 1):
                    ct.heal(p)
                    return
            except Exception:
                pass
    _heal_neighbour(ct, st)
    if ct.get_move_cooldown() == 0 and ct.get_action_cooldown() == 0:
        _step_toward(ct, st, [Position(core.x, core.y)])


# =============================================================================
# turrets
# =============================================================================
_TARGET_RANK = {EntityType.CORE: 0, EntityType.SENTINEL: 1, EntityType.GUNNER: 1,
                EntityType.LAUNCHER: 2, EntityType.HARVESTER: 3}


def sentinel_turn(ct, st):
    """Fire down our fixed line at the best enemy on it. The core, if it is
    there, is the only target that matters.

    Ranking is done by LOOKING at each tile, not by comparing it against a
    remembered enemy core position. The first version of this took the core
    position off the comms store and fired at anything if the store was empty --
    and the store is regularly empty, because publishing it waits on the
    symmetry being settled. The result was sentinels planted five tiles from the
    enemy core happily shooting the nearest conveyor for their whole lives, with
    the core finishing the game on 500 of 500 HP.

    A sentinel cannot rotate, so the facing chosen at build time is the whole
    aiming decision; this is only about which tile on the line to spend the shot
    on. Its vision radius equals its attack radius, so everything on the line
    can be identified.
    """
    if ct.get_action_cooldown() != 0:
        return
    if ct.get_global_ammo() < GameConstants.SENTINEL_AMMO_COST:
        return
    try:
        tiles = ct.get_attackable_tiles()
    except Exception:
        return
    my_team = ct.get_team()
    # Only where a builder is a REPAIRER. Against an economy their builders are
    # everywhere, and preferring them turns the whole battery away from the core
    # to chase 40 HP targets that keep walking: measured against herbert19 it
    # cost 11.7 points (70.0% -> 58.3%). In a confirmed rush mirror there is
    # exactly one enemy builder and its job is to undo our damage at 4 HP per
    # titanium, so three shots to remove it are the best shots we have.
    hunting = _rush_mirror(ct, st)
    ranked = []
    for t in tiles:
        rank = 9
        try:
            bid = ct.get_tile_building_id(t)
            if bid is not None and ct.get_team(bid) != my_team:
                rank = _TARGET_RANK.get(ct.get_entity_type(bid), 4)
        except Exception:
            pass
        # A builder bot on the line outranks the core.
        #
        # It is 40 HP, so three shots kill it permanently, and the builder
        # standing next to an enemy core is there to REPAIR it: healing is 4 HP
        # per titanium against the 1.8 a shot of ours buys, so a live enemy
        # builder undoes more of our damage than we can out-spend. Killing it is
        # worth far more than the 54 damage those three shots would have put
        # into a core that is being repaired faster than that.
        if config.SENTINEL_HUNTS_BUILDERS and hunting:
            try:
                uid = ct.get_tile_builder_bot_id(t)
                if uid is not None and ct.get_team(uid) != my_team:
                    rank = config.TARGET_RANK_BUILDER
            except Exception:
                pass
        ranked.append((rank, t))
    ranked.sort(key=lambda rt: rt[0])
    for _rank, t in ranked:
        try:
            if ct.can_fire(t):
                ct.fire(t)
                return
        except Exception:
            continue


def launcher_turn(ct, st):
    """Taxi our own rusher to its next sentinel site.

    The launcher has a higher entity id than the builder that made it, so it acts
    after that builder every round: the builder builds, then this throws it. The
    builder's own action is never spent on moving, which is the entire point of
    paying 20 titanium for this.
    """
    if ct.get_action_cooldown() != 0:
        return
    core = _read_enemy_core(ct, st)
    if core is None:
        return
    me = ct.get_position()
    my_team = ct.get_team()

    bot = None
    # Pickup reach is the EIGHT neighbours -- can_launch takes any bot at
    # Chebyshev 1 -- so scanning only the four cardinals silently loses every
    # diagonal pickup, which is half of them.
    for p in _orbit(me):
        if not st.board.in_bounds(p.x, p.y):
            continue
        try:
            bid = ct.get_tile_builder_bot_id(p)
        except Exception:
            continue
        if bid is None:
            continue
        if ct.get_team(bid) == my_team:
            bot = p
            break
        # An enemy builder beside us is here to take the sentinels down. Throwing
        # it away costs nothing and buys the whole ring a few rounds.
        far = _throw_away_target(ct, st, p)
        if far is not None:
            try:
                if ct.can_launch(p, far):
                    ct.launch(p, far)
                    return
            except Exception:
                pass
    if bot is None:
        return

    # TAXI, and the whole point is that it repeats. Throw the builder only to a
    # tile in THIS launcher's own eight-neighbourhood, so that next turn it is
    # still in pickup range and can be thrown again:
    #
    #     build sentinel -> launch -> build sentinel -> launch -> ...
    #
    # one sentinel per turn with no walking, for as long as sites remain. The
    # obvious alternative -- throw the builder to whichever tile in the full
    # sqrt(26) range sits next to the best remaining site -- ends the relay after
    # one hop, because the builder lands outside pickup range and the launcher
    # becomes a 20-titanium ornament.
    #
    # Keeping the pair together also buys the battery a bouncer: an enemy builder
    # that walks in to plant a counter-gunner is standing next to the launcher by
    # definition, and gets thrown out (handled above, before this).
    sites = [(q, d) for q, d in geom.sentinel_sites(st.board, core)
             if _tile_free(ct, st, q)]
    if not sites:
        return
    site_tiles = {(q.x, q.y) for q, _d in sites}
    best = None
    for t in _orbit(me):
        if (t.x, t.y) == (bot.x, bot.y):
            continue
        if (t.x, t.y) in site_tiles:
            continue                       # do not stand on a tile we want
        if not st.board.passable_guess(t.x, t.y):
            continue
        adj = sum(1 for q, _d in sites
                  if abs(q.x - t.x) + abs(q.y - t.y) == 1)
        if not adj:
            continue
        try:
            if not ct.can_launch(bot, t):
                continue
        except Exception:
            continue
        if best is None or adj > best[0]:
            best = (adj, t)
    if best is not None:
        try:
            ct.launch(bot, best[1])
        except Exception:
            pass


def _throw_away_target(ct, st, bot_pos):
    """The reachable tile furthest from us -- where an enemy builder can do the
    least damage soonest."""
    best = None
    for t in ct.get_nearby_tiles(GameConstants.LAUNCHER_VISION_RADIUS_SQ):
        d = (t.x - bot_pos.x) ** 2 + (t.y - bot_pos.y) ** 2
        if best is None or d > best[0]:
            try:
                if ct.can_launch(bot_pos, t):
                    best = (d, t)
            except Exception:
                continue
    return None if best is None else best[1]


def healer_turn(ct, st):
    """Stand on our own core and repair it, forever.

    +4 HP for 1 titanium, against the 1.8 HP a titanium buys as a sentinel shot.
    This unit never defends, never builds and never attacks: an enemy sentinel
    ignores obstacles and out-ranges everything a builder can do about it, so
    trying to kill the battery costs 2 Ti for 2 damage against 40 HP, while the
    same titanium repairs 8. Repairing is the counter.
    """
    core = st.board.my_core
    if core is None:
        return
    tiles = st.board.core_tiles(core)
    me = ct.get_position()

    # Repair from where we stand if we can reach any core tile.
    if ct.get_action_cooldown() == 0 and ct.get_global_resources() > 0:
        for t in tiles:
            if abs(t.x - me.x) + abs(t.y - me.y) != 1:
                continue
            try:
                if ct.can_heal(t):
                    ct.heal(t)
                    return
            except Exception:
                pass

    # Otherwise close on it. The core's own tiles are solid, so aim at the ring.
    reach = geom.flood(st.board, me)
    _step_toward(ct, st, _ring(st.board, core), reach)


def _enemy_base_empty(ct, st, core):
    """Is there anything at all defending their core?

    The barrier ring takes about twenty turns, so it may only be attempted
    against an opponent who has committed everything forward -- no conveyors, no
    harvesters, no turrets, and no builder anywhere in sight. Their rusher is by
    definition halfway across the map at our core, so it is not in our vision
    and does not count.
    """
    mine = ct.get_team()
    try:
        for bid in ct.get_nearby_buildings():
            if ct.get_team(bid) == mine:
                continue
            if ct.get_entity_type(bid) != EntityType.CORE:
                return False
        for uid in ct.get_nearby_units():
            if ct.get_team(uid) != mine:
                return False
    except Exception:
        return False
    return True


def _ring_walk(st, start, core):
    """Which ring corners we can reach WITHOUT stepping on a face tile.

    The eight face tiles are the ones we are about to fill with barriers, so a
    route that depends on walking over them stops existing halfway through the
    job. This is the user's "can we walk in a ring around the core" test, and it
    is the difference between walling the core in and walling ourselves out.
    """
    faces = {(q.x, q.y) for q in geom.core_face_tiles(core)}
    cores = {(q.x, q.y) for q in st.board.core_tiles(core)}
    blocked = faces | cores
    seen = {(start.x, start.y)}
    queue = [(start.x, start.y)]
    while queue:
        x, y = queue.pop(0)
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            nxt = (x + dx, y + dy)
            if nxt in seen or nxt in blocked:
                continue
            if not st.board.passable_guess(nxt[0], nxt[1]):
                continue
            seen.add(nxt)
            queue.append(nxt)
    return seen


def _try_barrier_ring(ct, st, core, reach):
    """Seal the eight tiles a builder could repair their core from.

    A barrier is 3 titanium and +1% on the cost scale -- the cheapest thing in
    the game -- and eight of them permanently switch off the enemy's only
    efficient defence. Healing is 4 HP per titanium against the 1.8 a sentinel
    shot buys, so an opponent with a builder on its core out-repairs our whole
    battery on equal income: traced on an 18x18 game, their core went to 80 HP
    at t40 and was back to 498 by t120 while we sat dry. Eight barriers make
    that impossible, because heal needs orthogonal adjacency and every tile with
    it is now a wall.

    Returns True if the turn was spent.
    """
    me = ct.get_position()
    todo = []
    for q in geom.core_face_tiles(core):
        if not st.board.in_bounds(q.x, q.y) or st.board.is_wall(q.x, q.y):
            continue
        if (q.x, q.y) in st.ring_built:
            continue
        try:
            bid = ct.get_tile_building_id(q)
            if bid is not None:
                if ct.get_team(bid) == ct.get_team():
                    # One of ours already occupies it -- a sentinel on a face
                    # tile denies healing exactly as well as a barrier does.
                    st.ring_built.add((q.x, q.y))
                    continue
                # An ENEMY structure in the ring kills the whole plan, and
                # silently skipping it is worse than not starting: the ring only
                # works if it is COMPLETE. A conveyor is walkable, so a builder
                # standing on one heals the core straight through our wall, and
                # every barrier we placed was paid for to accomplish nothing.
                st.ring_done = True
                return False
        except Exception:
            pass
        todo.append(q)
    if not todo:
        st.ring_done = True
        return False

    # Can we actually get round it? Checked once, against the corners we still
    # need, and abandoned for good if not -- per the spec, fall back to plain
    # sentinel logic rather than half-walling it.
    walkable = _ring_walk(st, me, core)
    need = []
    for q in todo:
        stands = [t for t in _stand_tiles(q)
                  if (t.x, t.y) in walkable and st.board.passable_guess(t.x, t.y)]
        if not stands:
            st.ring_done = True
            return False
        need.extend(stands)

    if ct.get_action_cooldown() == 0:
        cost = ct.get_barrier_cost()
        if ct.get_global_resources() >= cost + config.TITANIUM_FLOOR:
            for q in todo:
                if abs(q.x - me.x) + abs(q.y - me.y) != 1:
                    continue
                try:
                    if ct.can_build_barrier(q):
                        ct.build_barrier(q)
                        st.ring_built.add((q.x, q.y))
                        return True
                except Exception:
                    pass

    # Not beside anything left to wall: walk to a tile that is.
    targets = [t for t in need if (t.x, t.y) != (me.x, me.y)]
    if targets:
        _step_toward(ct, st, targets, reach)
        return True
    return False


def _note_lone_raider(ct, st):
    """Tell the rest of the team that a single enemy builder is crossing.

    Our rusher walks the whole map, so it is the unit that sees their rusher go
    past -- usually around the halfway line, heading for our core, alone. The
    store is the only channel between units, and a note left here means the core
    can tell a rush from an economic opponent whose builders never leave home.

    Writes are buffered for a round, which does not matter: the raider still has
    the rest of its walk to make.
    """
    if st.raider_noted:
        return
    mine = ct.get_team()
    home = st.board.my_core
    try:
        for uid in ct.get_nearby_units():
            if ct.get_team(uid) == mine:
                continue
            if ct.get_entity_type(uid) != EntityType.BUILDER_BOT:
                continue
            q = ct.get_position(uid)
            # Out in the open, away from their own core: a builder that has left
            # its base is a raider, not an economy.
            if home is not None:
                far = abs(q.x - home.x) + abs(q.y - home.y)
                if far < (st.board.w + st.board.h) // 4:
                    continue
            ct.write_store(config.SLOT_RAIDER_SEEN, 1)
            st.raider_noted = True
            return
    except Exception:
        pass


def _opposition(ct, st, gap):
    """Is this an opponent who will contest the spot?

    Not the same question as "can I see an enemy builder". In a rush matchup we
    ALWAYS see one -- theirs, walking the other way, on its way to our core --
    and mistaking it for a defender is what makes us walk the long way round for
    nothing. Two signals that do discriminate:

    * any enemy building that is not the core. A base with conveyors and
      harvesters in it belongs to an economy, and an economy has builders at
      home who will come and shoot at our battery.
    * an enemy builder seen once we are ALREADY at their core. By then their
      rusher is halfway across the map at ours, so a builder still standing here
      is one that stayed to defend.
    """
    mine = ct.get_team()
    try:
        for bid in ct.get_nearby_buildings():
            if ct.get_team(bid) == mine:
                continue
            if ct.get_entity_type(bid) != EntityType.CORE:
                return True
    except Exception:
        pass
    return gap <= config.ARRIVED_GAP and _defenders_near(ct, st)


def _rush_mirror(ct, st):
    """Are we certain this is a rush against a rush?

    Both halves must hold, and they are recorded by different units at different
    times: our rusher saw a single enemy builder crossing the open map
    (SLOT_RAIDER_SEEN) and then found their base undefended when it arrived
    (SLOT_BASE_EMPTY). Together they mean the opponent has spent everything on
    one bot pointed at our core -- which is the only situation where walling
    their core and repairing ours is worth the turns, because there is nobody
    left to punish either.
    """
    try:
        return bool(ct.read_store(config.SLOT_RAIDER_SEEN)
                    and ct.read_store(config.SLOT_BASE_EMPTY))
    except Exception:
        return False


def panic(ct, st=None):
    """Move. Anywhere sensible. Used when the turn threw, and by the watchdog.

    This exists because of a real 0-5: against ph our rusher stopped on (5,13)
    at turn 3 and stood there until turn 151, twenty-two tiles from the core it
    was sent to attack, on two 30x30 maps out of five. It could not be
    reproduced against any of six local opponents on any of the three 30x30
    maps, so rather than guess at the cause, this makes the SYMPTOM impossible:
    whatever went wrong, a builder that has done nothing for STUCK_LIMIT turns
    takes the step that most reduces its distance to the enemy core.

    Deliberately dumb -- no flood, no scoring, no memory -- because it has to
    work in the situation where the clever code did not.
    """
    try:
        if ct.get_entity_type() != EntityType.BUILDER_BOT:
            return False
        if ct.get_move_cooldown() != 0 or ct.get_action_cooldown() != 0:
            return False
        me = ct.get_position()
        goal = None
        if st is not None:
            try:
                goal = st.board.enemy_core()
            except Exception:
                goal = None
        best = None
        for q, d in _cardinal_neighbours(me):
            try:
                if not ct.can_move(d):
                    continue
            except Exception:
                continue
            if goal is None:
                return _do_move(ct, d)
            cost = abs(q.x - goal.x) + abs(q.y - goal.y)
            if best is None or cost < best[0]:
                best = (cost, d)
        if best is not None:
            return _do_move(ct, best[1])
    except Exception:
        pass
    return False


def _do_move(ct, d):
    try:
        ct.move(d)
        return True
    except Exception:
        return False


def _enemy_builder_positions(ct):
    """Where every enemy builder bot we can see is standing."""
    mine = ct.get_team()
    out = []
    try:
        for uid in ct.get_nearby_units():
            if ct.get_team(uid) == mine:
                continue
            if ct.get_entity_type(uid) != EntityType.BUILDER_BOT:
                continue
            q = ct.get_position(uid)
            out.append((q.x, q.y))
    except Exception:
        pass
    return out


def _defender_gap(pos, foes):
    """Chebyshev distance to the nearest enemy builder, or 99 if none in sight."""
    if not foes:
        return 99
    return min(max(abs(pos[0] - f[0]), abs(pos[1] - f[1])) for f in foes)


def _circle_step(ct, st, core, foes):
    """Orbit the enemy core, away from defenders, without drifting off it.

    The reference bot's approach is not a straight line and not a loiter -- it
    walks a full circuit around the core at gap 3-8, and only stops when the
    builders drift off. Copying that: take the step that most increases our
    distance from the nearest defender, subject to staying in reach of the core,
    so waiting is spent going somewhere rather than standing still.
    """
    me = ct.get_position()
    ctiles = st.board.core_tiles(core)
    best = None
    for q, d in _cardinal_neighbours(me):
        if not st.board.passable_guess(q.x, q.y):
            continue
        try:
            if not ct.can_move(d):
                continue
        except Exception:
            continue
        gap = min(abs(q.x - t.x) + abs(q.y - t.y) for t in ctiles)
        if gap > config.ARRIVED_GAP + 2:
            continue
        # Distance from the hunters first; closeness to the ideal firing gap
        # only as a tie-break, so we come back in tight once they have gone.
        score = _defender_gap((q.x, q.y), foes) - 0.25 * abs(gap - config.SITE_IDEAL_GAP)
        if best is None or score > best[0]:
            best = (score, d)
    if best is None:
        return False
    try:
        ct.move(best[1])
        return True
    except Exception:
        return False


def _launcher_grip(ct):
    """Every tile an enemy launcher could pick us up from: its eight neighbours."""
    grip = set()
    mine = ct.get_team()
    try:
        for bid in ct.get_nearby_buildings():
            if ct.get_team(bid) == mine:
                continue
            if ct.get_entity_type(bid) != EntityType.LAUNCHER:
                continue
            q = ct.get_position(bid)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    grip.add((q.x + dx, q.y + dy))
    except Exception:
        pass
    return grip


def _note_launchers(ct, st):
    """Remember every enemy launcher ever seen, and the tiles it reaches.

    Buildings do not move, so this only ever grows -- and it has to be
    remembered rather than re-observed, because a builder's vision is about four
    tiles and the whole point is to stay further away than that.
    """
    mine = ct.get_team()
    try:
        for bid in ct.get_nearby_buildings():
            if ct.get_team(bid) == mine:
                continue
            if ct.get_entity_type(bid) != EntityType.LAUNCHER:
                continue
            q = ct.get_position(bid)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    st.known_grip.add((q.x + dx, q.y + dy))
    except Exception:
        pass
