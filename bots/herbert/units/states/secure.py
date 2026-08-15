"""Builder state: secure the core's feed ring.

Fill the empty tiles around our core with conveyors that feed it, so titanium
keeps flowing in even if one feed line is cut.

Every empty tile in the Chebyshev-1 ring of the 2x2 core (the 12-tile ring) is a
valid target ("empty" = seen, with no building, wall, or ore):
  * a CARDINAL ring tile (manhattan-adjacent to a core cell) outputs straight
    into the core;
  * a DIAGONAL ring tile (corner) can't face a core cell, so it outputs into one
    of its two core-adjacent cardinal neighbours -- preferring one that already
    holds a carrying core-facer (least load if both do), else just any
    core-adjacent neighbour (it feeds the core too, or will once secured).
Preference: if any empty ring tile is adjacent to one of our LOADED (carrying)
conveyors, we restrict to those -- back up the live feed lines first and ignore
the idle spots entirely. Only when none are loaded-adjacent do all ring tiles
stay in play.

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
    can't face a core cell, so it points into one of its two core-adjacent
    cardinal neighbours -- preferring one that already holds a carrying
    core-facer (least load if both do), else just any core-adjacent neighbour
    (it feeds the core too, or will once secured). A ring tile always has such a
    neighbour, so this never returns None for a ring candidate."""
    d = _core_ward_dir(pos)
    if d is not None:
        return d
    core = map_info._bm_my_core_area
    cardinal_feed = map_info.manhattan(core) & ~core   # core-adjacent feed tiles
    facers = _carrying_core_facers()
    w, h = map_info._width, map_info._height
    best_dir = None
    best_load = None
    fallback_dir = None
    for dr in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
        nb = pos.add(dr)
        if not (0 <= nb.x < w and 0 <= nb.y < h):
            continue
        n = nb.x + nb.y * w
        if not ((cardinal_feed >> n) & 1):
            continue                                   # not a core-adjacent tile
        fallback_dir = dr
        if (facers >> n) & 1:
            load = map_info.conv_load[n] if n < len(map_info.conv_load) else 0.0
            if best_load is None or load < best_load:
                best_load = load
                best_dir = dr
    return best_dir if best_dir is not None else fallback_dir


def _my_claims() -> int:
    core = map_info._bm_my_core_area
    if not core:
        return 0
    w = map_info._width
    my_pos = map_info._my_pos
    # The full Chebyshev-1 ring around the core (cardinal feed tiles + diagonal
    # corners). EVERY empty ring tile is a valid target, unconditionally --
    # cardinal tiles output straight into the core, corners into a core-adjacent
    # neighbour (see _feed_dir). No packing / carrying / contested gate.
    ring = map_info.expand_chebyshev(core) & ~core
    # EMPTY: seen, and no building / wall / ore.
    valid = (map_info._bm_seen
             & ~map_info._bm_any_building
             & ~map_info._bm_env[map_info._IDX_ENV_WALL]
             & ~map_info._bm_env[map_info._IDX_ENV_ORE_TI])
    candidates = valid & ring
    if not candidates:
        return 0

    # Prefer securing beside a LOADED (carrying) conveyor of ours: if any ring
    # tile is adjacent to one, restrict to those and ignore the idle spots
    # entirely -- back up the live feed lines first.
    loaded = map_info._bm_ti_carrying & map_info._bm_team[map_info._my_team_idx]
    loaded_adj = candidates & map_info.manhattan(loaded)
    if loaded_adj:
        candidates = loaded_adj

    if units.builder._stay_near_core:
        candidates &= units.builder.near_core_mask()
    if not candidates:
        return 0
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, candidates, tie_self=True)


# Just below attack (9), above every other state (route_repair is 8).
MAX_SCORE = 3.75
_cached_target = None       # (candidate Position, core-ward direction) or None


def score(can_move=True):
    global _cached_target
    _cached_target = None
    # Can't afford a feed conveyor (+ the defender reserve) -> don't select the
    # state; run() couldn't build anyway, so it would only hog the builder.
    if rc.get_global_resources() < rc.get_conveyor_cost() + map_info.ti_reserve():
        return 0
    claims = _my_claims()
    if not can_move:
        # In-place retry: only a feed tile we can build on from right here counts.
        claims &= map_info.manhattan(1 << (map_info._my_pos.x + map_info._my_pos.y * map_info._width))
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


def run(can_move=True):
    if _cached_target is None:
        return
    log("SECURE")
    candidate, d = _cached_target
    # Always try to move into position first. bfs_move keeps us put when we're
    # already adjacent and safe, but steps us off our tile if it's now lethal --
    # only build when we didn't need to move.
    if nav.move_adjacent(candidate, can_move=can_move):
        return
    if (rc.can_build_conveyor(candidate, d)
            and rc.get_global_resources() >= rc.get_conveyor_cost() + map_info.ti_reserve()):
        log(f"SECURE: feed conveyor at {candidate} facing {d}")
        rc.build_conveyor(candidate, d)
        map_info.update_at(candidate)
