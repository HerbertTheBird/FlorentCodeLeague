"""Per-map hardcoded openers: the first few turns, written out tile by tile.

The pool is a fixed set of published maps, so on a map we have studied there is
no reason to rediscover the opening every game. This module is the table of what
to do; `units/opener.py` is the interpreter that runs it and `tools/check_openers.py`
is the offline checker that proves every coordinate in it is legal against the
real .map26 before a game is ever played.

Deliberately pure data with no `fcode` import, so the checker can load the exact
same table the bot will run. Entity kinds are plain strings, resolved to
EntityType by the interpreter.

# Writing an opener

Coordinates are written for ONE side -- whichever side owns the core at `core`.
The other side gets them mirrored through `sym`, so an opener is authored once.
(`sym` is the map's own symmetry, the same letter `tools/gen_mapdata.py` derives:
'h' mirrors x, 'v' mirrors y, 'r' does both.)

Three independent scripts, because three different kinds of unit have to be told
what to do and they do not run in a fixed order within a turn:

    core      a list of ops for the core, in order
    launchers {(x, y) of the launcher: [ops]} -- a launcher finds itself by where
              it stands, which is also how it survives being built a turn late
    builders  a list of per-role op lists. Role index is claim order: the first
              scripted builder to run is role 0, the next is role 1, and so on.

Ops, all gated on the matching can_*() at runtime and simply retried next turn if
they are not legal yet:

    (SPAWN,  (x, y))              core: spawn a builder on this tile
    (LAUNCH, (from_x, from_y), (to_x, to_y))
                                  launcher: throw whatever friendly builder is
                                  standing on from_* to to_*
    (LAUNCH, (from_x, from_y), rule)
                                  the same, to a tile the table cannot name.
                                  Three rules, all resolved by the launcher at
                                  the moment of the throw and all documented at
                                  `_throw_target` in units/opener.py:
                                    NEAR_CORE       the tile in range nearest
                                                    the enemy core
                                    SAME            whatever this launcher's
                                                    last rule chose
                                    (FLANK, (x, y)) that tile if it is free,
                                                    else the tile nearest the
                                                    core on the same side of it
    (BUILD,  kind, (x, y))        builder: build `kind` on this orthogonally
    (BUILD,  kind, (x, y), facing)  adjacent tile. Conveyors, splitters, gunners
                                  and sentinels need the facing, as a compass
                                  name ("southeast"); it is mirrored with the
                                  coordinates for the other side.
    (GOTO,   (x, y))              builder: path here, one step a turn
    (WAIT,   (x, y))              builder: do nothing until standing on (x, y).
                                  This is how a builder waits to be launched --
                                  it must not wander off the launcher's pickup
                                  tile, and it must not start its walk until it
                                  has actually landed.
    (WAIT,   RELAY)               builder: do nothing until no friendly launcher
                                  is within pickup reach (d^2 <= 2) any more.
                                  The form for a relay, where the landing tile
                                  of one hop is the pickup tile of the next and
                                  the number of hops left is not something the
                                  waiting builder can count. A named tile cannot
                                  do this job: two throws can land in the SAME
                                  round -- glacierkeep's do, because the two
                                  launchers involved act in id order and the
                                  earlier one clears the pickup tile the later
                                  one throws from -- and a builder that never
                                  ran on the tile in between would wait for it
                                  forever.
    (STRIKE, (x, y), kind)        builder: take this orthogonally adjacent tile.
                                  Break whatever enemy building is on it, then
                                  build `kind` there -- the point is to own the
                                  tile, and whether that needs a fight is not
                                  knowable when the script is written.
                                  While an enemy builder stands next to the
                                  target it heals 4 HP for 1 Ti against our 2
                                  damage for 2 Ti, so hitting it alone is worse
                                  than not hitting it: hold, and let a scripted
                                  sentinel's 18 arrive to make up a one-shot.
                                  With nobody there to heal it, chip away alone.
                                  A map may set `strike_stands_off` to refuse the
                                  fight outright and simply skip an occupied
                                  tile. That is right where every STRIKE target
                                  is beside the enemy CORE and therefore always
                                  defended -- the exchange is four to one against
                                  us and a hole in the ring costs less than the
                                  bank closing it would take. It is wrong on a
                                  lone doorway nobody is standing next to.
    (SKIP_UNLESS_ENEMY, (x, y), n)
    (SKIP_UNLESS_ENEMY, ((x, y), (x, y), ...), n)
                                  builder: unless an enemy building stands on
                                  (x, y) -- or on any one of them, in the second
                                  form -- skip the next n ops. Guards work that
                                  is only worth doing if the enemy is actually
                                  there -- a sentinel shares its cost scale with
                                  builder bots and can be 85 Ti by the time one
                                  is wanted, which is not a price worth paying to
                                  shoot at an empty tile. Only tiles this builder
                                  can SEE answer honestly, so keep them inside
                                  the r^2=20 it will have when it reads them.
    (ASSAULT, (stand_x, stand_y), (target_x, target_y))
                                  builder: go to the standing tile and hit the
                                  target from it for the rest of the game. Never
                                  completes, so it is always the last op.

`keep_launchers` stops the scripted launchers retiring after their last throw.
Set it when the launchers are still needed -- on a map whose script walls our own
half off, they are the only way across the wall we built.

A `ferry` section turns them into that permanent gateway:

    ferry {(x, y) of the launcher: ((pickup tile), (destination))}

Once its scripted throws are done, a ferry launcher throws whatever friendly
builder is standing on its pickup tile to the destination, for the rest of the
game. The pickup tile is named rather than "anything adjacent" for the same
reason LAUNCH names one: a launcher beside the core is also beside the core's
spawn ring, and "anything adjacent" would fling every newly spawned builder
across the map.

Builders put themselves on that tile once `ferry_after` is satisfied -- see
`units/states/opening.py`. `ferry_after: "ore_worked"` means "every ore tile in
our half has a harvester on it", i.e. the economy has nothing left to build.

`ore` lists exactly which tiles those are. Naming them beats deriving them: the
derived version asks whether every ore in the Voronoi harvest zone is worked,
which is a live reading that flips false the moment one harvester dies, and
which counts ore on the far side of a seal we built and can never reach. A
builder that has not actually LOOKED at one of these tiles does not guess -- it
goes and looks, and only then decides.

Only odd-id builders ever go. The launchers enforce it as well, so a builder
that is not one is never thrown even if it is standing on the pickup tile.

A `no_spawn` list names core-ring tiles the ordinary spawn chooser must not use.
The opener's own SPAWNs ignore it -- they name their tile outright -- so a tile
can be scripted and barred at the same time, which is exactly the case it exists
for: a builder that walks out through a gap and then seals it behind itself
leaves a tile that was fine to spawn on once and is a trap forever after.

A `sentinels` section scripts the turrets the builders put down, since a turret
cannot be told anything by the builder that built it:

    sentinels {(x, y) of the sentinel: [target tile, target tile, ...]}

Targets are tried in order and the first one holding an enemy building wins, so
the interesting tile goes first and the enemy core goes last. Every target except
the last also requires a friendly builder standing orthogonally next to it: those
are the shots meant to LAND WITH a builder's own hit, and a sentinel that fires
early wastes the 10 ammo and leaves the target alive on 2 HP. The last target is
the fallback and fires whenever it can.

Nothing is bound to an absolute turn number. Each actor holds its own queue and
pops the next op the moment that op is legal, which reproduces the intended
schedule on its own -- the core cannot spawn onto a tile a builder has not
vacated yet, so it simply waits -- and self-corrects instead of desynchronising
when a turn slips.

# Which builder is which

Roles are claimed, not assigned: nothing can assign them, because every unit runs
in its own sub-interpreter and the team store has no free slot. A builder claims
a role on its first turn by where it is standing:

  * at role r's spawn tile  -> claim role r and start at op 0
  * at role r's first WAIT tile, when WAIT is the role's FIRST op -> claim role r

The second case exists because a builder the core spawns and a launcher throws on
the same turn never runs beside the core at all -- its first `run()` already
happens wherever it landed. Restricting mid-program claims to roles that OPEN
with a WAIT is what keeps that unambiguous: a role that starts by building
something can only ever be claimed on its spawn tile, so two roles sharing a
landing tile (yulerune throws A and C to the same one) cannot be confused.

`claim_deadline` closes each role's window a few turns after the only turn it
could legitimately have been claimed on, so an ordinary builder that spawns on a
scripted tile later in the game does not inherit a finished role.

Once committed, a script is never given up on for being slow -- an op that is not
legal yet is simply retried, forever. The one thing that does drop an opener is
the map check in `units/opener.py`, which re-reads the board against
`mapdata.py`'s terrain every single turn and reverts everything to base bot logic
the moment a tile disagrees. That is not impatience, it is the script having been
addressed to a different map.

A launcher self-destructs once its last scripted throw is made: it has no further
use, it holds a slot against the 50-unit cap, and every launcher standing adds
10% to what the next one costs.

A builder that finishes its ops does not go back to the economy: it stays and
guards what it built, out of enemy turret fire, healing and rebuilding it for the
rest of the game -- unless its program ends in an ASSAULT, which never ends.
Scripted builds ignore every titanium reserve; only the core's spawns are gated,
by the barrier buffer described in `units/opener.py`.
"""

