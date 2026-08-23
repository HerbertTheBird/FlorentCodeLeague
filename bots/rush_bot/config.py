"""Every tunable in one place, with the arithmetic that sets it.

The whole strategy is a race between one number and another: how much ammunition
four sentinels burn, against how much titanium we ever have. Nothing here is a
free parameter -- each one moves that balance, so each one is written down with
the sum behind it.

# The ammunition budget, which is the real constraint

A sentinel is SENTINEL_DAMAGE=18 on a 2-round cooldown for SENTINEL_AMMO_COST=10
a shot, so four of them are 36 damage and 20 ammunition per round. A 500 HP core
takes 500/36 = 14 rounds to break, costing about 280 ammunition, and ammunition
comes only from the core converting titanium 1:1.

Against a start of 500 titanium:

    builder                       30
    launcher                      20
    four sentinels    30+33+36+39 = 138     (+20% scale each)
    ---------------------------------
                                 188, leaving 312

plus PASSIVE_TITANIUM_AMOUNT=10 every PASSIVE_TITANIUM_INTERVAL=4 rounds, i.e.
2.5 a round: by round 60 that is another 150. So roughly 460 titanium against a
280 ammunition bill -- comfortable IF nothing is spent on economy, and tight the
moment anything is. That is why the reference bot builds no conveyor and no
harvester on any map size, and it is the reason ECON_MIN_AREA exists rather than
economy being on everywhere.
"""

# --- map size switch --------------------------------------------------------
# Below this area the rush is unmodified: one builder, no economy, nothing else.
# Every observed game of the reference bot is a pure rush, including on 30x30, so
# treat economy as the experimental arm and the pure rush as the control.
#
# The case for economy on a big map is the walk: on 20x20 the rusher arrives
# around round 34 and the core dies around 59, but on 30x30 it arrives around 52
# and dies around 70 -- ten extra rounds of sentinels firing, 200 more
# ammunition, against only 25 more titanium of passive income.
ECON_MIN_AREA = 484              # 22x22; above this, consider economy

# ...but OFF by default, because it competes for the one resource the rush
# cannot spare. Traced on drumlin (25x25, so the economy arm was live): two extra
# builders at scaled cost (36 + 43), a harvester (20) and a conveyor every turn
# the route could take one drove the bank from 430 to 13 titanium by turn 40. The
# sentinels were up, correctly aimed -- (18,22) facing NORTH with the core tile
# (18,18) on its line -- and could not shoot, because ammunition is bought with
# the same titanium. Global ammo sat at 12 for the rest of the game, one shot
# short of a volley, and the enemy core finished on 498 of 500.
#
# The arithmetic says this is structural, not a tuning problem. A shot is 10
# titanium and a kill is 28 shots; passive income is 2.5 a round. The whole
# budget is the 500 we start with, and a harvester that finishes on turn 20 will
# not have returned its own cost before the game is decided on turn 60.
ECON_ENABLED = False
#
# MEASURED, and the answer is emphatic. 86 matches per arm against herbert19,
# all 43 maps, both sides, against a 62.8% baseline with economy off:
#
#     economy on maps > ECON_MIN_AREA      39.5%   (-23.3)
#     economy on every map                 19.8%   (-43.0)
#
# The arithmetic that motivated trying it is real -- against a defender healing
# its core at ~11 HP a turn, four sentinels at 36 HP a turn need ~400 ammunition
# and the starting bank only funds ~290, so the rush is structurally 110
# titanium short -- but an economy is the wrong way to find that titanium.
# Builder bots share the +20% cost-scale pool with sentinels, so each extra
# builder adds 6 Ti to the price of every sentinel still to come: two builders is
# 79 Ti of spawns plus 48 Ti of scale before a single conveyor is laid, against a
# harvester returning 2.5 Ti a round into a window that is decided in fifteen.

# At most this many extra builders, ever. Each one costs 30 titanium AT +20%
# SCALE ON THE RUSHER'S PRICE -- the rusher is already built so its cost is sunk,
# but a third builder pushes the scale to 1.44 and there is nothing left to spend
# it on. Two is what the ammunition budget above will carry.
ECON_MAX_BUILDERS = 2

