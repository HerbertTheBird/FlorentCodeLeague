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
                                  Two refinements, both about not wasting
                                  titanium. While an enemy builder stands next to
                                  the target it can heal 4 HP for 1 Ti against
                                  our 2 damage for 2 Ti, so hitting it alone is
                                  worse than not hitting it: hold, and let the
                                  scripted sentinel's 18 arrive to make up a
                                  one-shot. With nobody there to heal it, chip
                                  away alone and it dies on its own schedule.
                                  And once the tile is OURS, the program ends
                                  there -- the builder holds and guards what it
                                  just built rather than running whatever came
                                  after.
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


# A/B TWIN: identical to bots/Tyr_v1 except the opener table is empty.
OPENERS = {}

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