# --- op codes ---------------------------------------------------------------
SPAWN = "spawn"
LAUNCH = "launch"
BUILD = "build"
GOTO = "goto"
WAIT = "wait"
STRIKE = "strike"
ASSAULT = "assault"
SKIP_UNLESS_ENEMY = "skip_unless_enemy"

# --- the arguments that are rules rather than tiles -------------------------
RELAY = "relay"           # WAIT: until the launchers are finished with us
NEAR_CORE = "near_core"   # LAUNCH: the tile in range nearest the enemy core
SAME = "same"             # LAUNCH: wherever this launcher's last rule landed
FLANK = "flank"           # LAUNCH: (FLANK, tile) -- that tile, or its side

# There is deliberately no stall timeout. An op that is not legal yet is retried
# for as long as it takes, because the reason is almost always "not yet" rather
# than "never": the tile is occupied this turn, the action cooldown has not come
# round, or -- the case that actually bites -- the titanium is not there. A
# sentinel shares its cost scale with builder bots, so by the time a builder has
# walked across a 30x30 map it can cost 85 Ti rather than 30, and a script that
# gave up after forty turns of being poor threw away the rest of its program for
# a shortage that a few more turns of income would have cleared.
#
# The map check in `units/opener.py` is a different thing and still fires: that
# one is not "this is taking a while", it is "this is not the map we thought it
# was", and there is nothing to wait for.
#
# How late a role may still be claimed. The core spawns at most one builder a
# turn and works down `core_script` in order, so role r cannot be spawned before
# turn r and its builder cannot first run before turn r+1; the slack on top
# covers a script that stalls a turn or two waiting for a tile to clear.
#
# This bound is what stops an ordinary mid-game builder that happens to spawn on
# a scripted tile from inheriting a role that is already finished -- which is not
# hypothetical: yulerune spawns role 3 on (4,11), an ordinary economic spawn tile
# the core reuses within a dozen turns, and without the bound that builder claims
# role 3, waits for a throw that will never come, and idles until it times out.
CLAIM_SLACK = 4

