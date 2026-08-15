"""Builder state: put up one launcher next to our core to catapult builders out.

The map-wide core rush (`cut`, score 13) is the highest-value thing most of our
builders do, and almost all of its cost is walking. Instrumented against loki:
the enemy core's eight-tile delivery ring is untouched at round 25 and only
fully ours around round 125 -- a hundred rounds, nearly all of it travel, on
maps where the two cores are twenty-odd tiles apart.

A launcher shortcuts that. `launch` picks up a builder within PICKUP_R2 and
throws it anywhere within THROW_R2 (26, so five tiles), it costs no titanium and
no ammo, it fires once a round forever, and -- the part that matters here -- it
ignores walls and line of sight entirely. It is a teleport. One 20 Ti launcher
sited on the enemy-facing side of our core gives *every* builder we ever spawn a
five-tile head start toward their ring, and on a walled map it can be worth far
more than five tiles of walking.

KNOWN LIMIT: on wide maps this still occasionally builds two. Three independent
guards were tried -- a remembered-bitmap count, a claim_subset partition, and a
live get_nearby_buildings check -- and all three leak, because a builder sees
only radius^2 20 (~4.5 tiles) and two builders on opposite faces of the core
each derive a different ferry tile from a different remembered map and never see
each other. Measured: 2 launchers on eider and archipelago, 1 everywhere else.
The cost of the leak is bounded and small (40 Ti and +20 scale rather than 20
and +10), so it is left in the open rather than papered over with comms traffic;
tighten it only if the idea earns its place first.

Deliberately one launcher, near home, and only on maps big enough for the trip
to dominate. Two would double the +10 scale for a second throw we have no
builders queued for, and on a small map the walk is short enough that 20 Ti and
a builder-turn buys nothing.

The throw itself lives in units/turret_launcher.py (`_try_ferry_ally_forward`);
this state only gets the launcher built.
"""
import map_info
import pathing
import units.builder
import units.defense as defense
from fcode import Controller, EntityType, Position
from log import log
from pathing import Pathing

rc: Controller = None
nav: Pathing = None

# Below this the cores are close enough that walking is not the bottleneck, and
# a 20 Ti launcher plus the builder-turn to place it is pure loss. 400 tiles is
# a 20x20 board; the maps we measured the slow seal on (saga 24x24, quarry,
# nordkap) are all above it, and the fast ones (fjordgate 10x10, sprint,
# string, duel) are all below.
MIN_MAP_TILES = 400
# Past this the trip no longer decides anything -- the ring is either ours or
# contested, and 20 Ti is better spent elsewhere.
LATEST_ROUND = 250
# Sits above route (5) so the single build actually happens promptly instead of
# losing every contested turn for a hundred rounds. It can only fire once, and
# only while the gates below hold, so it cannot become a turn sink.
MAX_SCORE = 6

_cached_tile: Position | None = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _want_ferry() -> bool:
    if rc.get_current_round() > LATEST_ROUND:
        return False
    if map_info._width * map_info._height < MIN_MAP_TILES:
        return False
    if map_info._their_core is None:
        return False        # nothing to aim the throw at yet
    if map_info._my_core is None:
        return False
    # One is the whole design. my_count reads this unit's remembered bitmaps, so
    # a builder that has wandered off may not see the launcher we already have;
    # that is acceptable here because the build itself is gated on
    # can_build_launcher and on standing next to our own core.
    return defense.my_count(map_info._IDX_LAUNCHER) == 0


def _ferry_tile() -> Position | None:
    """A buildable tile beside our core, on the side facing the enemy core.

    Enemy-facing so the five-tile throw spends all of itself on progress rather
    than undoing the offset, and beside our core so freshly spawned builders are
    already inside PICKUP_R2 without walking anywhere first.
    """
    core = map_info._my_core
    their = map_info._their_core
    if core is None or their is None:
        return None
    w = map_info._width
    ring = map_info.expand_manhattan(map_info._bm_my_core_area, 2)
    ring &= ~map_info._bm_my_core_area
    ring &= map_info._board_mask & ~map_info._bm_any_building
    ring &= ~map_info._bm_env[map_info._IDX_ENV_WALL]
    ring &= ~map_info._bm_env[map_info._IDX_ENV_ORE_TI]     # do not squat on ore
    if not ring:
        return None
    best, best_key = None, None
    for pos in map_info.iter_mask(ring):
        # Toward the enemy, and reachable: prefer the tile that most reduces the
        # distance to their core.
        key = (pos.distance_squared(their), pos.x + pos.y * w)
        if best_key is None or key < best_key:
            best, best_key = pos, key
    return best


def score():
    global _cached_tile
    _cached_tile = None
    if not _want_ferry():
        return 0
    if rc.get_global_resources() < rc.get_launcher_cost() + map_info.ti_reserve():
        return 0
    tile = _ferry_tile()
    if tile is None:
        return 0
    # Exactly one builder may go for it. my_count() reads this unit's OWN
    # remembered bitmaps, so on a wide core two builders both see zero launchers
    # in the same round and both build one -- measured, 2 launchers on eider and
    # archipelago. claim_subset is the same Voronoi partition the economy states
    # use and hands the tile to exactly one of us without any comms traffic.
    w = map_info._width
    my_bit = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    tile_bit = 1 << (tile.x + tile.y * w)
    if not pathing.claim_subset(my_bit, map_info._bm_friendly_bots, tile_bit, tie_self=True):
        return 0
    _cached_tile = tile
    return MAX_SCORE



def _launcher_already_up() -> bool:
    """True if a friendly launcher is standing right now, by live query."""
    my_team = map_info._my_team
    for bid in rc.get_nearby_buildings():
        if rc.get_team(bid) == my_team and rc.get_entity_type(bid) == EntityType.LAUNCHER:
            return True
    return False


def run():
    log("FERRY")
    tile = _cached_tile
    if tile is None:
        return
    my_pos = map_info._my_pos
    if abs(tile.x - my_pos.x) + abs(tile.y - my_pos.y) == 1:
        # Last check, live rather than remembered. claim_subset only partitions
        # over builders THIS unit can see, so two builders on opposite faces of
        # the core each claim the tile and each build -- measured, 2 launchers on
        # eider and archipelago. Units act in ascending entity id, so by the time
        # the second one runs the first launcher already exists and
        # get_nearby_buildings (live vision, not our remembered bitmaps) sees it.
        if _launcher_already_up():
            return
        if rc.can_build_launcher(tile) and \
                rc.get_global_resources() >= rc.get_launcher_cost() + map_info.ti_reserve():
            log(f"FERRY: launcher at {tile} pointing at {map_info._their_core}")
            rc.build_launcher(tile)
            map_info.update_at(tile)
        return
    nav.move_adjacent(tile)
