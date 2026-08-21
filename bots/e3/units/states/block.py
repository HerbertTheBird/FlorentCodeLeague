import map_info
from fcode import *
from log import log

rc: Controller = None


def init(c: Controller):
    global rc
    rc = c


# ----------------------------------------------------------------------------
# Block: drop a barrier on a tile that chokes the enemy economy, without moving.
# A tile is a choke iff it is either
#   (a) the OUTPUT target of an enemy conveyor -- wall off where its titanium is
#       trying to flow, or
#   (b) cardinally adjacent to BOTH an enemy harvester and an enemy conveyor -- a
#       harvester->belt junction.
# can_build_barrier() then requires the tile be empty and orthogonally adjacent to
# us, so occupied targets (the belt continuing, a building) and out-of-reach tiles
# fall away on their own. This state NEVER moves: it only fires when such a tile is
# already one step away.
# ----------------------------------------------------------------------------
MAX_SCORE = 10
_cached_target = None


def _block_tiles() -> int:
    """Bitmask of tiles where a barrier chokes the enemy economy (see module note)."""
    enemy = map_info._bm_team[1 - map_info._my_team_idx]
    enemy_conv = map_info._bm_et[map_info._IDX_CONVEYOR] & enemy
    enemy_harv = map_info._bm_et[map_info._IDX_HARVESTER] & enemy
    conv_targets = map_info._conveyor_target_tiles(enemy_conv)
    junctions = (map_info.expand_manhattan(enemy_harv)
                 & map_info.expand_manhattan(enemy_conv))
    return conv_targets | junctions


def score(can_move=True):
    """Valid (MAX_SCORE) iff we can place a choke barrier on a cardinally-adjacent
    tile right now. This state never moves, so its validity is purely about our
    current position -- `can_move` doesn't change it."""
    global _cached_target
    _cached_target = None
    # The barrier's cost must clear the defender reserve, or we can't place it.
    if rc.get_global_resources() < rc.get_barrier_cost():
        return 0
    cand = _block_tiles()
    if not cand:
        return 0
    w, h = map_info._width, map_info._height
    my = map_info._my_pos
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        x, y = my.x + dx, my.y + dy
        if not (0 <= x < w and 0 <= y < h):
            continue
        if not ((cand >> (x + y * w)) & 1):
            continue
        p = Position(x, y)
        if rc.can_build_barrier(p):          # empty, orthogonally adjacent, affordable
            _cached_target = p
            return MAX_SCORE
    return 0


def run(can_move=True):
    """Place the barrier on the chosen adjacent choke tile. Never moves."""
    p = _cached_target
    if p is None:
        return
    log("BLOCK")
    if (rc.can_build_barrier(p)
            and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.ti_reserve()):
        rc.build_barrier(p)
        map_info.update_at(p)