# Hiring ceiling on a scripted map, and the bank that lifts it.
#
# The core's own crew gate counts builders within r^2=36 of itself, so a crew
# that has dispersed reads as a small crew and it keeps hiring -- which is how
# drakkarfjord ran to the 50-unit cap with thousands banked while royale, whose
# crew stays home behind its seal, never did. These count every unit alive.
#
# Under BUILDER_CAP_RELEASE the crew holds at BUILDER_SOFT_CAP and the titanium
# goes to the bank, which is what every one of these plans is actually waiting
# on. Over it, the brakes come off entirely -- the soft cap lifts and the
# per-map `builder_buffer` stops applying.
BUILDER_SOFT_CAP = 20
BUILDER_CAP_RELEASE = 1200


def claim_deadline(spec, role):
    return role + 1 + spec.get("claim_slack", CLAIM_SLACK)


OPENERS = {
    # ------------------------------------------------------------------------
    # valkyrie 30x30, mirror symmetry in x. A big diamond of wall rings the
    # centre with exactly four one-tile doors -- (8,10), (21,10), (8,19), (21,19)
    # -- and the only ways around the outside are the open strips at the top
    # (rows 0-2) and bottom (rows 27-29), each of which can be plugged at x=13
    # against the wall that starts at (14,3) / (14,26).
    #
    # So: two launchers by our core throw four builders out past the doors, and
    # the four of them shut all six openings. A and B take a door on our side
    # and a door on theirs; C and D plug the top and bottom strips.
    #
    #        A: door (8,10) behind it, then door (21,10) on their side
    #        B: door (8,19) behind it, then door (21,19) on their side
    #        C: top strip    (13,2), (13,1), (12,0)
    #        D: bottom strip (13,27), (13,28), (12,29)
    #
    # C and D step the last barrier one tile west rather than walling straight to
    # the edge: standing at (12,1) they can reach both (13,1) and (12,0) without
    # moving, and (13,0) is then a dead pocket that only opens eastward.
    #
    # MEASURED, AND CURRENTLY LOSING. Against an identical build with openers
    # off, 6 seeds x both sides: 0-12. The seal works exactly as drawn -- the
    # enemy core really is cut off -- and it is still the wrong trade here. Two
    # things the checker names: only 5 of 16 ore tiles stay reachable from our
    # core, because the diamond we shut holds the contested ore; and roles 0 and
    # 1 finish INSIDE the diamond, so two of the four opening builders can never
    # come home. Fixing both (a variant where A and B build only the far
    # barriers, 11/16 ore and nobody stranded) still went 0-12, so the cost is
    # the plan itself, not the tiles: ~200 Ti of builders and launchers spend
    # twenty turns walking a 30x30 map and nothing is defending when the
    # opponent arrives. Losses land between t184 and t295.
    #
    # Kept live deliberately, as a base to iterate from. Re-measure with
    # `tools/benchmark_bots.py --bots bots/Tyr_v1 <twin-with-OPENERS-emptied>
    # --maps valkyrie --seeds 1 2 3 4 5 6` before concluding a change helped.
    # ------------------------------------------------------------------------
    "valkyrie": {
        "size": (30, 30),
        "core": (2, 14),          # our core's 2x2 top-left corner, as authored
        "sym": "h",
        # The two ring tiles on the inward side. Role E spawns on (4,14), walks
        # out east through (7,13) and then barriers it shut behind itself, which
        # turns that whole side into a 10-tile pocket with no ore in it and no
        # way back to the other 280 tiles our core can reach. Measured, both
        # tiles: region 10, ore 0. Any ordinary builder dropped there afterwards
        # is gone for the rest of the game, so the ordinary spawn chooser is not
        # allowed to use them -- E's own scripted spawn still can.
        "no_spawn": [(4, 14), (4, 15)],
        # Once the bank passes 1000 the economy has done its job, and a third of
        # the workforce goes over the top of each seal instead of banking more.
        # A volunteer walks to the pickup tile, builds the launcher beside it if
        # nobody has yet, then stands there and is thrown clean across.
        #
        #   id%3==0 -> (10,28), launcher (11,28), over to (16,28)
        #   id%3==1 -> (10,1),  launcher (11,1),  over to (16,1)
        #   id%3==2 -> stays home
        #
        # Straight across, same row, landing three tiles beyond the barrier at
        # x=13 and well clear of the wall stubs at (14,26) and (14,3). Both
        # throws are d^2=25 against the cap of 26, so they are as far over as the
        # launcher can manage.
        #
        # The pickup tiles are one west of each launcher and -- checked against
        # every cardinal neighbour of every seal barrier -- are not tiles the
        # scripted guards ever stand on. That is what keeps D and C from being
        # picked up off the barriers they are there to hold; a launcher throws
        # whatever is on its pickup square and cannot ask who it is.
        "ferry_after": {"titanium": 1000},
        "ferry": {
            (11, 28): ((10, 28), (16, 28), (3, 0)),
            (11, 1): ((10, 1), (16, 1), (3, 1)),
        },
        "ferry_build": [(11, 28), (11, 1)],
        "core_script": [
            (SPAWN, (3, 13)),     # A
            (SPAWN, (3, 16)),     # B
            (SPAWN, (3, 13)),     # C, once A has been thrown clear
            (SPAWN, (3, 16)),     # D, once B has been thrown clear
            (SPAWN, (4, 14)),     # E, the near line; walks, never thrown
        ],
        "launchers": {
            (3, 12): [            # built by A
                (LAUNCH, (3, 13), (7, 10)),   # A out to the north door lane
                (LAUNCH, (3, 13), (7, 9)),    # C out toward the top strip
            ],
            (3, 17): [            # built by B
                (LAUNCH, (3, 16), (7, 19)),   # B out to the south door lane
                (LAUNCH, (3, 16), (7, 20)),   # D out toward the bottom strip
            ],
        },
        # No sentinels here any more. One shares its cost scale with builder
        # bots, so by the time a builder has walked this far it is ~85 Ti rather
        # than 30, and A can take the doorway with a 3 Ti barrier instead. The
        # `sentinels` machinery is still in the framework for a map where the
        # trade works out.
        "builders": [
            [   # role 0 -- A: seals two doors, then goes for the enemy core.
                (BUILD, "launcher", (3, 12)),
                (WAIT, (7, 10)),
                (GOTO, (9, 10)),
                (BUILD, "barrier", (8, 10)),
                (GOTO, (20, 10)),
                (BUILD, "barrier", (21, 10)),
                # (23,12) is the one tile whose southeast ray covers BOTH the
                # (24,13) doorway and the enemy core at (26,15): the ray runs
                # (24,13) (25,14) (26,15), all inside r^2=32. From (23,13) or
                # (22,13) the same facing misses the core entirely.
                # A is the only one that goes in. (24,13) and (24,15) are the two
                # tiles either side of (24,14), and taking both walls the west
                # approach to the enemy core off from the corridor A came up.
                (GOTO, (24, 14)),
                (STRIKE, (24, 13), "barrier"),
                (BUILD, "barrier", (24, 15)),
            ],
            [   # role 1 -- B: the same two doors on the south side, and then it
                # stays home and guards them. Only A pushes on.
                (BUILD, "launcher", (3, 17)),
                (WAIT, (7, 19)),
                (GOTO, (9, 19)),
                (BUILD, "barrier", (8, 19)),
                (GOTO, (20, 19)),
                (BUILD, "barrier", (21, 19)),
            ],
            [   # role 2 -- C
                (WAIT, (7, 9)),
                (GOTO, (12, 2)),
                (BUILD, "barrier", (13, 2)),
                (GOTO, (12, 1)),
                (BUILD, "barrier", (13, 1)),
                (BUILD, "barrier", (12, 0)),
            ],
            [   # role 3 -- D
                (WAIT, (7, 20)),
                (GOTO, (12, 27)),
                (BUILD, "barrier", (13, 27)),
                (GOTO, (12, 28)),
                (BUILD, "barrier", (13, 28)),
                (BUILD, "barrier", (12, 29)),
            ],
            [   # role 4 -- E, the near line east of our own core ring.
                # Walls already close this corridor at (7,12) and (7,17), and
                # (6,14)/(6,15) close its west side; four barriers join them into
                # one unbroken run, stepping diagonally so each tile touches the
                # next. E finishes on (9,15), on the far side of what it built.
                (GOTO, (7, 14)),
                (BUILD, "barrier", (7, 13)),
                (BUILD, "barrier", (8, 14)),
                (GOTO, (7, 15)),
                (BUILD, "barrier", (7, 16)),
                (GOTO, (9, 15)),
                (BUILD, "barrier", (8, 15)),
            ],
        ],
    },

    # ------------------------------------------------------------------------
    # yulerune 20x20, 180-degree symmetry. Two diagonal wall runs come off the
    # central pillar toward each corner and stop short of the edge: the north-east
    # one is (10,7) (11,6) (12,5) (13,4) (14,3), and then nothing until row 0.
    # Three barriers continue that diagonal to the top edge -- (13,2) (12,1)
    # (11,0) -- and a diagonal seals as hard as a straight one, because a builder
    # can only step cardinally and every step down the staircase costs it a step
    # sideways it cannot take.
    #
    # The south-east run gets the same treatment. Both are on the ENEMY half of
    # the diagonal, so our own north-west and south-west lanes stay open: we can
    # still swing around the outside and they cannot.
    #
    #        A: (13,2), then (12,1) from the same tile
    #        C: (11,0), closing the staircase against the map edge
    #        B: (13,17), then (12,18)
    #        D: (11,19)
    #
    # A and C are thrown to the SAME tile, (7,3), one after the other -- A has
    # walked on by the time C lands. Roles stay unambiguous because A's program
    # opens with a build and so can only be claimed beside the core; see the
    # role-claiming note at the top of this file.
    # ------------------------------------------------------------------------
    "yulerune": {
        "size": (20, 20),
        "core": (2, 9),
        "sym": "r",
        "core_script": [
            (SPAWN, (4, 8)),      # A
            (SPAWN, (4, 11)),     # B
            (SPAWN, (4, 8)),      # C
            (SPAWN, (4, 11)),     # D
        ],
        "launchers": {
            (4, 7): [             # built by A
                (LAUNCH, (4, 8), (7, 3)),
                (LAUNCH, (4, 8), (7, 3)),
            ],
            (4, 12): [            # built by B
                (LAUNCH, (4, 11), (7, 16)),
                (LAUNCH, (4, 11), (7, 16)),
            ],
        },
        "builders": [
            [   # role 0 -- A
                (BUILD, "launcher", (4, 7)),
                (WAIT, (7, 3)),
                (GOTO, (12, 2)),
                (BUILD, "barrier", (13, 2)),
                (BUILD, "barrier", (12, 1)),
            ],
            [   # role 1 -- B
                (BUILD, "launcher", (4, 12)),
                (WAIT, (7, 16)),
                (GOTO, (12, 17)),
                (BUILD, "barrier", (13, 17)),
                (BUILD, "barrier", (12, 18)),
            ],
            [   # role 2 -- C
                (WAIT, (7, 3)),
                (GOTO, (11, 1)),
                (BUILD, "barrier", (11, 0)),
            ],
            [   # role 3 -- D
                (WAIT, (7, 16)),
                (GOTO, (11, 18)),
                (BUILD, "barrier", (11, 19)),
            ],
        ],
    },

    # ------------------------------------------------------------------------
    # auroraveil 20x20, mirror symmetry in y -- note this one is authored from
    # the SOUTH core, (9,17), and the table mirrors it for the north.
    #
    # Rows 9 and 10 are a corridor running the full width of the map, holding the
    # only four central ore tiles at (9,9) (10,9) (9,10) (10,10). Rows 8 and 11
    # wall it off except for two four-wide gaps each: x=4..7 and x=12..15. Row 11's
    # gaps are ours, row 8's are the enemy's.
    #
    # So both builders go up into the corridor and shut row 8 behind them. Three
    # barriers fill x=5..7 of a gap and a fourth steps DOWN to (4,9) rather than
    # taking (4,8): from (5,9) a builder reaches both (5,8) and (4,9) without
    # moving, and (4,8) is then a pocket whose only open neighbour is (4,7), on
    # the enemy's side. Same trick as yulerune's staircase, one row lower.
    #
    #        A: (7,8) (6,8) (5,8) (4,9)    -- the west gap
    #        B: (12,8) (13,8) (14,8) (15,9) -- the east gap, mirrored in x
    #
    # Both throws are exactly at the launcher's limit: (9,15) to (8,10) and to
    # (10,10) are both d^2 = 26, and the cap is 26.
    # ------------------------------------------------------------------------
    "auroraveil": {
        "size": (20, 20),
        "core": (9, 17),
        "sym": "v",
        "core_script": [
            (SPAWN, (9, 16)),     # A
            (SPAWN, (9, 16)),     # B, once A has been thrown clear
        ],
        "launchers": {
            (9, 15): [            # built by A
                (LAUNCH, (9, 16), (8, 10)),
                (LAUNCH, (9, 16), (10, 10)),
            ],
        },
        "builders": [
            [   # role 0 -- A, west
                (BUILD, "launcher", (9, 15)),
                (WAIT, (8, 10)),
                (GOTO, (7, 9)),
                (BUILD, "barrier", (7, 8)),
                (GOTO, (6, 9)),
                (BUILD, "barrier", (6, 8)),
                (GOTO, (5, 9)),
                (BUILD, "barrier", (5, 8)),
                (BUILD, "barrier", (4, 9)),
            ],
            [   # role 1 -- B, east: A mirrored about x=9.5
                (WAIT, (10, 10)),
                (GOTO, (12, 9)),
                (BUILD, "barrier", (12, 8)),
                (GOTO, (13, 9)),
                (BUILD, "barrier", (13, 8)),
                (GOTO, (14, 9)),
                (BUILD, "barrier", (14, 8)),
                (BUILD, "barrier", (15, 9)),
            ],
        ],
    },

    # ------------------------------------------------------------------------
    # royale 20x20, mirror symmetry in y. Rows 8-11 are a solid wall band across
    # the whole map with exactly two ways through it: the corridors at x=4..5 and
    # x=14..15. Nothing else connects the two halves at all.
    #
    # Four barriers shut both, at row 9 -- the NORTHERN mouth, theirs. That cuts
    # the map cleanly in half and keeps the corridor tiles on our side of the
    # line, so the two builders end up guarding from inside our own half rather
    # than stranded beyond the wall they just built.
    #
    #        A: (5,9) from (5,10), then (4,9) from (4,10)
    #        B: (14,9) from (14,10), then (15,9) from (15,10)
    #
    # Which is why `keep_launchers` is set here and nowhere else: once the seal is
    # up, the two launchers are the only way anything of ours crosses it. They
    # stay, and they go on ferrying builders forward to (5,10) and (14,10) for
    # the rest of the game -- through our own wall, onto the enemy's half, where
    # ordinary bot logic takes over and goes after their supply and their core.
    #
    # The ferry pickup tiles (8,13) and (11,13) sit one tile outside the core's
    # spawn ring on purpose. A launcher next to the core is also next to half
    # that ring, and ferrying "anything adjacent" would throw every newly spawned
    # economic builder across the map the turn it appeared.
    # ------------------------------------------------------------------------
    "royale": {
        "size": (20, 20),
        "core": (9, 16),
        "sym": "v",
        "keep_launchers": True,
        "ferry_after": "ore_worked",
        # Half the crew crosses, half holds the seal. Split on the comms slot
        # rather than the entity id -- see `opener.split_matches`. On the id this
        # read 3 of 12 builders from one seat and 9 of 15 from the other, and the
        # seat that got 3 lost every game.
        "ferry_who": (2, 1),
        # Our half's ore: two 2x2 clusters, in the corners either side of the
        # core. The seal at row 11 makes this exactly the ore we can ever reach,
        # so "the economy is finished" means these eight tiles are worked.
        "ore": [(2, 16), (3, 16), (2, 17), (3, 17),
                (16, 16), (17, 16), (16, 17), (17, 17)],
        "core_script": [
            (SPAWN, (8, 15)),     # A
            (SPAWN, (11, 15)),    # B
        ],
        "launchers": {
            (8, 14): [(LAUNCH, (8, 15), (5, 10))],      # built by A
            (11, 14): [(LAUNCH, (11, 15), (14, 10))],   # built by B
        },
        # A CHAIN, not a single hop, and it goes through the middle.
        #
        # Rows 9-10 hold three sections nothing can walk into at all: x=1..2,
        # x=7..12 and x=17..18 are ringed by wall on every side. The big middle
        # one is the staging post -- a launcher standing in there is unreachable
        # by any enemy on foot, and reaches the enemy half in one throw.
        #
        # The first version threw straight from home to (5,10) and (14,10) and
        # mostly did not fire: those are the exact tiles roles 0 and 1 stand on
        # to guard the barriers they just built, and a launch needs its landing
        # tile empty. Routing through the middle sidesteps our own guards.
        #
        #   home (8,14)  picks up (8,13)  -> (7,10)   in the middle room
        #   there, build the chain launcher (8,10) if it is not up yet
        #   chain (8,10) picks up (7,10)  -> (8,5)    the enemy half
        #
        # The landing tile of hop one IS the pickup tile of hop two, so a builder
        # arrives already standing where the next launcher will collect it.
        "ferry": {
            (8, 14): ((8, 13), (7, 10)),
            (11, 14): ((11, 13), (12, 10)),
            (8, 10): ((7, 10), (8, 5)),
            (11, 10): ((12, 10), (11, 5)),
        },
        # Launchers a ferried builder puts up itself, if they are not there yet.
        "ferry_build": [(8, 10), (11, 10)],
        "builders": [
            [   # role 0 -- A, the west corridor
                (BUILD, "launcher", (8, 14)),
                (WAIT, (5, 10)),
                (BUILD, "barrier", (5, 9)),
                (GOTO, (4, 10)),
                (BUILD, "barrier", (4, 9)),
            ],
            [   # role 1 -- B, the east corridor
                (BUILD, "launcher", (11, 14)),
                (WAIT, (14, 10)),
                (BUILD, "barrier", (14, 9)),
                (GOTO, (15, 10)),
                (BUILD, "barrier", (15, 9)),
            ],
        ],
    },

    # ------------------------------------------------------------------------
    # drakkarfjord 30x30, 180-degree symmetry. One launcher by the core throws
    # six builders out along the diagonal, and each plugs one hole in the broken
    # wall line that runs corner to corner between the two bases.
    #
    # Six of the eight barriers are single-tile gaps with wall directly above and
    # below -- (9,7) (6,2) (13,11) (13,18) (17,23) (20,27) -- so one barrier each
    # closes them outright.
    #
    # The other two are a pair and only work together. (13,14) has wall above but
    # open floor below; (14,15) has open floor on all four sides. Placed together
    # they make a diagonal chain
    #
    #        wall (13,13) -> (13,14) -> (14,15) -> wall (15,16)
    #
    # and diagonal contact is enough, because a builder can only step cardinally:
    # crossing between (13,15) and (14,14) needs a diagonal move that does not
    # exist. A and D place one each, so neither seals anything until both have.
    #
    # A and B are both thrown to (7,18), one after the other -- A has walked on
    # by the time B lands. That stays unambiguous because A's program opens with
    # a build, so A can only ever be claimed beside the core.
    # ------------------------------------------------------------------------
    "drakkarfjord": {
        "size": (30, 30),
        "core": (2, 24),
        "sym": "r",
        # Past 1000 titanium every odd-id builder stops banking and goes over the
        # wall. The destination is not a fixed tile here: unlike valkyrie's two
        # strips there is no single doorway to aim at, so the launcher picks the
        # tile nearest the enemy core out of everything it can reach that lies on
        # their side of the map. From (12,15) that is sixteen tiles to choose
        # from, all of them across the seal, the nearest one 170 away from their
        # core against the launcher's own 25.
        # Two mines we deliberately leave alone. Working them drags builders out
        # to the far corners for ore we do not need, and the bank is what this
        # plan actually spends.
        "avoid_ore": [(18, 26), (2, 2)],
        # And hire more slowly than the default 150, for the same reason: at the
        # normal rate the bank never reached the 1000 the crossing waits on,
        # because every spare 150 went straight back into another builder.
        "builder_buffer": 250,
        "ferry_after": {"titanium": 1000},
        "ferry": {
            # Every odd id, plus half the even ones: id%4 in {0,1,3}. The
            # remaining quarter stays home on the seal and the harvesters.
            (12, 15): ((11, 15), "enemy_side", (4, (0, 1, 3))),
        },
        "ferry_build": [(12, 15)],
        "core_script": [
            (SPAWN, (4, 23)),     # A
            (SPAWN, (4, 23)),     # B
            (SPAWN, (4, 23)),     # C
            (SPAWN, (4, 23)),     # D
            (SPAWN, (4, 23)),     # E
            (SPAWN, (4, 23)),     # F
        ],
        "launchers": {
            (4, 22): [            # built by A, then throws the other five out
                (LAUNCH, (4, 23), (7, 18)),   # A
                (LAUNCH, (4, 23), (8, 19)),   # D, second out
                (LAUNCH, (4, 23), (7, 18)),   # B, once A has moved on
                (LAUNCH, (4, 23), (5, 17)),   # C
                (LAUNCH, (4, 23), (9, 23)),   # E
                (LAUNCH, (4, 23), (7, 26)),   # F
            ],
        },
        "builders": [
            [   # role 0 -- A: the lower hole first, then up the column
                (BUILD, "launcher", (4, 22)),
                (WAIT, (7, 18)),
                (GOTO, (12, 14)),
                (BUILD, "barrier", (13, 14)),
                (GOTO, (12, 12)),
                (BUILD, "barrier", (12, 11)),
            ],
            [   # role 1 -- D. (13,18) turned out to be redundant: the line closes
                # without it, so it is one barrier and one turn saved.
                (WAIT, (8, 19)),
                (GOTO, (14, 18)),
                (BUILD, "barrier", (15, 18)),
                (GOTO, (14, 16)),
                (BUILD, "barrier", (14, 15)),
            ],
            [   # role 2 -- B: the far northern hole
                (WAIT, (7, 18)),
                (GOTO, (7, 7)),
                (BUILD, "barrier", (8, 7)),
            ],
            [   # role 3 -- C: the corner hole
                (WAIT, (5, 17)),
                (GOTO, (4, 2)),
                (BUILD, "barrier", (5, 2)),
            ],
            [   # role 4 -- E
                (WAIT, (9, 23)),
                (GOTO, (16, 23)),
                (BUILD, "barrier", (17, 23)),
            ],
            [   # role 5 -- F
                (WAIT, (7, 26)),
                (GOTO, (19, 27)),
                (BUILD, "barrier", (20, 27)),
            ],
        ],
    },

    # ------------------------------------------------------------------------
    # glacierkeep 30x30, mirror symmetry in y -- authored from the SOUTH core,
    # (14,26), and mirrored for the north.
    #
    # Not a seal. The two halves are joined by everything outside the central
    # pillars, so there is nothing to shut; what there IS, is a clear column at
    # x=14 running the whole height of the map between the two pillar gaps at
    # (14,11)/(15,11) and (14,18)/(15,18). So this one goes up it.
    #
    # A RELAY: A is thrown, builds a launcher beside where it lands, is thrown
    # again from that one, and so on until it is standing next to their core.
    #
    #        (14,25) -> (15,19) -> (14,13) -> (14,7) -> (13,4)
    #             26        26        25         5
    #
    # The first two hops are exactly d^2=26, the launcher's limit, and the tiles
    # they land on are forced by it: from (15,19) the ONLY orthogonal neighbour
    # that can still reach (14,13) is (15,18) -- (14,19) is 36, (16,19) is 40,
    # (15,20) is 50. B follows one hop behind through the same four launchers and
    # finishes on the far side of their core.
    #
    # The last two throws are rules, not tiles. (14,7) is where NEAR_CORE lands
    # on an empty board -- it is the unique nearest tile to their core inside 26
    # of (14,12) -- but it is resolved live so that a builder or a building in
    # the way costs a tile rather than the plan, and SAME then sends B to
    # whichever tile A actually got. (13,4) and (16,4) are the two corners of
    # their core's ring, which is exactly where a conveyor is likely to be, so
    # those are FLANK: the named tile if it is free, else the nearest tile to
    # their core still on that side of it -- measured, (13,2) and (16,2).
    #
    # Of the four candidate tiles for the last launcher, (14,6) is the one that
    # keeps both flanks in range and even: it reaches 10 tiles west of their core
    # and 8 east, against (13,7)'s 8/3 and (14,8)'s 4/2.
    #
    # Then A and B take all twelve tiles of their core's spawn ring between them
    # -- ten barriers plus the two corners they landed on -- which is a box, not
    # a wall: at CORE_SPAWNING_RADIUS_SQ=2 those twelve tiles are every tile the
    # enemy core may spawn a builder onto, so a complete ring is a core that
    # cannot replace anything it loses.
    #
    #        A: (14,4) (13,3) (13,4) (13,2) (13,1) (14,1), from x=12 and row 0
    #        B: (15,4) (16,3) (16,4) (16,2) (16,1) (15,1), from x=17 and row 0
    #
    # Each works the column from OUTSIDE it, because a builder that barriers the
    # tile it is standing next to has to be on the far side of the line it is
    # drawing or it walls itself in. Both land on a corner, take the two tiles
    # they can reach from it, step out, and barrier the corner behind them.
    #
    # STRIKE rather than BUILD throughout: by turn 10 the ring may well be
    # holding enemy conveyors, and "own this tile" is the instruction -- whether
    # that needs a fight first is not knowable from here.
    #
    # MEASURED against Champion_v54: A lands beside their core on t8 and B on t9,
    # the ring closes on t45, and their core mines ZERO titanium for the rest of
    # the game -- final 15780 to 0, 327 buildings to 4, core destroyed on t782.
    # The FLANK rule earns its keep on the way: (16,4) was occupied when the
    # throw came, and B went to (16,3) instead without the script noticing.
    # ------------------------------------------------------------------------
    "glacierkeep": {
        "size": (30, 30),
        "core": (14, 26),
        "sym": "v",
        # Never trade hits for a ring tile here. Unlike a lone doorway, every one
        # of these is next to the enemy core, so a defender is always beside it.
        "strike_stands_off": True,
        "core_script": [
            (SPAWN, (14, 25)),    # A
            (SPAWN, (14, 25)),    # B, once A has been thrown clear
            (SPAWN, (16, 27)),    # C, the economy
        ],
        # And nothing else until C has finished. Hiring a crew before there is a
        # harvester to pay for it is what buried this map: the rush leaves and
        # the core spends the bank on builders that have no route to work on.
        "spawn_gate": (26, 27),
        "launchers": {
            (14, 24): [           # built by A, beside our own core
                (LAUNCH, (14, 25), (15, 19)),     # A
                (LAUNCH, (14, 25), (15, 19)),     # B, once A has moved on
            ],
            (15, 18): [           # built by A where it landed, in the gap
                (LAUNCH, (15, 19), (14, 13)),     # A
                (LAUNCH, (15, 19), (14, 13)),     # B
            ],
            (14, 12): [           # built by A in the far gap
                (LAUNCH, (14, 13), NEAR_CORE),    # A, as far up as 26 reaches
                (LAUNCH, (14, 13), SAME),         # B, to the tile A got
            ],
            (14, 6): [            # built by A three tiles short of their core
                (LAUNCH, (14, 7), (FLANK, (13, 4))),   # A, the west corner
                (LAUNCH, (14, 7), (FLANK, (16, 4))),   # B, the east one
            ],
        },
        "builders": [
            [   # role 0 -- A: builds the whole relay, then the west half of the
                # ring. Every WAIT is RELAY rather than a tile: A is thrown four
                # times and two of the four destinations are rules.
                (BUILD, "launcher", (14, 24)),
                (WAIT, RELAY),
                (BUILD, "launcher", (15, 18)),
                (WAIT, RELAY),
                (BUILD, "launcher", (14, 12)),
                (WAIT, RELAY),
                (BUILD, "launcher", (14, 6)),
                (GOTO, (14, 7)),      # back on the pickup tile, if the build moved us
                (WAIT, RELAY),
                (GOTO, (13, 4)),
                (STRIKE, (14, 4), "barrier"),
                (STRIKE, (13, 3), "barrier"),
                (GOTO, (12, 4)),
                (STRIKE, (13, 4), "barrier"),   # the corner it landed on
                (GOTO, (12, 2)),
                (STRIKE, (13, 2), "barrier"),
                (GOTO, (12, 1)),
                (STRIKE, (13, 1), "barrier"),
                (GOTO, (14, 0)),
                (STRIKE, (14, 1), "barrier"),
            ],
            [   # role 1 -- B: A's east half, mirrored about x=14.5. Rides the
                # relay from end to end without building anything, so one RELAY
                # covers all three throws.
                (WAIT, (15, 19)),     # the claim tile: B may be spawned and
                (WAIT, RELAY),        # thrown in one round and first run here
                (STRIKE, (15, 4), "barrier"),
                (STRIKE, (16, 3), "barrier"),
                (GOTO, (17, 4)),
                (STRIKE, (16, 4), "barrier"),
                (GOTO, (17, 2)),
                (STRIKE, (16, 2), "barrier"),
                (GOTO, (17, 1)),
                (STRIKE, (16, 1), "barrier"),
                (GOTO, (15, 0)),
                (STRIKE, (15, 1), "barrier"),
            ],
            [   # role 2 -- the economy, and for a while the whole of it.
                # Walks east along row 27 laying the line behind itself, one
                # conveyor per tile vacated, all facing west into the core at
                # (15,27). The harvester goes down LAST, on (26,27): built
                # first it would spend the whole walk producing stacks with
                # nowhere to put them.
                (GOTO, (17, 27)),
                (BUILD, "conveyor", (16, 27), "west"),
                (GOTO, (18, 27)),
                (BUILD, "conveyor", (17, 27), "west"),
                (GOTO, (19, 27)),
                (BUILD, "conveyor", (18, 27), "west"),
                (GOTO, (20, 27)),
                (BUILD, "conveyor", (19, 27), "west"),
                (GOTO, (21, 27)),
                (BUILD, "conveyor", (20, 27), "west"),
                (GOTO, (22, 27)),
                (BUILD, "conveyor", (21, 27), "west"),
                (GOTO, (23, 27)),
                (BUILD, "conveyor", (22, 27), "west"),
                (GOTO, (24, 27)),
                (BUILD, "conveyor", (23, 27), "west"),
                (GOTO, (25, 27)),
                (BUILD, "conveyor", (24, 27), "west"),
                (GOTO, (26, 27)),
                (BUILD, "conveyor", (25, 27), "west"),
                (GOTO, (27, 27)),
                (BUILD, "harvester", (26, 27)),
            ],
        ],
    },
}


