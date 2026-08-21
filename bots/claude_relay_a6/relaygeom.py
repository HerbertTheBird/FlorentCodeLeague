"""Launcher-relay geometry -- a port of Tyr_Jython's `jython.py` with the
hardcoded map tables removed.

The strategy, read off the ladder and documented in `bots/Tyr_Jython/jython.py`:
the first builder a core spawns builds a launcher on the tile beside it, is
thrown ~5 tiles by that launcher on the launcher's first turn, builds the next
launcher where it lands, and repeats. Two turns a hop, about 5.8 tiles a hop --
nearly three tiles a turn against a walking builder's one.

# What had to change, and why

Tyr derives the whole chain from `mapdata.py`: fifteen published maps, every
tile of each, so `their_core`, `is_wall` and `is_ore` are known from round 0 and
every unit computes the SAME chain from the SAME table without exchanging a
byte. CLAUDE.md forbids that -- tournaments run on fresh maps -- so the table is
replaced by live inference:

  * our core            `map_info._my_core`, or the core's own slot-0 broadcast
                        (`comms.core_position()`) for a launcher that has been
                        thrown halfway across the map and has never seen it;
  * the enemy core      our core reflected through the surviving symmetry;
  * walls / ore         `map_info._bm_env`, which already carries symmetry-
                        mirrored terrain and the tiles other units relay.

That has one consequence which drives the whole design of the port. Tyr can
precompute `chain()` once and index into it from either end, because the table
is identical in every interpreter. Live knowledge is NOT identical -- each unit
runs in its own interpreter and has seen different tiles -- so a precomputed
chain would silently disagree between the builder that is walking it and the
launcher that is throwing along it. This module therefore exposes the geometry
one HOP at a time (`best_hop`, `landings`) and both ends recompute their own
step every turn from whatever they currently know. The builder picks the site;
the launcher picks the landing. Neither has to agree with the other about
anything except which way the enemy is.

`their_core()` is the one place where disagreement would still be fatal, so it
is resolved by a fixed priority -- seen, then rotational, then vertical, then
horizontal -- rather than by `map_info._predicted_enemy_core`, whose
hor/ver tiebreak is a function of the READER's own position and so can resolve
two different ways for two units standing one tile apart. Symmetry itself
converges because the core broadcasts it in comms slot 0 and every unit applies
it.
"""

from collections import deque
import os

import map_info

TRACE = os.environ.get("RELAY_TRACE") == "1"


def trace(rc, *a):
    if TRACE:
        print("RELAY", rc.get_current_round(), rc.get_id(), *a)

# GameConstants has LAUNCHER_VISION_RADIUS_SQ = 26 and no separate attack
# radius; Tyr uses 26 for the throw and 2 for the pickup, and every candidate
# this module produces is re-validated with `rc.can_launch` before it is used.
LAUNCHER_RANGE_SQ = 26
PICKUP_RANGE_SQ = 2

# How close to the enemy ring the relay has to get before the builder walks the
# rest. Below this, another launcher costs more than the turns it saves.
ARRIVE_DIST = 6

# Entity ids are handed out globally in spawn order and both cores are created
# before anything else: core = 1 and 2, then the round-1 builders 3 (the team
# whose core is id 1) and 4 (the other). Verified on replay 12297. Only one of
# the pair is ever on our team and every caller checks the team as well, so the
# pair is safe to test as a set.
#
# This is the identity marker the relay needs and cannot get any other way: each
# unit runs in its OWN interpreter, so a module-level "I am the siege builder"
# flag set by the builder is invisible to the launcher that has to throw it.
# Tyr avoids the problem entirely by making both ends derive everything from the
# map table; without the table, something has to name the unit.
RELAY_BOT_IDS = (3, 4)

_CARDINALS = ((0, -1), (1, 0), (0, 1), (-1, 0))

_dist = None
_dist_key = None


def reset() -> None:
    global _dist, _dist_key
    _dist = None
    _dist_key = None


# --- terrain ----------------------------------------------------------------
def in_bounds(p) -> bool:
    return 0 <= p[0] < map_info._width and 0 <= p[1] < map_info._height


