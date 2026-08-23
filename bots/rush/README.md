# rushdown — the not adgato v25 sentinel rush

One builder, no economy, four sentinels, dead core by turn 60.

    aggregate over 6 distinct opponents, 43 maps, both sides, 516 matches

    herbert19 (our best)   54-32   62.8%      (seed 2: 54-32, 62.8% — identical)
    Ladder_v36             62-24   72.1%
    Khaos                  47-39   54.7%
    loki                   80- 6   93.0%
    Champion_v54           72-14   83.7%
    Tyr_v1_v78             75-11   87.2%
    ------------------------------------
    AGGREGATE             390-126  75.6%

    reference: not adgato v25 on the ladder, 220-56 games = 79.7%

The first working version of this scored 17% against herbert19 and 34.5% on the
three-bot suite.

## What the strategy is

Read off 15 replays of not adgato v25 (`replays_adgato/`, tools in
`tools/beanalysis/`):

    t 0        spawn ONE builder. Never another. No conveyor, no harvester, on
               any map size including 30x30.
    t 5-40     walk it to the enemy core. A near-optimal shortest path straight
               down the centre line — 90% path efficiency, moves on 98.8% of
               turns, zero evasion, takes zero damage en route in 15/15 games.
    t +0..+5   build FOUR sentinels on tiles whose fixed line of fire crosses
               the enemy core, one per turn, mostly from a single standing tile.
    t +15      core dead. Four sentinels are exactly 36 HP/round (measured:
               9.96 / 19.96 / 28.99 / 36.14 for 1/2/3/4 alive).

The whole game is an ammunition budget. A shot is 10 titanium and a kill is 28
shots; builder plus battery is 210 Ti of the 500 we start with, leaving ~290 —
29 shots, 522 damage against a 500 HP core. It is budgeted to the last shot,
which is why nothing may be spent on anything else.

## The things that actually mattered

In rough order of measured value:

1. **Pathing.** Enemy conveyors are *walkable* — an enemy base is mostly belt,
   and treating it as solid walls the rusher out of the region it is trying to
   reach. Plus one reachability flood per turn instead of a BFS per candidate,
   so it can only ever choose a site it can get to. (13.6% → 22.7%)
2. **Pick the tile to STAND on, not the site to build.** A builder builds on a
   cardinal neighbour and building costs its whole turn, so a tile with three
   sites around it is three sentinels in three turns with no steps. Ranking
   sites individually spread the battery over 15-30 turns and the defender ate
   it one turret at a time.
3. **Never bankrupt yourself.** Rebuilding dead sentinels at +20% scale each
   drove the bank to 13 Ti by turn 40, so the battery stood there aimed and
   silent. Hold 120 ammunition, keep a 10 Ti floor, cap total sentinels ever
   built. (see `config.AMMO_TARGET`, `MAX_EVER_BUILT`)
4. **Set up on the far face of the enemy core.** (31.8% → 45.5%, the single
   biggest jump)
5. **Never stall.** herbert19 body-blocks: it parks a builder on the tile ahead
   and mirrors every sideways step. The rusher once stood at gap 3, in range,
   with four legal sites around it, from turn 55 to the end of the game because
   the spot it preferred was behind the blocker. `COMMIT_AFTER_STALL` settles
   for a good tile after three idle turns. (50% → 68%)

## Two results worth keeping

**The large-map economy arm does not work.** Measured, 86 matches each against a
62.8% baseline: economy above `ECON_MIN_AREA` scores 39.5%, economy everywhere
scores 19.8%. Builder bots share the +20% cost-scale pool with sentinels, so
each extra builder adds 6 Ti to every sentinel still to come — two builders is
79 Ti of spawns plus 48 Ti of scale before a conveyor is laid. The code is still
here behind `ECON_ENABLED` with the numbers written down, so nobody has to
rediscover it.

**The last gap is arithmetic, not tuning.** Against a defender healing its own
core at ~11 HP/turn (herbert19 does), four sentinels net 25 HP/turn and need
~400 ammunition; the bank funds ~290. Every sentinel count is short:

    3 sentinels  27 dps  bank 344 Ti  need 469   short 125
    4 sentinels  36 dps  bank 290 Ti  need 400   short 110
    5 sentinels  45 dps  bank 230 Ti  need 368   short 138

Four is the least-bad and that is why `SENTINEL_TARGET` is 4. The games still
lost are the ones where the defender heals through the burst — visible directly
in `tools/beanalysis/ammo.py`, which decodes the global ammunition the replay
records in the economy event (team submessage field 7) and which nothing had
read before.

## Where it deliberately differs from v25

* **Avoids the map edge.** 21 of v25's 64 sentinels sit on the outermost row and
  48% of those died, against 12% for tiles one or two steps in.
* **Blacklists a dead site and its rays.** v25 rebuilds straight back onto the
  tile a gunner just cleared — in one game it fed (5,0) three times to one
  gunner at (6,0), ~225 Ti of scaled sentinels for a 20 Ti turret.
* **Never steps back onto the tile it was on two turns ago.** v25 loses 6-20
  turns a game to that two-cycle when an enemy builder is beside it.
* **Screens gunners with barriers.** Gunner shots are stopped by obstacles and
  sentinel shots are not, so 3 Ti turns off a 20 Ti turret. No bot in the 15
  reference games ever did this.

## Tools

`tools/beanalysis/` — `rushstat.py` (per-game failure mode, the tuning loop),
`ammo.py` (titanium + ammunition + core HP curves), `rusher.py` (per-turn trace
of one builder, throws included), `opening.py`, `placement.py`, `launches.py`,
`weapons.py`, `sweep.py`, `quick.sh`, `selfcheck.sh`.

Run `tools/beanalysis/selfcheck.sh` after every edit. `main.py` swallows
exceptions so a bug costs one turn instead of the unit — which is right for the
ladder and means a `NameError` otherwise looks exactly like a strategy that does
not work. One deleted helper cost a 22-game run reading 0% before anyone looked
at stderr.