# --- identification ---------------------------------------------------------
# Keyed on (width, height, our core's top-left corner) for both sides of every
# opener, so a unit picks its script with one dict lookup on turn 0 and learns at
# the same time whether it has to mirror. Terrain is never consulted: an opener
# is checked op by op as it runs (every op is gated on a can_*()), so a wrong
# guess costs the turns it takes to notice, not a corrupted world model.

def _flip(pos, sym, w, h):
    x, y = pos
    if sym == "h":
        return (w - 1 - x, y)
    if sym == "v":
        return (x, h - 1 - y)
    if sym == "r":
        return (w - 1 - x, h - 1 - y)
    return pos


# A mirrored script needs its facings mirrored too, and there is no helper for
# that anywhere in the bot -- `Direction.opposite()` is only the 180-degree case.
_FLIP_H = {"north": "north", "south": "south", "east": "west", "west": "east",
           "northeast": "northwest", "northwest": "northeast",
           "southeast": "southwest", "southwest": "southeast"}
_FLIP_V = {"north": "south", "south": "north", "east": "east", "west": "west",
           "northeast": "southeast", "southeast": "northeast",
           "northwest": "southwest", "southwest": "northwest"}


def _flip_dir(name, sym):
    if sym == "h":
        return _FLIP_H[name]
    if sym == "v":
        return _FLIP_V[name]
    if sym == "r":
        return _FLIP_H[_FLIP_V[name]]
    return name