# --- medics and late economy: MEASURED AND REMOVED -----------------------------
# Both were attempts to spend titanium on a second builder, and both lost badly:
#
#     medic spawned at t1, parked on the core healing      27.9%   (-34.9)
#     the full 1337 opening (3 sentinels + 290 ammo + medic) 22.1%   (-40.7)
#
# against a 62.8% one-builder baseline, 86 matches each. The mechanism is the
# same one that sinks the economy arm above: builder bots share the +20%
# cost-scale pool with sentinels, so a second builder is not 36 titanium, it is
# 36 plus 6 on every sentinel still to come. Team 1337 runs exactly this opening
# and it is the only team that beats the reference rush -- but transplanted here
# it is -40, and why it works for them and not for us is the largest open
# question left in this bot.

# Economy builders are only worth it if their harvester is running early
# enough to pay for itself. A harvester delivers a stack every 4 rounds, so
# one finished on round 10 returns about 120 titanium by round 60 and one
# finished on round 30 returns 75 -- against 30 for the builder and ~15 for
# the route. After this the sums stop working and the titanium is worth more
# as ammunition.
ECON_LAST_ROUND = 6

# An economy builder is only worth spawning for ore the core can already see and
# that is close enough to pay back inside the game's ~60 rounds. The core's
# vision is CORE_VISION_RADIUS_SQ=36, so 6 tiles, and a harvester delivers a
# stack every 4 rounds once its route is finished: at this range the route is
# about 5 conveyors, 15 titanium, finished by round 12, returning 10 titanium
# every 4 rounds for the remaining fifty. Anything further and the conveyors cost
# more than the ore returns before the game ends.
ECON_MAX_ROUTE = 6

# --- the rush ---------------------------------------------------------------
# How many sentinels to plant. Four is 36 damage a round; the fifth is 39
# titanium and 10 more ammunition a round for a 25% faster kill, which the
# reference bot takes only when a launcher has saved it the movement turns.
SENTINEL_TARGET = 4

# Hard ceiling on sentinels ever built, rebuilds included. The reference bot
# averages 4.2 a game and rebuilt only 6 times across 15 games, and every
# rebuild is dearer than the last because the +20% scale is shared with
# builder bots: the ladder runs 36, 42, 48, 54, 60, 66. Two spare turrets is
# 126 Ti, which is already 13 shots of ammunition given up.
MAX_EVER_BUILT = 5

# Build a launcher on arrival, before the sentinels.
#
# It looks like a waste of 20 titanium and it is not. The engine runs units in
# ascending entity id order and the launcher is built AFTER the builder, so it
# acts after the builder every round: the builder spends its turn building a
# sentinel, and then the launcher picks it up and throws it to the next site for
# free. Without one, every sentinel that is not cardinally adjacent to the last
# costs a whole turn of walking. In the observed games the three with a launcher
# placed five sentinels and the two without placed four.
# OFF. Three separate forms of launcher were built and measured against
# herbert19, 86 matches each, all 43 maps, both sides, against a 62.8% baseline:
#
#     destination launcher (taxi between sites + evict defenders)   53.5%  (-9.3)
#     cross-map transport relay, 1 hop                              40.9%
#     cross-map transport relay, 2 hops                             18.2%
#     cross-map transport relay, 3 hops                             18.2%
#
# The relay was always doomed and the arithmetic says why: a hop needs the
# builder to stand still to be collected, so it costs the very turn it saves.
# The DESTINATION launcher is the interesting one, because it is genuinely free
# on turn cost -- it has a higher entity id than the builder that made it, so it
# acts later in the same round and can throw a builder that has already spent
# its action building. It still loses, for a duller reason: 20-24 titanium is
# two and a half sentinel shots out of a bank that is already ~110 short of what
# a healing defender demands, and it has to stand on a tile the battery wanted.
# Against herbert19 neither of its two jobs ever actually fired -- no taxi throw
# and no eviction in the games traced -- so it was pure cost.
#
# Worth revisiting only against an opponent that chases the rusher with builders,
# which is the case the eviction half is for and which our local suite does not
# contain. The code is all still here and works; this is a one-line switch.
# OFF -- and the orbit design is right, it is just redundant.
#
# The intended cycle is build sentinel -> launch to another tile in the
# launcher's own eight-neighbourhood -> build -> launch, so the builder never
# leaves pickup range and never spends a turn walking. It works. It is also
# unnecessary, because `_pick_stand` already chooses a hub whose four CARDINAL
# neighbours are all sentinel sites, so the builder gets the same one-per-turn
# cadence standing still, for nothing.
#
# Instrumented over 8 pool games: 7 launchers built, 0 taxi throws, 0 evictions.
# 24 titanium each -- two and a half sentinel shots -- for literally no action.
# Pool measurement: 53.3% with it, 70.0% without.
USE_LAUNCHER = False