## Round 2: the launcher, and why it stays off

You would expect the launcher to be the cheapest upgrade available. It is not,
and the reason is worth writing down because it is counter-intuitive twice over.

Three forms were built and measured against herbert19, 86 matches each, all 43
maps, both sides, against the 62.8% baseline:

    destination launcher (taxi between sites + evict defenders)   53.5%  (-9.3)
    cross-map transport relay, 1 hop                              40.9%
    cross-map transport relay, 2 hops                             18.2%
    cross-map transport relay, 3 hops                             18.2%

**The relay cannot work.** A hop needs the builder to stand on the pickup tile
doing nothing so the launcher can collect it — which costs exactly the turn the
hop was meant to save. Net gain is ~3 turns per hop for 20+ titanium, and the
titanium is worth more.

**The destination launcher should work and still does not.** It is genuinely
free on turn cost: it has a higher entity id than the builder that made it, so
it acts LATER in the same round and can throw a builder that has already spent
its action building a sentinel. That is why the reference bot builds one in 5 of
15 games, always 1-7 turns *before* the first sentinel. It loses here for a
duller reason — 20-24 titanium is two and a half sentinel shots out of a bank
already ~110 short of what a healing defender demands, and it has to stand on a
tile the battery wanted. Traced against herbert19, neither of its jobs ever
fired: no taxi throw, no eviction. It was pure cost.

It is one config line (`USE_LAUNCHER`) and worth revisiting against an opponent
that actually chases the rusher with builders — the case the eviction half
exists for, and one our local suite does not contain.

## The pattern behind every failed experiment

Six arms have now been measured and reverted, and five of them are the same
mistake in different clothes: **spending titanium on anything other than
ammunition.**

    economy, all maps                                    19.8%
    1337's full opening (3 sentinels + 290 ammo + medic)  22.1%
    medic at t1, parked on the core healing               27.9%
    economy, big maps only                                39.5%
    destination launcher                                  53.5%
    ---------------------------------------------------------
    baseline: one builder, four sentinels, nothing else   62.8%

Builder bots share the +20% cost-scale pool with sentinels, so a second builder
is never just its own price — it is that plus 6 titanium on every sentinel still
to come. And every 10 titanium not converted is one sentinel shot, 18 damage,
out of a budget that is already short.

The uncomfortable corollary: team 1337 runs the medic-plus-cheap-battery opening
and is the *only* team that beats the reference rush, 0-5 four times. The same
opening transplanted here is -40 points. Why it works for them and not for us is
the largest open question left in this bot.

## Ladder results (rushdown as our active submission, unrated unless noted)

    v119   8W-6L on matches, 36-34 on games
           5-0 vs Bean counters (RATED), 3-2, 3-2 | 5-0 Clankers | 5-0, 5-0 Part-timers
           0-5, 0-5 vs not adgato | 0-5, 1-4 Pivot | 1-4, 2-3 ph | 1-4 Lorem Ipsum
    v120   3W-1L   after adding gunner screening + chipping adjacent turrets:
           ph 1-4,2-3 -> 3-2 W    Pivot 0-5,1-4 -> 3-2 W    Lorem Ipsum 1-4 -> 0-5

51% on the real ladder against 75.6% locally is the honest gap: the local suite
is six bots from one lineage and they all defend the same way. The rush is a
counter-pick — it beats Bean counters and Clankers decisively and loses the
mirror to not adgato — not a replacement for our main bot, which beats all four
of ph / Pivot / 1337 / Lorem Ipsum on its own.

## Round 3: we were benchmarking on the wrong maps

`fcode maps list` says the competition pool is **15 maps, and we only had 5 of
them locally**. Every number above this section was measured on `--maps all`,
the 43-map legacy set in `maps/` — a set the ladder does not play. `fcode maps
sync` fixes it, and `tools/benchmark_bots.py` already defaults to the pool;
passing `--maps all` was an explicit opt-out into the wrong instrument.

The pool is also much bigger on average (four 30x30, two 24x24, a 28x18) where
the old set was full of 10x10s and 14x18s, so anything whose value scales with
the length of the walk was being measured under the wrong conditions.

Re-measured on the pool, 60 matches (2 seeds), vs herbert19:

    two-cycle guard OFF    63.3%
    two-cycle guard ON     70.0%     <- +6.7
    suite (Ladder_v36 / Khaos / loki, 180 matches)   72.8%

The launcher conclusion survives the move: 60.0% on with it, 66.7% off, so that
was not a map artifact.

## The two-cycle guard

The dominant loss on the pool was OUTRACED — our core dying first — and on
helheim it was total: 2 NO-BUILD and 3 OUTRACED across five opponents, no wins.
The trace shows why, and it is not symmetry (the target core was correct): the
rusher shuttled (6,7) <-> (6,8) from turn 12 to the end of the game while a
defender mirrored it one tile away.

A body-block presents as MOTION. The old stall detector counted turns spent
standing still, so it never fired. The fix is in `roles._step_toward`: refuse the
tile we occupied two turns ago when an alternative exists. Breaking the loop
beats reacting to it — the "give up and build here" alternative measured 58-62%
against 63.3% for leaving it off.

## Correction: the cost scale is GLOBAL

Probed directly (`/tmp/probe`, reproduced below). Spawning builders on
consecutive turns:

    r=0 scale=100%  sentinel=30 gunner=20 launcher=20 harvester=20 conveyor=3
    r=1 scale=120%  sentinel=36 gunner=24 launcher=24 harvester=24 conveyor=3
    r=5 scale=200%  sentinel=60 gunner=40 launcher=40 harvester=40 conveyor=6

There is ONE global scale and it multiplies every base cost. AGENTS.md's "as you
build more of that category" reads as per-category and is misleading. A second
probe confirms the reverse: a 3 Ti barrier moves the scale 120% -> 121%, and
that applies to sentinels too.