def is_wall(p) -> bool:
    n = p[0] + p[1] * map_info._width
    return bool((map_info._bm_env[map_info._IDX_ENV_WALL] >> n) & 1)


def is_ore(p) -> bool:
    n = p[0] + p[1] * map_info._width
    return bool((map_info._bm_env[map_info._IDX_ENV_ORE_TI] >> n) & 1)


def open_tile(p) -> bool:
    """Terrain a builder could stand on -- walls only, exactly as Tyr has it.

    Buildings are deliberately not consulted here. Whether a particular tile is
    occupied on the day is a runtime question, and it is asked where the build
    or the throw is actually made (`can_build_launcher`, `can_launch`); folding
    it in here would make the geometry flicker from turn to turn as the board
    changes under it.

    An unseen tile reads as not-a-wall, which is the right default: it makes the
    relay optimistic about ground it has not looked at, and a throw that turns
    out to be illegal simply is not made.
    """
    return in_bounds(p) and not is_wall(p)


# --- the two cores ----------------------------------------------------------
def my_core():
    c = map_info._my_core
    if c is not None:
        return (c.x, c.y)
    import comms
    p = comms.core_position()
    return None if p is None else (p.x, p.y)


def their_core():
    """Top-left of the enemy 2x2 core, or None.

    Fixed priority, NOT `map_info._predicted_enemy_core` -- see the module
    docstring. Every unit that reads the same symmetry flags gets the same
    answer here regardless of where it is standing.
    """
    c = map_info._their_core
    if c is not None:
        return (c.x, c.y)
    mine = my_core()
    if mine is None:
        return None
    w, h = map_info._width, map_info._height
    if map_info._rot_sym:
        return (w - 2 - mine[0], h - 2 - mine[1])
    if map_info._ver_sym:
        return (mine[0], h - 2 - mine[1])
    if map_info._hor_sym:
        return (w - 2 - mine[0], mine[1])
    return None          # asymmetric map: the relay stands down


def core_tiles(core):
    x, y = core
    return ((x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1))


def ring(core):
    """{tile: 'corner' | 'edge'} for the twelve tiles around a 2x2 core."""
    out = {}
    x, y = core
    for dx in (-1, 0, 1, 2):
        for dy in (-1, 0, 1, 2):
            if 0 <= dx <= 1 and 0 <= dy <= 1:
                continue
            p = (x + dx, y + dy)
            if not open_tile(p):
                continue        # a wall already denies the tile, for free
            out[p] = "corner" if dx in (-1, 2) and dy in (-1, 2) else "edge"
    return out


def _forbidden():
    out = set()
    t = their_core()
    if t is not None:
        out |= set(core_tiles(t))
    m = my_core()
    if m is not None:
        out |= set(core_tiles(m))
    return out


# --- distance to the enemy ring ---------------------------------------------
def dist_field():
    """Cardinal BFS steps from every tile to the enemy core's ring, or None if
    we do not know where the enemy core is.

    Straight-line distance is not good enough to steer a relay: a throw that
    looks like progress can land behind a wall, and the next hop then has
    nowhere to go.

    Cached on (their core, our core, the wall mask). Keying on the wall mask
    rather than `map_info._struct_version` matters -- struct_version bumps on
    every building change anywhere on the board, which during the opening is
    almost every turn, and this BFS is the only expensive thing the relay does.
    """
    global _dist, _dist_key
    theirs = their_core()
    if theirs is None:
        return None
    mine = my_core()
    key = (theirs, mine, map_info._bm_env[map_info._IDX_ENV_WALL])
    if _dist_key == key:
        return _dist
    w, h = map_info._width, map_info._height
    INF = 1 << 20
    d = [INF] * (w * h)
    q = deque()
    blocked = _forbidden()
    for p in ring(theirs):
        if p in blocked:
            continue
        d[p[0] + p[1] * w] = 0
        q.append(p)
    while q:
        cur = q.popleft()
        nd = d[cur[0] + cur[1] * w] + 1
        for dx, dy in _CARDINALS:
            nxt = (cur[0] + dx, cur[1] + dy)
            if not open_tile(nxt) or nxt in blocked:
                continue
            n = nxt[0] + nxt[1] * w
            if nd < d[n]:
                d[n] = nd
                q.append(nxt)
    _dist = d
    _dist_key = key
    return d