# Sites the spot must offer before the launcher is worth 20 titanium. With fewer
# than this there is nothing to taxi between and the launcher is just a turret we
# did not build.
LAUNCHER_MIN_SITES = 2

# Distance^2 to the enemy core at which the rusher tells the core it is in
# position, so the core can stop holding the launcher's price back. Purely a
# comms signal -- what the rusher DOES is driven by which sites are reachable,
# not by this.
ARRIVE_DIST_SQ = 40
# Manhattan gap at or inside which the rusher counts as having arrived, for the
# stuck-at-the-core fallback. A site is at most 5 from the core footprint and a
# stand tile is adjacent to one, so 6 is exact.
ARRIVED_GAP = 6



# --- stand-tile selection ---------------------------------------------------
# A builder builds on a cardinal NEIGHBOUR of where it stands, and building costs
# its whole turn -- so what matters is not which site is best but which TILE has
# the most sites around it. Weight per site available from a spot. It dominates
# everything else deliberately: a spot worth three sentinels in three consecutive
# turns beats a spot one tile better placed every time, because the defender gets
# to kill them one at a time otherwise. Measured on the eleven-map subset before
# this existed, the battery took from turn 28 to turn 84 to assemble.
STAND_PER_SITE = 10.0
# How far the tour planner will walk while collecting sites. Three steps is the
# worst case the arithmetic tolerates: build/move/build/move/build/move/build is
# seven turns for four sentinels.
# How far the tour planner walks while collecting sites. ZERO -- i.e. the
# planner is disabled and a spot is scored only by the sites already beside it.
#
# The idea was sound and it is worth writing down why it fails. Scoring a stand
# tile by what a short walk from it could place, rather than by what stands next
# to it, does improve the metric it targets: peak concurrent sentinels 2.85 ->
# 3.00, sentinels built 3.50 -> 3.65. It still loses, at every setting:
#
#     TOUR_MAX_MOVES   0 -> 71.7%   1 -> 66.7%   2 -> 66.7%   3 -> 66.7%
#     TOUR_TURN_COST   0 -> 65.0%   0.75 -> 66.7%   1.5 -> 66.7%   3.0 -> 55.0%
#
# A plan degrades faster than it pays. Sites get taken, blocked or built on
# between the turn the tour is chosen and the turn it would be walked, so
# committing to a spot on the promise of a multi-step tour is worse than taking
# the best tile available now and re-deciding every turn.
TOUR_MAX_MOVES = 0
# Cost per turn of the planned tour, so a spot that can place four in four turns
# beats one that needs seven.
TOUR_TURN_COST = 0.75
# Bonus for the tile we are already on. A step is a turn, and two spots of equal
# score with nothing to separate them make the rusher flap between them forever.
STAND_STICKINESS = 12.0
# Don't consider spots further than this. Purely a CPU bound on the scan over the
# reachability flood, which on a 30x30 map is up to 900 tiles.
STAND_MAX_WALK = 60
# Preference for the far face of the enemy core -- see roles._far_face.
# Worth about one and a half sites, so it will not override a spot that can
# put up two more sentinels, but it decides between otherwise equal spots.
FAR_FACE_BONUS = 14.0
# Turns of neither building nor moving before the rusher gives up on the
# spot it wants and builds whatever this tile can reach.
COMMIT_AFTER_STALL = 3
# Turns without getting any closer to the core than our best so far, before we
# stop trying to advance and build from where we are. Separate from the tile-
# based counter above because a body-block presents as motion, not stillness.
# Turns without getting closer to the core before we stop advancing and build
# from where we are. Effectively OFF (999): measured over 60 matches on the pool
# at 8 -> 61.7%, 12 -> 58.3%, 16 -> 61.7% against 63.3% with it disabled. The
# body-block it was meant to answer is handled by the two-cycle guard in
# roles._step_toward instead, which breaks the loop rather than surrendering to
# it.
COMMIT_AFTER_NO_PROGRESS = 999