So the earlier explanation in this file -- "builders share the +20% pool with
sentinels" -- was right about the direction and wrong about the mechanism, and
the truth is worse: a builder taxes conveyors, harvesters, barriers and turrets
alike. It also means ORDER matters: build the expensive things first, while the
scale is still low.

## Open question, now sharper

Every defensive idea has measured negative against herbert19 -- but **herbert19
never attacks our core**, so that instrument can only ever see the cost of a
medic and never its benefit. On the live pool, OUTRACED is 12 of 25 games. The
medic question therefore cannot be settled locally and has to be A/B'd on the
ladder on a fixed map set, which is what `arms/rush_medic` (submission v123) is
for. First data point: Lorem Ipsum 1-4 without, 0-5 with.

## Round 3 results, and five more negative arms

Re-tuned every major constant on the pool, 60 matches an arm (2 seeds), vs
herbert19. **Every one already sat at its optimum**, so the tuning transferred
even though the map set did not:

    SENTINEL_TARGET     3 -> 60.0    4 -> 70.0    5 -> 71.7
    SITE_IDEAL_GAP      1 -> 56.7    2 -> 70.0    3 -> 61.7
    FAR_FACE_BONUS      0 -> 55.0   14 -> 70.0   28 -> 58.3
    MAX_EVER_BUILT      4 -> 63.3    5 -> 70.0    7 -> 65.0

Mechanisms tried and rejected this round:

    sentinels target enemy TURRETS before the core        61.7%  (-8.3)
    reactive core medic (measured ON THE LADDER)          6/20 games vs 7/20
    wider spawn search                                    impossible, see below

**The medic question is now settled.** It measured -35 locally, but herbert19
never attacks our core, so that instrument could only ever see the cost. A/B'd on
the ladder instead, on an identical fixed map set: 7 of 20 games without it, 6 of
20 with. It is not the reason 1337 beats the reference rush.

**There is no free tempo at spawn.** `CORE_SPAWNING_RADIUS_SQ` is 2, not the 8 of
`CORE_ACTION_RADIUS_SQ`; probed directly, the legal offsets from a 2x2 core are
exactly the ring.

**The approach is not the problem.** 85% path efficiency over 25 pool games,
mean 3.3 turns lost against a true-map BFS optimum -- and the consistent +3 is
the far-face detour, which is worth +14 points.

## Where this leaves the bot

    vs herbert19, pool, 60 matches      70.0%
    vs suite,     pool, 180 matches     72.8%   (Ladder_v36 66.7 / Khaos 58.3 / loki 93.3)
    on the ladder                       ~50% of games

The local number is a genuine local optimum: five independent mechanisms and
four parameter sweeps all fail to beat it. The ladder number is lower and that
gap is the real finding -- our six local opponents are one lineage that defends
one way, and none of them rushes our core, so the instrument is blind to exactly
the half of the game the ladder decides on.

## Round 4: the controlled comparison

Found games where not adgato and rushdown played the SAME opponent on the SAME
map from the SAME side, and diffed them.

**Lorem Ipsum @ midgard, both as side B.** adgato wins (core dead t77); we lose
(their core finishes on 282):

                       adgato (won)          rushdown (lost)
    sentinels          t55 57 59 60          t48 52 67 70
    span               5 turns               22 turns
    placement          (0,4)(0,3)(1,1)(0,2)  (6,0)(6,0)(2,0)(2,1)
    gap to core        2-3                   6, then 2
    survival           17-22 turns           3 and 4 turns for the first two

We arrive SEVEN TURNS EARLIER and throw it away. The first two sentinels go up
six tiles out and die in three turns each.

**Pivot @ holmgang, both as side B.** adgato builds five sentinels from t47 and
wins. We build NOTHING in seventy turns: Pivot parked two builders on the only
two sites next to our rusher -- (4,1) and (5,2) -- and it stood at (5,1) from
turn 14 to the end.

### Why the obvious fixes did not work

The objective explains the placement: a stand tile is scored at
STAND_PER_SITE=10 per site against SITE_CORE_WEIGHT=1 per tile of distance, so a
cluster of four sites six tiles out (score 36) beats a tight pair at gap 2
(score 20). But raising the proximity weight measured:

    SITE_CORE_WEIGHT   1 -> 70.0    4 -> 56.7    8 -> 55.0   12 -> 55.0   (pool)
    ladder A/B, identical maps, tight vs loose:  6/20 games vs 6/20

Neither instrument supports the change. Excluding bot-occupied tiles from the
site filter -- the obvious answer to the holmgang block -- costs 6.7 points,
because a defender stepping across a site for one turn should not make the
rusher abandon a spot it walked twenty turns to reach.

### The launcher, third design, and why it is still off

The right design (build sentinel -> launch to a tile in the launcher's OWN
eight-neighbourhood -> build -> launch, so the builder never leaves pickup range
and the launcher survives as a bouncer) was built and works. Instrumented over 8
pool games: **7 launchers built, 0 taxi throws, 0 evictions.** It is redundant --
`_pick_stand` already picks a hub whose four cardinal neighbours are all sites,
so the builder gets one-sentinel-per-turn standing still, for nothing. 53.3%
with it, 70.0% without.

### What this round actually establishes

The local benchmark cannot see the thing that decides ladder games. herbert19
does not build counter-turrets within two turns of our first sentinel, and does
not park two builders on our sites. Lorem Ipsum, Pivot and ph do all three.
Every fix aimed at those behaviours measures neutral or negative locally, and
the two that were also A/B'd on the ladder came back neutral there as well.

## Round 5: what actually keeps a sentinel alive

The question was how not adgato picks a spot its sentinels survive on. The
answer is that **it is not the spot**.

Every site feature was tested against realised survival, on both bots, using
kill-rate rather than lifetime (adgato's games END at t60-77 because it wins, so
raw lifetime says our sentinels live five times longer):

    feature              rushdown killed    adgato killed
    gap 4-9                   75%               20%
    gap 0-2                   19%               35%
    on the map edge           41%               48%
    one tile in               20%                0%
    5-8 directions sealed     36%               50%
    0-2 directions sealed     18%               14%