def dist_at(p) -> int:
    f = dist_field()
    if f is None or not in_bounds(p):
        return 1 << 20
    return f[p[0] + p[1] * map_info._width]


# --- one hop ----------------------------------------------------------------
def sites(stand):
    """Tiles a builder standing on `stand` could put a launcher on."""
    out = []
    forbidden = _forbidden()
    for dx, dy in _CARDINALS:
        p = (stand[0] + dx, stand[1] + dy)
        if not open_tile(p) or p in forbidden:
            continue
        # An ore tile is a harvester site; spending it on a launcher that exists
        # to be walked past once is the worst trade on the board.
        if is_ore(p):
            continue
        out.append(p)
    return out


def landings(site, cur_dist):
    """Where a launcher on `site` could usefully throw: in range, on open
    ground, and strictly closer to the ring than `cur_dist`.

    Sorted best-first as (ring distance, then FARTHEST from the launcher) --
    among tiles equally close to the target, the longest throw is the one that
    buys the most map for the same two turns.
    """
    out = []
    r = int(LAUNCHER_RANGE_SQ ** 0.5)
    forbidden = _forbidden()
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            d2 = dx * dx + dy * dy
            if d2 > LAUNCHER_RANGE_SQ or d2 == 0:
                continue
            p = (site[0] + dx, site[1] + dy)
            if p in forbidden or not open_tile(p):
                continue
            d = dist_at(p)
            if d >= cur_dist:
                continue
            out.append((d, -d2, p))
    out.sort()
    return out


def fallback_landings(site, bot):
    """Ranked landings when the enemy core is unknown to THIS unit.

    The builder chose which side of itself to put the launcher on, and it chose
    it by looking at the enemy. So the vector from the builder to the launcher
    is the builder's own answer to "which way is the enemy", and a launcher that
    knows nothing else can still throw along it. Ranked by progress in that
    direction, then by throw length.
    """
    dx0 = site[0] - bot[0]
    dy0 = site[1] - bot[1]
    out = []
    r = int(LAUNCHER_RANGE_SQ ** 0.5)
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            d2 = dx * dx + dy * dy
            if d2 > LAUNCHER_RANGE_SQ or d2 == 0:
                continue
            p = (site[0] + dx, site[1] + dy)
            if not open_tile(p):
                continue
            prog = (p[0] - bot[0]) * dx0 + (p[1] - bot[1]) * dy0
            if prog <= 0:
                continue
            out.append((-prog, -d2, p))
    out.sort()
    return out


def best_hop(stand, site_ok=None):
    """(launcher site, landing tile) for the next hop from `stand`, or None.

    This is one iteration of Tyr's `chain()` loop, evaluated live instead of
    precomputed -- see the module docstring for why the chain cannot be
    precomputed here.
    """
    cur = dist_at(stand)
    if cur >= (1 << 20):
        return None
    best = None
    for site in sites(stand):
        if site_ok is not None and not site_ok(site):
            continue
        for d, negd2, landing in landings(site, cur):
            # The third term is the tie-break that makes the CHOICE OF SIDE
            # legible from outside. Every site is one step from the builder and
            # the landing is a throw away from the site, so among sites that
            # reach an equally good landing, the one nearest that landing is the
            # one that points at it. A launcher which has lost track of the
            # enemy core reads exactly that vector as "which way is the enemy"
            # (`fallback_landings`), and before this tie-break existed it could
            # be handed a launcher built on the builder's SOUTH side while the
            # plan was to throw east -- measured on frostgate, and it threw the
            # builder six tiles the wrong way.
            toward = abs(site[0] - landing[0]) + abs(site[1] - landing[1])
            key = (d, negd2, toward, site, landing)
            if best is None or key < best:
                best = key
            break                    # `landings` is sorted; take its best
    if best is None:
        return None
    return (best[3], best[4])
