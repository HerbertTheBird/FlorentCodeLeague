from collections import deque

import map_info
from pathing import Pathing, CARD_DIR
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
def score(can_move=True):
    # Explore is pure movement -- nothing to do in place.
    return 0 if not can_move else 1

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
        # Prefer reachable tiles near the core. Settling for an `avoid` tile (the
        # old `or near`) hands back a wall or a building as a walk target.
        pos = _random_tile(near & ~avoid)
        if pos is not None:
            explore_target = pos
            return
    passable = ~avoid & map_info._board_mask
    my_pos = map_info._my_pos
    my_n = my_pos.x + my_pos.y * w

    # Seed the ring flood from my tile AND every teammate builder, so the frontier is
    # pushed out past the whole crew and we spread over the map rather than clustering.
    seeds = (1 << my_n) | map_info._bm_friendly_bots

    # Keep the trailing 6 frontiers so we can recover the ring at iteration (c-5) once the fill terminates.
    visited = seeds
    frontier = seeds
    recent_frontiers = deque([seeds], maxlen=3)
    c = 0
    while frontier and c < 1000:
        expanded = frontier | ((frontier & nrc) << 1) | ((frontier & nlc) >> 1) | (frontier << w) | (frontier >> w)
        frontier = expanded & passable & ~visited
        visited |= frontier
        c += 1
        recent_frontiers.append(frontier)
    explore_target = _random_tile(recent_frontiers[0])
    if explore_target is None:
        # Fall back to ANY tile the fill reached (still reachable by me) rather
        # than a uniform random board tile, which was frequently a wall or sat in
        # a sealed-off region -- an unreachable walk target.
        explore_target = _random_tile(visited & ~seeds) or my_pos
    for i in range(len(recent_frontiers)):
        units.builder.draw_mask(recent_frontiers[i], 255, (i*32)%256, 0)


def run(can_move=True):
    if not can_move:
        return                      # movement-only state; nothing to do in place
    global explore_target, _explore_target_from_initial
    log("EXPLORE")

    # Rush mode: stop wandering -- march straight at the (predicted) enemy core.
    if units.builder.in_rush_mode():
        tgt = map_info._compute_predicted_enemy_core()
        if tgt is not None:
            explore_target = tgt
            _explore_target_from_initial = False
            nav.move_to(tgt)
            return

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
    moved = False
    while attempts < MOVE_ATTEMPTS:
        if not nav.move_to(explore_target, hard_avoid_turret=True):
            generate_explore_target()
        else:
            moved = True
            break
        attempts += 1
    if not moved:
        _step_anywhere()


def _step_anywhere():
    """Last resort: take ANY safe legal step rather than stand still.

    `pathing.move_to` has a stuck-breaker of its own, but explore could never
    reach it: on a failed move this state generates a NEW target, so the next
    turn's `target_set != self.target_p` and `stuck_turns` is reset to 0. The
    threshold is `2 + id % 8` and was never hit -- the result was builders that
    never moved again (656 failed explore turns to 38 successful ones in one game,
    id=3 pinned to a single tile from round 16 to 999). Stepping somewhere breaks
    the pocket that made every target unreachable; next turn's fill re-seeds from
    the new tile."""
    die = map_info.lethal_mask(rc.get_hp())
    n = map_info._my_pos.x + map_info._my_pos.y * map_info._width
    # Explore hard-avoids turret threat even on this last-resort step -- never stray
    # into fire just to unstick.
    ok = (map_info.passable()
          & ~die
          & ~map_info._bm_enemy_turret_threat
          & ~map_info._bm_friendly_bots
          & ~map_info._bm_enemy_bots
          & ~(1 << n))
    if not ok:
        return
    for d in random.sample(CARD_DIR, len(CARD_DIR)):
        p = map_info.pos_add(map_info._my_pos, d)
        if not map_info.in_bounds(p):
            continue
        if not (ok >> (p.x + p.y * map_info._width)) & 1:
            continue
        if nav.move(d):
            return