Two of those look decisive and neither survives intervention:

    hard gap cap only     68.3%      edge fix only   66.7%
    both                  65.0%      neither         70.0%

They are confounded. The bot places far, or gets boxed in, precisely when the
good spots are blocked or taken -- so those placements MARK a bad situation
rather than cause one, and forbidding them just means building nothing there.

**The enclosure idea is refuted outright.** For both bots, MORE sealed
directions means MORE likely destroyed. Being surrounded is what being cornered
looks like, not what being safe looks like.

### What does discriminate

Split adgato's own games by result. Arrival and loiter are identical either way:

                        arrive   loiter   1st->4th span
    adgato, 15 wins      t22.7    13.5        4.3   (max 9)
    adgato, 31 losses    t21.9    14.4       20.2   (max 122)
    rushdown, pool       t23.4     5.5        7.3   (max 22)

**Assembly span is the whole thing.** A battery that goes up in four turns is at
its full 36 damage a round before the defender has anything that shoots; one
that dribbles out gets farmed a turret at a time. And this reframes the loiter:
it is not hesitation, it is waiting for a hub that can produce the WHOLE battery
at once. We loiter less than adgato does and pay for it at the other end.

The race confirms it from the other side -- turns between our fourth sentinel and
the defender's first turret:

    adgato    -2.6 turns   (battery finished before their turret in 4/15 games)
    rushdown  -9.4 turns   (3/22)

### The change, and where it got to

`rusher_turn` now refuses to place the FIRST sentinel on a spot offering fewer
than MIN_SITES_TO_OPEN sites -- but only when an enemy builder is actually in
vision, because with nobody there to punish a slow battery there is nothing to
be patient for. Measured on the ladder, it does what it was designed to do:

    mean 1st->4th span   25.8 -> 11.1   (max 172 -> 59)

and the win rate did not move (6/20 games either way). That is consistent rather
than contradictory: adgato wins at 4.3 and loses at 20.2, so 11.1 is still on
the losing side of its own threshold. Pushing further needs MIN_SITES_TO_OPEN=4,
which measures -13 (56.7%) because the four-site hub often never materialises and
we simply never open.

### ...and then the UR test refuted it

45 games on two fixed map sets against 1337, Pivot, Lorem Ipsum, Bean counters
and not adgato, v122 against v125: **every single match ended in the identical
score**, 12/45 either way. The gate never binds, because `_pick_stand` already
picks hubs with three or more sites -- which the local sweep had said too, with
1, 2 and 3 all returning exactly 42W-18L.

Worse, the span improvement was mis-attributed. Measured properly on ladder
replays:

    v121 (before the two-cycle guard)   span 25.8   max 172
    v122 (two-cycle guard)              span  7.7   max  19
    v125 (+ this gate)                  span 11.1   max  59

The two-cycle guard did all of it, and the gate slightly undid it.

**And that undercuts the hypothesis entirely.** v122 already assembles in 7.7
turns, close to adgato's winning 4.3 -- and still wins 12 of 45. So the
4.3-versus-20.2 split in adgato's own games is most likely REVERSE causality:
when it is winning the defender is already dead and the battery goes up
unopposed; when it is losing the spot is contested and every sentinel is a
fight. Span is an effect of winning, not a cause of it.

That is the third feature this file has recorded as predictive-but-not-causal,
after placement gap and edge distance. The pattern is consistent enough to be
the finding: nothing observable about WHERE or HOW FAST the battery goes up
changes the result, because all of it is downstream of whether the defender
showed up.

## Round 6: the gap, finally measured precisely

Decomposed the damage instead of the geometry, and the answer is concentration.

    against the same five opponents        dealt   healed off   net
    adgato vs Lorem/Pivot (won)              576          83    493
    adgato vs Pantheon/Bean (won)            580         112    468
    rushdown                                 729         512    217

**We deliver MORE damage than the reference bot and they heal off 512 of it.**
Not an output problem -- a concentration problem. Peak damage in any 10-turn
window, at comparable game lengths (61 vs 63 turns):

    adgato     347      (~34.7/turn, i.e. four sentinels firing at the cap)
    rushdown   156      (~15.6/turn, i.e. 1.7 sentinels)

And the cause, which is the single cleanest number in this whole investigation:

                          sentinels built   peak ALIVE at once   turns with 4 up
    adgato                      4.2               3.90               12.0
    rushdown (v122)             4.25              2.57                5.9

We build the same battery and never stand it up. Turrets die and get replaced
instead of ever firing together, so four sentinels' worth of titanium delivers
1.7 sentinels' worth of damage.

