"""Builder state: secure the core's feed ring.

Whenever a conveyor already points into our core, fill the EMPTY tiles around it
that could also feed the core with more conveyors. This packs the core's feed
ring so titanium keeps flowing in even if one feed line is cut.

We only bother securing feed lines that are believed to be carrying titanium
(map_info._bm_ti_carrying) -- an idle core-facer has no flow to protect. The
candidate ring is every tile at Chebyshev distance 1 of the 2x2 core (the
12-tile ring around it), EMPTY (no building, no ore, no wall):
  * a CARDINAL ring tile (manhattan-adjacent to a core cell) outputs straight
    into the core;
  * a DIAGONAL ring tile (corner) can't face a core cell, so it outputs into
    whichever of its two core-adjacent neighbours already holds a *carrying*
    core-facing conveyor -- and if both do, the one carrying LESS load (feed the
    emptier lane).

A spot is a valid target iff it is such an empty ring tile AND either:
  * it is adjacent to a conveyor of ours that faces into the core AND is
    believed to be carrying (pack the live feed ring -- this is also exactly
    what makes a diagonal tile have a neighbour to point into), OR
  * (cardinal tiles only) it is contested -- an enemy builder is within BFS
    CONTEST_RANGE of it and a friendly bot can reach it no later than that
    enemy, so we plant a feed conveyor and take the spot before they do.

Ranks just below attack and above everything else: once a builder is beside the
core with a feed to complete, sealing that feed beats any economy work, but a
real combat need (attack) still preempts it. Work is Voronoi-partitioned across
builders exactly like every other state.
"""
from main import has_op
import map_info
import pathing
from pathing import Pathing
from fcode import Controller, Direction, Position
import units.builder
from log import log

# An enemy builder within this BFS distance of a core feed tile is "rushing" it.
CONTEST_RANGE = 5

rc: Controller = None
nav: Pathing = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _core_facing_conveyors() -> int:
    """My conveyors whose output tile is a core tile (they feed the core)."""
    my = map_info._bm_team[map_info._my_team_idx]
    my_convs = map_info._bm_conveyors & my
    if not my_convs:
        return 0
    reverse = map_info._conv_reverse
    facers = 0
    m = map_info._bm_my_core_area
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        if n < len(reverse):
            facers |= reverse[n]
    return facers & my_convs


def _carrying_core_facers() -> int:
    """Core-facing conveyors believed to be carrying titanium -- the only feed
    lines worth securing. An idle core-facer has no flow to protect, so packing
    redundancy beside it (branch A) is wasted; only carrying lanes anchor a
    secure target."""
    return _core_facing_conveyors() & map_info._bm_ti_carrying


def _core_ward_dir(pos):
    """The cardinal direction from `pos` into its adjacent core tile, or None if
    `pos` doesn't touch the core. A feed tile touches exactly one core cell."""
    core = map_info._bm_my_core_area
    w, h = map_info._width, map_info._height
    for d in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
        nb = pos.add(d)
        if 0 <= nb.x < w and 0 <= nb.y < h and (core >> (nb.x + nb.y * w)) & 1:
            return d
    return None


def _feed_dir(pos):
    """Direction a secure conveyor on `pos` should face.

    Cardinal ring tile -> straight into the core. Diagonal (corner) ring tile
    can't face a core cell, so it points into whichever of its core-adjacent
    neighbours already holds a core-facing conveyor; if both do, the one with
    the least load. Returns None if no valid output exists."""
    d = _core_ward_dir(pos)
    if d is not None:
        return d
    facers = _carrying_core_facers()
    if not facers:
        return None
    w, h = map_info._width, map_info._height
    best_dir = None
    best_load = None
    for dr in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
        nb = pos.add(dr)
        if not (0 <= nb.x < w and 0 <= nb.y < h):
            continue
        n = nb.x + nb.y * w
        if not ((facers >> n) & 1):
            continue
        load = map_info.conv_load[n] if n < len(map_info.conv_load) else 0.0
        if best_load is None or load < best_load:
            best_load = load
            best_dir = dr
    return best_dir