# --- threat ------------------------------------------------------------------
# A gunner's ray: vision/attack r^2=13, so 3 tiles cardinally and 2 diagonally.
GUNNER_REACH = 3
# Cost added to a site already covered by an enemy turret's CURRENT facing. Large
# enough to lose to any site that is not, because a sentinel on a turret's line
# is dead in six turns and there is no answer to it -- our sentinels are locked
# onto the core and cannot shoot back.
THREAT_ON_LINE = 40.0
# Cost for a tile a gunner could cover by paying 10 Ti to rotate. Real but
# cheaper than being aimed at right now.
THREAT_ROTATABLE = 8.0
# Cost for a tile on a ray out of a site where one of ours has already died.
DEAD_RAY_PENALTY = 25.0
DEAD_RAY_SPAN = 3
# A sentinel destroyed this fast was placed somewhere already covered.
FAST_DEATH_TURNS = 8
# After this many of those, refuse every site within LETHAL_AREA of a grave --
# move to another face of the core rather than refeed this one.
FAST_DEATHS_BEFORE_MOVE = 2
# Radius around a tile where a sentinel died young inside which we refuse to
# build again. ZERO -- off. The pathology is real (two ladder games with five
# sentinels built six turns apart and peak alive ONE) but excluding a
# neighbourhood excludes the good sites with the bad, because they are all
# clustered against the core:
#     LETHAL_AREA  0 -> 71.7%   2 -> 68.3%   4 -> 68.3%   7 -> 65.0%
LETHAL_AREA = 0

# Dislike tiles within this many steps of the map edge -- see roles._edge_penalty
# for the measurement (48% of edge sentinels destroyed against 12% one step in).
EDGE_COMFORT = 2
EDGE_PENALTY = 1.5
# Ray distance from the core to aim for -- see SITE_IDEAL_GAP below. Measured
# over all 64 of the reference bot's sentinels, the number of steps from the
# sentinel to the core tile its line lands on is k=1 x15, k=2 x24, k=3 x13,
# k=4 x10, k=5 x2: median 2, mean 2.2. It plants CLOSE and almost never shoots at
# max range, where one tile of terrain error takes the core off the line.
# Cost added to a site that would shoot through a sentinel we already placed.
# ZERO, and that is measured, not an oversight: 20 of the reference bot's 64
# sentinels sit on a line an earlier one already occupies, 5 of 15 games put all
# four on just two lines, and three games stack three in a straight row shooting
# through each other. Damage per LIVING sentinel is a flat 9 a round either way
# (1 alive -> 9.96 measured, 2 -> 19.96, 3 -> 28.99, 4 -> 36.14), so a sentinel
# does not block another's shot and spreading the battery out buys nothing while
# costing the walk between sites.
SAME_LINE_PENALTY = 0.0
SENTINEL_CARDINAL_REACH = 5
# Sites a spot must offer before we place the FIRST sentinel. See
# roles.rusher_turn: assembly span is the single discriminator between the
# reference bot's wins and its losses.
# Sites a spot must offer before the FIRST sentinel goes down, when an enemy
# builder is in vision. Effectively OFF (1), and the reason is worth keeping.
#
# The idea was that assembly span is what keeps a battery alive: split not
# adgato's own games and it wins with a 4.3-turn span and loses with 20.2, at
# identical arrival and loiter. So make the rusher wait for a hub that can build
# the whole battery, but only when someone is there to punish a slow one.
#
# It is a no-op. Ladder A/B on two fixed map sets, 45 games, against 1337, Pivot,
# Lorem Ipsum, Bean counters and not adgato: EVERY match ended in the identical
# score, 12/45 either way. `_pick_stand` already picks hubs with three or more
# sites, so the gate never binds -- which the local sweep had already said, with
# 1, 2 and 3 all returning exactly 42W-18L.
#
# And the span it was meant to fix was already fixed by something else. Measured
# on ladder replays: v121 25.8 turns, v122 (two-cycle guard) 7.7, v125 (this
# gate) 11.1. The two-cycle guard did all of it.
#
# Which in turn undercuts the hypothesis: v122 sits at 7.7, near adgato's
# winning 4.3, and still wins 12 of 45. So the 4.3-vs-20.2 split in adgato's
# games is most likely REVERSE causality -- when it is winning the defender is
# already dead and the battery goes up unopposed; when it is losing the spot is
# contested and every sentinel is a fight. Span is an effect, not a cause.
MIN_SITES_TO_OPEN = 1
# ...but never wait forever: open the battery anyway after this many turns
# without getting closer to the core.
OPEN_ANYWAY_AFTER = 12
# Radius^2 within which an enemy builder counts as contesting the spot. Vision is
# r^2=20 for a builder, so this is everything we can see.
DEFENDER_WATCH_SQ = 20

