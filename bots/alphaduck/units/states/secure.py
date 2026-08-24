"""Builder state: secure the core ring.

Once we KNOW the enemy fields more than one builder (more than one distinct enemy
id seen over the whole game), fill the tiles cardinally adjacent to our core with
conveyors that output straight INTO the core -- a protective belt-ring around it.
The bodies block a raider from stepping onto a core-adjacent tile and, if ever fed,
deliver straight into the core.

Ranks with chase (5.9). Targets are Voronoi-partitioned across our builders like every
other state so several builders each take a slice of the ring. A rush-mode builder
never secures -- it stays committed to the enemy core.
"""
import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import Controller, Position, Direction
from log import log

rc: Controller = None
nav: Pathing = None

MAX_SCORE = 5.9
_cached_target = None    # (Position pos, Direction facing) to build this turn

# Cardinal (delta -> Direction), in a fixed order for determinism.
_CARDINALS = (
    (0, -1, Direction.NORTH),
    (0, 1, Direction.SOUTH),
    (1, 0, Direction.EAST),
    (-1, 0, Direction.WEST),
)


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _secure_tiles():
    """(candidate_mask, {tile_n: Direction}) for every buildable tile cardinally
    adjacent to our core, each mapped to the direction that outputs INTO the core cell
    it touches. Buildable = in bounds, not a wall, and nothing already built there."""
    core = map_info._bm_my_core_area
    if not core:
        return 0, {}
    w, h = map_info._width, map_info._height
    # Ring = cardinal neighbours of the core, minus the core cells themselves, minus
    # anything already built (walls / our own belt already placed here / enemy stuff).
    ring = (map_info.manhattan(core) & ~core & map_info._board_mask
            & ~map_info._bm_env[map_info._IDX_ENV_WALL]
            & ~map_info._bm_any_building)
    facings = {}
    mask = 0
    m = ring
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        x, y = n % w, n // w
        for dx, dy, d in _CARDINALS:
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and (core >> (nx + ny * w)) & 1:
                facings[n] = d                    # output into the adjacent core cell
                mask |= b
                break
    return mask, facings


def score(can_move=True):
    global _cached_target
    _cached_target = None
    if not can_move:
        return 0                                  # pure build+move, nothing in place
    # A rush-mode builder never peels back to secure the core.
    if units.builder.in_rush_mode():
        return 0
    # Only once we KNOW the enemy has more than one builder (more than one distinct id
    # seen all game) -- a lone rusher isn't a reason to wall in our own core.
    if len(map_info._enemy_ids_ever) <= 1:
        return 0
    cand_mask, facings = _secure_tiles()
    if not cand_mask:
        return 0
    need = rc.get_conveyor_cost() + map_info.ti_reserve()
    if rc.get_global_resources() < need:
        return 0                                  # can't afford the belt this turn
    w = map_info._width
    my_pos = map_info._my_pos
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    # Voronoi-partition the ring tiles across our builders, same tie rule as everything.
    claimed = pathing.claim_subset(my_mask, map_info._bm_friendly_bots, cand_mask, tie_self=True)
    if not claimed:
        return 0
    tgt, _ = nav.closest(claimed)                 # nearest reachable ring tile
    if tgt is None:
        return 0
    facing = facings.get(tgt.x + tgt.y * w)
    if facing is None:
        return 0
    _cached_target = (tgt, facing)
    return MAX_SCORE


def run(can_move=True):
    if _cached_target is None:
        return
    pos, facing = _cached_target
    log("SECURE", pos, facing)
    # Move beside the tile first (bfs_move keeps us put when already adjacent+safe, and
    # steps us off a lethal tile instead of building on it and dying).
    if nav.move_adjacent(pos, can_move=can_move):
        return
    need = rc.get_conveyor_cost() + map_info.ti_reserve()
    if rc.get_global_resources() >= need and rc.can_build_conveyor(pos, facing):
        rc.build_conveyor(pos, facing)
        map_info.update_at(pos)