Note this also retires two earlier conclusions in this file. The "3.2 sentinels
per game" figure was measured on v121 replays, before the two-cycle guard; v122
builds 4.25, matching the reference exactly. And every remaining battery metric
now matches too -- 100% of our sentinels have the enemy core on their firing
line (80/80, same as adgato's 64/64), and inside the burst window we out-fire
them, 0.674 shots per sentinel-turn against 0.533.

### What was tried against it

    STAND_STICKINESS   12 -> 70.0     22 -> 58.3     35 -> 60.0
    SENTINEL_TARGET     4 -> 70.0      5 -> 64.4      6 -> 55.6   (90 matches)
    walk at free sites when boxed in   ladder 12/45, same as before -- and a
                                       REGRESSION on its own metric: sentinels
                                       per game 4.25 -> 3.50, incomplete
                                       batteries 1/20 -> 15/40

Peak concurrency is the right target and none of the available levers move it.
Raising stickiness to hold the hub through the whole assembly costs 12 points;
building a fifth sentinel costs 6; chasing free sites when boxed in costs the
battery itself.

### Where a future attempt should start

Concurrency, not placement, not span, not survival, not output. Four sentinels
standing together for twelve turns is 347 damage in a ten-turn window and kills
a healing core; the same four built and replaced over twenty turns is 156 and
does not. The measurement is one command:

    python3 tools/beanalysis/output.py 'replays/*.replay26' --side N

## Round 7: the launcher, understood completely (and still off)

The concurrency finding above gives the launcher a real job: if the builder can
be thrown between sites instead of walking, all four sentinels go up on
consecutive turns and stand together. That is exactly what the reference bot
does, and it is why it builds one.

Chasing why ours never fired turned up three genuine bugs, each fixed:

1. **Pickup scanned four tiles, not eight.** `can_launch` accepts any builder at
   Chebyshev 1, so scanning only the cardinals silently lost half of them.
2. **The launcher was built before we had committed to a hub.** Placed on a
   cardinal neighbour of wherever the builder happened to be standing -- traced
   on jotunheim, launcher on (19,17) at t26, after which the builder walked off
   and built its whole cluster around (20,19)-(20,20), permanently out of range.
   Moved to after the first sentinel, so the hub is settled first.
3. **Nothing kept the builder in the orbit.** Even placed correctly, the builder
   wanders out of pickup range between builds, because `_pick_stand` did not
   know the launcher existed.

Fixing all three DOES produce the intended behaviour -- with stand tiles
restricted to the launcher's orbit, the last three sentinels went up on three
consecutive turns. And it costs 23 points: **46.7% against a 70.0% baseline**,
because eight tiles is not enough to hold four sites and the builder ends up
locked in a pocket with nothing to build.

So all three previous launcher measurements were measuring an ornament -- the
taxi genuinely never fired -- but fixing that does not rescue it. The mechanism
works and the constraint it imposes costs more than the tempo it buys.

    launcher off (current)                          70.0%
    launcher on, orbit-constrained builder          46.7%
    launcher on, unconstrained (taxi never fires)   53.3%
    cross-map relay, 1 / 2 / 3 hops          40.9 / 18.2 / 18.2%

## Round 8: planning a tour of sites

The concurrency finding says the bot should only commit to a region where all
four sentinels can actually be placed -- worst case build, move, build, move,
build, move, build, seven turns. `_pick_stand` did not do that: it scored a tile
by the sites on its four cardinal neighbours, so a two-site tile looked fine and
the relocation afterwards is where the battery fell apart.

So `_tour` was added: from a candidate tile, repeatedly step to whichever
adjacent stand tile exposes the most uncounted sites, and score the tile by what
the whole tour can place and how many turns it costs.

**It improves the metric it targets and loses anyway.**

    peak concurrent sentinels   2.85 -> 3.00      (reference bot: 3.90)
    sentinels built per game    3.50 -> 3.65      (reference bot: 4.20)

    TOUR_MAX_MOVES   0 -> 71.7%   1 -> 66.7%   2 -> 66.7%   3 -> 66.7%
    TOUR_TURN_COST   0 -> 65.0%   0.75 -> 66.7%  1.5 -> 66.7%  3.0 -> 55.0%

Zero moves -- the planner disabled -- is the best setting at every combination.
The reason is that a plan degrades faster than it pays: sites get taken, blocked
or built on between the turn a tour is chosen and the turn it would be walked,
so committing to a region on the promise of a multi-step route is worse than
taking the best tile available now and re-deciding every turn.

That is the fourth time in this file that moving the right metric has failed to
move the result, after placement gap, edge distance and assembly span. The
pattern is consistent enough to be the conclusion: **the observable properties of
the battery are all downstream of whether the defender contests the spot, and
optimising them directly buys nothing.**

## Round 9: CORRECTION -- rounds 6 and 8 were measured on the wrong side

The analyses above that used `--side 1` on ladder replays were wrong whenever
Pantheon was team A, which is 5 of 20 v122 matches. In those games the numbers
describe the OPPONENT. The tell was the action census: it showed our rusher
being "thrown" 1.9 times a game while `USE_LAUNCHER` is False -- those were an
opponent's launcher relay, being counted as ours.

Re-measured with the side taken from each match's own metadata:

                              built   peak alive        as previously reported
    adgato                     4.20      3.90
    rushdown v122              4.25      3.55           (2.57 -- wrong)
    rushdown v121              3.20      2.84

And the damage decomposition, which round 6 built its whole conclusion on:

                        peak/10t   dealt   healed off   length
    adgato (Lorem/Pivot)     347     576           83       76
    adgato (Pantheon/Bean)   341     580          112       61
    rushdown v122            316     503           86       63     (was 156 / 729 / 512)

So the round 6 story -- "we deliver more damage and they heal off 512 of it" --
is false. Healing is not the difference: they heal 86 off us and 83 off adgato.
Game length is not the difference either: 63 turns against 61.

**The real gap is small and purely output.** We deal 503 where the reference bot
deals 576-580: about 75 damage, four sentinel shots. It is not ammunition (zero
turns below one shot's worth, for either bot, and we convert MORE ammunition than
they do: 226 against 194-218). It is not titanium (we finish with 39.5 in the
bank against their 16.3 -- we UNDERSPEND). It is the last 0.35 of a sentinel of
concurrency, 3.55 against 3.90, worth roughly 40 of the 75.

Which means the bot is far closer to the reference than rounds 6-8 claimed, and
the remaining difference is about four shots -- not a strategic gap.

**Process note.** Three times now a conclusion has come from a metric measured
the wrong way: lifetime censored by game length, span attributed to the wrong
version, and now the side. The fix each time was to check the target metric
directly rather than the win rate. Any future measurement over ladder replays
must resolve the side per match -- `/tmp/sides.json` style, from `fcode match
info` -- and never assume side 1.

## Round 10: the authoritative numbers, from 305 games

Rounds 6 and 9 were both measured on unrepresentative samples -- round 6 used
the wrong SIDE, round 9 corrected that but used 20 rated ladder games we had
mostly won. The ladder had meanwhile played 61 unrated matches against the top
five teams with v122 active. Taking all 305 of those games, with each match's
side resolved from its own metadata:

                          rushdown v122 (305 games)      adgato
    games won                    41%
    sentinels built             3.44                      4.20
    peak alive at once          2.72                      3.90
    peak damage / 10 turns       234                       347
    damage dealt / healed off  530 / 271                576 / 83
    game length                  103                     61-76

So round 6 was right in substance and wrong in magnitude, and round 9's
"correction" was itself wrong. The concurrency gap is real: **2.72 against 3.90**.

### And it is not a tuning problem, it is a bimodal failure

Sentinels built per game over those 305:

    0 sentinels   54 games (17.7%)      <-- the whole problem
    1-3           32 games (10.5%)
    4-5          219 games (71.8%)

Nearly three quarters of games build a full battery. The mean is dragged down by
a hard failure bucket, and the failures split:

    reached the core (gap<=5) and never built    31
    never reached the core at all                23

For the 31, the tiles the rusher could have built on were **45% FREE**. They
were simply not firing positions -- nothing adjacent had a line to the core --
and the fallback kept walking at a core ring the rusher was already standing on.

Fixed narrowly: once `gap <= ARRIVED_GAP`, a rusher with no reachable hub walks
at the stand tiles of any free site instead of at the ring. The gate is
deliberately tight because the wide version of this (firing on every
`stand is None`, including mid-approach) cost 0.75 sentinels a game by pulling
the rusher off good hubs. Neutral locally -- herbert19 never creates the state --
so it has to be measured on the ladder.

### The lesson about instruments

Three rounds of conclusions came from samples that could not answer the
question: censored lifetimes, the wrong side, and a winners-only subset. The
local suite cannot see this failure at all. The only instrument with the power
to measure it is the ladder's own unrated stream, which accumulates ~300 games
against the top five if the arm is left active. Anything measured on 60-90 local
matches against herbert19 should be treated as a check for regressions, not as
evidence that a change helps.

## v129 -- self-blocking (the rusher entombs itself)

Our own sentinels are solid buildings, so every one built on a cardinal
neighbour is one fewer way out of the tile we are standing on. Over the 305-game
unrated corpus, of the 32 games that stopped short of four sentinels:

    21 of 32 had three or four of the four exits blocked (6 blocked outright)
    of those that stopped at exactly 3: 10 of 14 were blocked by their OWN turret

Two changes, and one instructive failure:

* **Build order** (`_try_build_sentinel`) -- when more than one site is adjacent
  and the battery is unfinished, build the ones that do not seal us in first and
  leave the sealing site for last. Pure reordering, never refuses; 71.1% (neutral
  locally, as expected -- see below).
* **Dead ends** (`_pick_stand`) -- a spot that cannot finish the battery and has
  no non-site way out is worth only what it can build. Penalised: 72.2%.

The failure is the interesting half. **Refusing** the sealing build outright
measured 66.7% against 71.1%: if the sealing site is the only one available,
skipping it builds nothing at all, which is strictly worse than being stranded
with one more turret. And penalising *every* unfinishable dead end cost ten
points (71.7 -> 61.7, identical at penalty 12 and 30, so it flips a consistent
set of choices). A tile with no way out is walled, and walls are what keep a
sentinel alive -- the penalty trades pockets for open ground and the battery gets
shot instead of stranded. Hence `DEAD_END_MAX_SITES = 2`: only a dead end that
traps us at one or two is worth avoiding. Three-in-a-pocket beats four-in-the-open.

Local benchmarks are near-blind to all of this (the herbert19 sparring partner
does not contest the spot the way the ladder field does), so the numbers above
are a safety check, not the measurement. The instrument is the unrated stream.

## v130 -- what actually beats us, measured against not adgato

Five unrated games against not adgato on a fixed map set, lost 0-5. The games
are DETERMINISTIC -- five separate unrated challenges on the same five maps
returned identical winners and identical turn counts, with the sides swapped --
so one challenge is a complete measurement and repeating it buys nothing.

Two distinct failures, and neither is the one we had been optimising:

**Nobody defends any more.** Across all five games not one sentinel on either
side was ever destroyed. The entire justification for the careful placement work
-- exposure, gap caps, dead-site memory, the far face -- is a defender who is no
longer there. It is a pure race with a stopwatch on it.

**On big maps we lose the race by nine turns.** Battery complete t44/t39/t35 for
them against t53/never/t44 for us on the three 30x30 maps; they killed us at
t48-56. Tracing the walk showed the rusher's first two steps taking it FURTHER
from the target core (distance 10 -> 11 -> 12) to come round the outside. That is
FAR_FACE_BONUS, and it is worth +7 points against an opponent that defends and
-31 against one that does not. It is now conditional on having actually seen
opposition -- an enemy building that is not their core, or a builder still
standing at their core once we have arrived. Their rusher crossing us in the
open no longer counts, which is the distinction that matters: in a rush matchup
we always see an enemy builder, and it is never a defender.

**On small maps the rush cannot close, and then we have no second act.** On an
18x18 we put their core to 80 HP by t40 -- and both banks ran dry at t40. They
then parked a builder on their core and repaired it 80 -> 498 while banking the
surplus; our core sat at 284 and never healed at all, because we were converting
every scrap of passive income into ammunition and dribbling it at 4.5 HP a turn
into a core being repaired at 5.2. We spent our whole income for a net negative,
and they burst us down at leisure. The arithmetic behind it:

    heal    1 Ti -> 4 HP
    shot   10 Ti -> 18 HP   = 1.8 HP per Ti

Repairing is 2.2x more titanium-efficient than shooting, so on equal income the
side that repairs wins every game that does not end in the opening burst.

The answer is a package, and only a package -- each piece alone measured as a
regression:

* **Healers** (`_wants_healer`) -- up to HEALER_MAX builders that only repair,
  never defend. Gated four ways: the battery must already be paid for, the core
  must be below HEALER_HP_FRACTION, and the match must be a CONFIRMED rush
  mirror. Ungated it cost 40 points; correctly gated it is free locally (61.7%
  either way) because it never fires in a game that ends quickly.
* **The barrier ring** (`_try_barrier_ring`) -- eight 3-titanium barriers on the
  tiles orthogonally adjacent to their core, which are the only tiles a builder
  can repair it from. Built during the TERMINAL state, on the turns the rusher
  was spending on nothing anyway (the reference bot idles 74% of them).
* **The rush-mirror flag** (`_rush_mirror`) -- two facts recorded by different
  units: our rusher saw a lone enemy builder crossing the open map, and then
  found their base empty when it arrived. Both are required before a titanium is
  spent on either of the above.
* **Sentinels prefer builder bots to the core** -- 40 HP, so three shots kill the
  repairer that is undoing more damage than we can deal.

### The self-inflicted deadlock

Separately, and worth more than all of it: `_step_toward` computed the ideal
first step and, if that one tile was blocked, DID NOTHING. Every turn. The flood
plans through enemy builders because they are units, not buildings, so the ideal
step is routinely a tile with an enemy body on it. On helheim the rusher stopped
at (8,6) on turn 7 with the enemy rusher at (9,6) and stood there until turn
1000 -- on a map with no walls at all, which it could have walked around in two
steps. Three of fifteen pool maps ended that way, against 17.7% of ladder games
that build no sentinel whatsoever. It now ranks the other three directions by
distance to the goal and takes the first legal one.

Note the tension this exposed: breaking the deadlock made the MIRROR worse
(50.0% -> 36.7%), because in a head-on block the bot that yields loses the race
while the stubborn one gets a coinflip. That is an argument about who blinks,
not an argument for standing still for a thousand turns.

## v131 -- narrowing beats reverting

The v130 batch lost 13 points against herbert19 while gaining 12 against a pure
rusher. Ablated one flag at a time from a frozen snapshot, two of the three
ungated changes were responsible and the third was innocent:

    SENTINEL_HUNTS_BUILDERS   on 58.3%   off 70.0%    -11.7
    SIDESTEP_BLOCKED          on 58.3%   off 68.3%    -10.0
    far-face conditional         58.3%       58.3%      0.0

The far-face conditional costing exactly nothing is the conditional WORKING:
`_opposition` latches against an opponent with buildings, so the fast-route
value is never the one used. It only changes behaviour against an empty base.

Neither of the other two was reverted, because both were right in the situation
they were designed for and wrong everywhere else:

* **Hunting builders** is now gated on `_rush_mirror`. Against an economy their
  builders are everywhere and preferring them turns the battery away from the
  core to chase 40 HP targets that keep walking. In a rush mirror there is
  exactly ONE enemy builder, its job is repairing at 4 HP per titanium, and
  three shots delete it.
* **The sidestep** now stands firm for `SIDESTEP_AFTER` turns first. Yielding a
  corridor hands the race to whoever stays put, which is why immediate
  sidestepping cost 10 points against a bot that body-blocks on purpose -- but
  never yielding is the thousand-turn deadlock. Standing firm and then walking
  around wins the staring contest against a blocker that is merely passing:

    SIDESTEP_AFTER   1 -> 70.0%    3 -> 80.0%    8 -> 81.7%

Both together put the bot at 80%+ against herbert19, ABOVE the 72.2% it managed
before either change existed -- so the two features are now worth more than
their absence, rather than less.

The lesson worth keeping: a feature that measures as a regression is not
necessarily a bad feature, it is often a feature with no precondition on it. All
three of these -- the healers, the ring, the builder-hunting -- were regressions
until they were told when to apply.

## v132 -- the assembly span, which was most of the nine turns

Against not adgato the first sentinel now goes down at t31 rather than t36, but
the battery still finished no earlier, so the remaining gap was the SPAN. Traced
on valkyrie:

    t36 build (26,17)   t37 step east   t38 build (27,17)   t39 step east
    t40 build (28,17)   t41-43 step x3  t44 build (29,15)

Eight turns for four sentinels, against not adgato's three (t32-t35). And the
tile it stepped onto at t37 was itself a site it could have built from where it
was already standing -- the bot re-shopped for a better hub every single turn
and paid a turn of tempo each time.

So after the first sentinel, build from where we stand instead of relocating.
Choosing the FIRST hub carefully is still right; that decision happens while
placed == 0. After that the best tile is the one we are on, because standing
there is free.

Conditional, on the same signal as the far face and for the same reason:

    build-before-moving      vs herbert19 (defends)   vs rush mirror
      on                          75.0%                   78.3%
      off                         81.7%                   65.0%

Against a defender the careful hub is worth its tempo; against a rusher, who
never contests anything, tempo is the entire game. Gated on `saw_defender` it
takes the mirror from 65.6% to 78.9% at no cost against herbert19 (81.1% both).

### Open: the single-sentinel stall against not adgato

On yggdrasil against not adgato the rusher built one sentinel at t38 from (8,3)
and then stood on that tile for twelve turns doing nothing, with 384 titanium in
the bank and a valid firing site at (9,3) directly beside it. Not affordability,
not an exception (a clean error sweep across five maps found none), and NOT
reproducible locally: the same map in the same orientation against our own
sparring copy builds four sentinels from that exact tile, t38/39/40/42,
including (9,3). Something about that specific opponent state makes every site
adjacent to (8,3) fail `_sites_around`. Unresolved -- do not assume the span fix
is at fault, and do not "fix" it without a reproduction.

## v133 -- a frozen builder is always a bug

Lost 0-5 to ph. On the two 30x30 maps of the five, the rusher built one sentinel
and zero; on the three smaller ones it played normally. The trace is stark: on
game 2 it walked two steps, stopped on (5,13) at turn 3, and stood there until
turn 151 -- twenty-two tiles from the core it was sent to attack, with nothing
adjacent to it.

Could not be reproduced. Eighteen local games, six opponents, all three 30x30
pool maps: four or five sentinels every time and not one exception. Peak turn
cost measured 4.78ms of the 10ms budget, so a slower server timing out is
possible but a PERMANENT freeze from t3 is not what an intermittent timeout
looks like.

One real bug was found by reading rather than reproducing, and it fits exactly:

    if self.st is None:
        self.st = roles.State(ct)
        self.kind = ct.get_entity_type()     # <-- anything throwing above here

`self.kind` was set after `State` was built, inside a guard on `self.st`. If
anything threw in between, `st` was assigned, `kind` stayed None, init never ran
again because the guard is on `st`, and every dispatch branch compared against
None. The unit then did nothing for the rest of the game -- permanently, from
its first turn, on one map and not another. Kind is now set first and
independently.

Since the cause is not confirmed, the SYMPTOM is now impossible:

* **`panic()`** -- a deliberately dumb step toward the enemy core. No flood, no
  scoring, no memory, because it has to work in the situation where the clever
  code did not. Called by the watchdog, and again from main's exception handler:
  a unit that throws must not also stand still.
* **The watchdog** -- twelve turns of a builder neither moving nor building is
  not a tactical choice. Gated on an UNFINISHED battery, because standing still
  is the terminal state's entire job.

No cost: 81.1% against herbert19 (unchanged) and 80.0% against a pure rusher.

## v135 -- circle until the crowd leaves

not adgato does not walk in and build. Traced against ph, its rusher spends
t23-t43 walking a full circuit around the enemy core at gap 3-8, passing
defenders at distance 3-4 the whole way, and plants nothing. At t42 the builders
drift off, and it puts up all four sentinels at t44/46/48/49.

Quantified over five of its games, the distance to the nearest enemy builder AT
THE MOMENT IT OPENS:

    not adgato   5, 6, 6, 5, 3        35% of builds on clear turns, 2.5x rate
    ours         2, 1, 2, 3           96% of builds with a defender inside 4

And ours died for it: on glacierkeep t47, t91, t106, t126, t150 -- five turrets
fed in one at a time, peak alive one. A sentinel is 30+ titanium and takes a
turn to place; a builder standing next to it takes it apart at 2 titanium a hit.

Two mistakes were made implementing this, both worth keeping written down:

* Gated on `placed == 0`. That falls back to zero when a turret dies, so every
  REBUILD waited for a clearing too and the fourth sentinel slipped t51 -> t92.
  It is `ever_built == 0`: waiting is worth it to place a battery into open
  ground, not to replace one turret mid-fight.
* Bounded by `since_progress`, which resets whenever we get closer to the core
  than ever before -- and circling does that by accident. The wait was
  unbounded and our opening slid to t119-128 against their t30-44. It now has
  its own counter.

The gate is a CROWD, and the discriminator was the opposite of the obvious
guess:

    vs ph          mean 2.34 enemy builders within 4, 51% of turns >= 2, 26 all game
    vs herbert19   mean 0.94,                          9% of turns >= 2,  4 all game

ph swarms; herbert19 barely defends at all. Waiting out a crowd that will
dismantle the turret pays; waiting out one loiterer is just not shooting.
Ungated the stall cost 15 points against herbert19; gated at two defenders it
costs nothing at all.

    vs ph          0-5  ->  1-4       (93% of builds now on clear turns)
    vs herbert19   81.1% -> 81.1%
    vs pure rush   80.0% -> 80.0%

Still open: against ph our first sentinel lands at t119-130 on the maps we lose,
against t26 on the one we win. The stall is capped, so that lateness is arrival,
not waiting -- their base is dense with buildings and most firing sites fail
`_tile_free`. That is the next thing to look at.

## v138 -- why not adgato beats ph and we do not

Traced game by game on the maps both of us play against ph.

**They arrive sooner and kill before the economy matters.** Turns of overhead
beyond the straight-line walk to the core:

    glacierkeep   not adgato +0    us +12
    valkyrie                 +2       +5
    stavkirke               +17      +31

On glacierkeep they reach the core at t16, build four sentinels t44-t49, lose
none, and win at t68. Note they are NOT dominant -- across four matches they go
13-7, losing icefloe, longhouse, helheim, holmgang and bifrost. They win the
fast maps and lose the slow ones, exactly as we do; they just have more fast
maps because they get there sooner.

**ph rings OUR core with barriers.** Eight of them at distance 1, t19 through
t87 -- the same denial play this bot builds, aimed at us. It does the same to
not adgato (t18-t47); they simply win before it lands.

**ph keeps a launcher beside its core.** This was worth a whole game. On
glacierkeep it picked our rusher up and threw it three tiles back, twenty times:
in at t28, thrown t31, in again, thrown t35, again, thrown t39, and we built
ZERO sentinels in 149 turns. Three attempts to fix it, in order:

1. Penalise grip tiles as DESTINATIONS. No effect and slightly worse (20
   launches -> 32): the rusher never chose to stand there, it was thrown while
   walking through.
2. Refuse to step into the grip. Works, but blanket avoidance cost 81.1% ->
   74.4% against herbert19, which builds launchers of its own and never throws
   us. Now armed only by evidence -- a throw is the only thing that moves a
   builder more than one tile in a turn.
3. REMEMBER the launcher. Vision is four tiles, so backing off took it out of
   sight, emptied the grip and walked us back in. Buildings do not move.

That took ph from 1-4 to 2-3, with icefloe flipping from a t300 loss to a t48
win, four sentinels built and none destroyed.

Also tried and REVERTED (it went back to 1-4): removing grip tiles from the
flood entirely and refusing firing sites inside it. More thorough, and worse.

Still unsolved: glacierkeep. 23-26 launches survive every version of this and we
still build nothing there. The launcher sits between us and the only face we
will accept, and the local step rule walks the boundary of its reach rather than
committing to another approach.