def _my_claims() -> int:
    core = map_info._bm_my_core_area
    if not core:
        return 0
    w = map_info._width
    my_pos = map_info._my_pos
    # The full Chebyshev-1 ring around the core (cardinal feed tiles + diagonal
    # corners). Cardinal tiles can face the core directly; corners feed a
    # core-facing neighbour instead (see _feed_dir).
    ring = map_info.expand_chebyshev(core) & ~core
    cardinal = map_info.manhattan(core) & ~core
    # EMPTY: seen, and no building / wall / ore.
    valid = (map_info._bm_seen
             & ~map_info._bm_any_building
             & ~map_info._bm_env[map_info._IDX_ENV_WALL]
             & ~map_info._bm_env[map_info._IDX_ENV_ORE_TI])
    empty_feed = valid & ring
    if not empty_feed:
        return 0

    # (A) Adjacent to a conveyor that already feeds the core -> pack the ring.
    # For a cardinal tile this is the classic "beside a core-facer"; for a
    # diagonal corner it is exactly the condition that gives _feed_dir a
    # core-adjacent neighbour to point into.
    facers = _carrying_core_facers()
    candidates = (empty_feed & map_info.manhattan(facers)) if facers else 0

    # (B) Contested: an enemy builder is rushing this feed tile (BFS <= 5) and a
    # friendly bot (nearest, INCLUDING me) can reach it no later than the enemy,
    # so we can plant a feed conveyor and deny the spot before they take it.
    # Restricted to cardinal tiles -- only they can feed the core directly, which
    # is the point of racing for the spot.
    # Nearest-friendly (not strictly my own distance) keeps the candidate mask
    # shared across builders so the Voronoi claim stays consistent -- whoever the
    # claim hands the tile to is the friendly bot that can win the race.
    # Cheap Manhattan pre-filter (a superset of BFS<=5), then confirm with BFS.
    enemy_bots = map_info._bm_enemy_bots
    if enemy_bots:
        rush = (empty_feed & cardinal & ~candidates
                & map_info.expand_manhattan(enemy_bots, CONTEST_RANGE))
        if rush:
            all_friendly = (1 << (my_pos.x + my_pos.y * w)) | map_info._bm_friendly_bots
            while rush:
                b = rush & -rush
                rush ^= b
                n = b.bit_length() - 1
                tpos = Position(n % w, n // w)
                _, edist = nav.closest(enemy_bots, pos=tpos)
                if 0 <= edist <= CONTEST_RANGE:
                    _, fdist = nav.closest(all_friendly, pos=tpos)
                    if 0 <= fdist <= edist:      # we get there first (or tie)
                        candidates |= b

    if units.builder._stay_near_core:
        candidates &= units.builder.near_core_mask()
    if not candidates:
        return 0
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, candidates, tie_self=True)


# Just below attack (9), above every other state (route_repair is 8).
MAX_SCORE = 8.5
_cached_target = None       # (candidate Position, core-ward direction) or None


def score():
    global _cached_target
    _cached_target = None
    claims = _my_claims()
    if not claims:
        return 0
    candidate, _ = nav.closest(claims)      # nearest reachable feed tile
    if candidate is None:
        return 0
    d = _feed_dir(candidate)
    if d is None:
        return 0
    _cached_target = (candidate, d)
    return MAX_SCORE


def run():
    if _cached_target is None:
        return
    log("SECURE")
    candidate, d = _cached_target
    my_pos = map_info._my_pos
    adjacent = abs(candidate.x - my_pos.x) + abs(candidate.y - my_pos.y) == 1
    if adjacent:
        if (rc.can_build_conveyor(candidate, d)
                and rc.get_global_resources() >= rc.get_conveyor_cost() + map_info.ti_reserve()):
            log(f"SECURE: feed conveyor at {candidate} facing {d}")
            rc.build_conveyor(candidate, d)
            map_info.update_at(candidate)
        return
    nav.move_adjacent(candidate)
