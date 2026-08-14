from collections import deque

import map_info
from pathing import Pathing
import units.builder
from fcode import *
import random
from log import log

rc: Controller = None
nav: Pathing = None

explore_target = None
_explore_target_from_initial = False


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav

MAX_SCORE = 1
def score():
    return 1

# How many targets we're willing to burn on a failed move in one turn.
MOVE_ATTEMPTS = 1

# Walk to the pick-th set bit a chunk at a time; clearing one bit at a time costs
# a full-width bigint op per step on a 900-tile board.
_CHUNK = 64
_CHUNK_MASK = (1 << _CHUNK) - 1


def _random_tile(mask: int):
    """Uniformly pick one set bit of `mask` and return it as a Position, or None
    if `mask` is empty."""
    count = mask.bit_count()
    if count == 0:
        return None
    pick = random.randint(0, count - 1)
    base = 0
    while True:
        chunk = mask & _CHUNK_MASK
        c = chunk.bit_count()
        if pick < c:
            break
        pick -= c
        mask >>= _CHUNK
        base += _CHUNK
    for _ in range(pick):
        chunk &= chunk - 1
    n = base + (chunk & -chunk).bit_length() - 1
    w = map_info._width
    return Position(n % w, n // w)


def generate_explore_target():
    global explore_target
    w = map_info._width
    nlc = map_info._not_left_col
    nrc = map_info._not_right_col
    avoid = map_info.get_avoid(False)
    if units.builder._stay_near_core:
        near = units.builder.near_core_mask()
        # Prefer reachable tiles near the core, but settle for any of them.
        pos = _random_tile(near & ~avoid or near)
        if pos is not None:
            explore_target = pos
            return
    passable = ~avoid & map_info._board_mask

    # Seed with all other builders' claimed tiles + incremental steps from
    # the nearest friendly bot toward each claim, plus my own position.
    seeds = 0
    my_pos = map_info._my_pos
    my_n = my_pos.x + my_pos.y * w
    seeds |= 1 << my_n
    seeds |= map_info._bm_friendly_bots

    # Seed tiles every 5 Chebyshev steps from my position toward each claim.
    bx, by = my_pos.x, my_pos.y
    mask = seeds
    while mask:
        lsb = mask & -mask
        n = lsb.bit_length() - 1
        tx, ty = n % w, n // w
        steps = max(abs(bx - tx), abs(by - ty))
        for s in range(5, steps, 5):
            ix = bx + (tx - bx) * s // steps
            iy = by + (ty - by) * s // steps
            seeds |= 1 << (ix + iy * w)
        mask ^= lsb

    # Keep the trailing 6 frontiers so we can recover the ring at iteration (c-5) once the fill terminates.
    visited = seeds
    frontier = seeds
    recent_frontiers = deque([seeds], maxlen=6)
    c = 0
    while frontier and c < 100:
        h = frontier | ((frontier & nrc) << 1) | ((frontier & nlc) >> 1)
        expanded = h | (h << w) | (h >> w)
        frontier = expanded & passable & ~visited
        visited |= frontier
        c += 1
        recent_frontiers.append(frontier)
    explore_target = _random_tile(recent_frontiers[0])
    if explore_target is None:
        explore_target = Position(random.randint(0, w - 1),
                                  random.randint(0, map_info._height - 1))


def run():
    global explore_target, _explore_target_from_initial
    log("EXPLORE")
    

    if units.builder._initial_explore_target is not None:
        if map_info._my_pos.distance_squared(units.builder._initial_explore_target) <= 18:
            units.builder._initial_explore_target = None
        else:
            explore_target = units.builder._initial_explore_target
            _explore_target_from_initial = True
    elif _explore_target_from_initial:
        # initial target was cleared externally (e.g. timeout); don't trust the stale copy
        explore_target = None
        _explore_target_from_initial = False
    if explore_target is None or map_info._my_pos.distance_squared(explore_target) <= 18:
        generate_explore_target()
        _explore_target_from_initial = False
    attempts = 0
    while attempts < MOVE_ATTEMPTS:
        if not nav.move_to(explore_target):
            generate_explore_target()
        else:
            break
        attempts += 1