def _flip_core(pos, sym, w, h):
    """Mirror a 2x2 core's top-left corner, which steps back one on each flipped
    axis -- the same correction map_info.{hor,ver,rot}_flip_core makes."""
    x, y = pos
    if sym == "h":
        return (w - 2 - x, y)
    if sym == "v":
        return (x, h - 2 - y)
    if sym == "r":
        return (w - 2 - x, h - 2 - y)
    return pos


def _build_index():
    index = {}
    for name, spec in OPENERS.items():
        w, h = spec["size"]
        core = spec["core"]
        index[(w, h, core)] = (name, False)
        mirrored = _flip_core(core, spec["sym"], w, h)
        if mirrored != core:
            index[(w, h, mirrored)] = (name, True)
    return index


INDEX = _build_index()


def lookup(width, height, my_core):
    """(name, spec, mirror) for this board, or None.

    `mirror` is True when the coordinates in `spec` were authored for the other
    side and every tile has to go through `mirror_pos` first.
    """
    hit = INDEX.get((width, height, (my_core[0], my_core[1])))
    if hit is None:
        return None
    name, mirror = hit
    return name, OPENERS[name], mirror


def mirror_pos(pos, spec):
    w, h = spec["size"]
    return _flip(pos, spec["sym"], w, h)


def mirror_dir(name, spec):
    return _flip_dir(name, spec["sym"])


def barrier_total(spec):
    """How many barriers the whole opener owes, across every role.

    This is what the titanium reserve is sized against: the opener must never
    spend itself out of being able to finish the seal it started.
    """
    return sum(1
               for role in spec["builders"]
               for op in role
               if op[0] == BUILD and op[1] == "barrier")