SITE_IDEAL_GAP = 2
# Hard ceiling on how far a site may be from the core footprint. See
# roles._pick_stand: past this, sentinels die 75% of the time and die fast.
# Hard ceiling on how far a site may be from the core footprint. Effectively OFF
# (99), and that is measured, not an oversight. Our own sentinels at gap 4+ are
# killed 75% of the time and killed within fifteen turns every time, against 19%
# at gap 0-2 -- but capping it costs points, so the correlation is confounded:
# the far placements are chosen when the near ones are blocked or taken, and
# forbidding them just means building nothing there at all.
#     gap cap only   68.3%     edge fix only   66.7%
#     both           65.0%     neither         70.0%
SITE_MAX_GAP = 99
SITE_CORE_WEIGHT = 1.0
SITE_WALK_WEIGHT = 0.5

# --- the core ---------------------------------------------------------------
# Ammunition to hold. Not a buffer -- a target, topped up every turn. 120 is what
# the reference bot converts on round 0 in 15 of 15 games and never exceeds.
# Four sentinels fire 1.6-1.9 shots a turn (the reload-2 cap is 2.0), burning
# 16-19 Ti/turn against 2.5 Ti/turn of passive income, so the whole game is a
# ~16-turn burst off the starting stack and the bank must not be held back from
# it. The previous value here was 40 with a multi-sentinel reserve on top, which
# left sentinels standing and aimed with 12 ammunition for the rest of the game.
AMMO_TARGET = 120
# Titanium never converted, so a rebuild or a heal is always affordable.
TITANIUM_FLOOR = 10

# --- comms slots ------------------------------------------------------------
# Writes are buffered to the next round, so these are always one round stale.
# Nothing here needs to be fresher than that.
SLOT_SENTINELS = 0               # sentinels the rusher has placed so far
SLOT_ARRIVED = 1                 # 1 once the rusher is in position
SLOT_ENEMY_CORE = 2              # packed x + y*width + 1, 0 while unknown
# The rusher's entity id, written by the core on the turn it spawns it. A builder
# reads this on its first run to learn which role it has. The one-round write
# delay is free here: the core writes on round 0 and the builder's first run is
# round 1, so the value is already visible when it looks.
SLOT_RUSHER_ID = 3
# Round+1 on which the rusher last ran, so the core can tell it is dead.
SLOT_HEARTBEAT = 4

# A stand tile that cannot finish the battery AND has no way out is a dead end:
# our own sentinels are solid, so building the last of its sites seals the
# rusher in at whatever it has managed. Over 305 ladder games, of the 32 that
# stopped short of four sentinels, 21 had three or four of the rusher's exits
# blocked and 6 were entombed outright -- and among those that stopped at
# exactly three, 10 of 14 were walled in by their OWN turret.
#
# Penalty, not a filter: a dead end that CAN finish (4 sites) is perfectly fine,
# and a dead end is still better than nowhere. Priced at just over one site so a
# 3-site tile with a way out beats a 3-site tile without one, and a 4-site tile
# beats both.
DEAD_END_PENALTY = 12.0
# ...but only for a dead end that traps us at this few sentinels or fewer.
# Applied to every unfinishable dead end it is a 10-point REGRESSION (71.7 ->
# 61.7 on the pool, identical at 12 and 30, so it flips a consistent set of
# choices the wrong way). The reason is that a tile with no way out has no way
# out BECAUSE it is walled, and walls are what keep a sentinel alive -- the
# penalty trades pockets for open ground and the battery gets shot instead of
# stranded. Three-in-a-pocket beats four-in-the-open.
DEAD_END_MAX_SITES = 2
