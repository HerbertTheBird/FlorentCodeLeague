from collections import deque

import map_info
from pathing import Pathing
import units.builder
import units.econ_states.harvest as harvest
from fcode import *
import random
from log import log

rc: Controller = None
nav: Pathing = None

explore_target = None
_explore_target_from_initial = False
target = None  # current destination, for status logging


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav

MAX_SCORE = 1
def score():
    return 1


def _closest_core_ore_target():
    """The open cardinal-adjacent tile of the unharvested ore tile nearest our
    core. Ore that has no reachable open neighbour is skipped so we never pin the
    target to something we could never stand beside. Returns None when no
    unharvested ore is known (caller falls back to discovery exploration)."""
    core = map_info._my_core
    if core is None:
        return None
    # Only ore we can actually route to and stand beside (harvestable_ore already
    # drops harvested, landlocked, enemy/friendly-blocked, and known-unroutable
    # tiles) — otherwise an econ bot fixates on a landlocked ore it can never
    # build on. When none is reachable, return None so the caller falls back to
    # discovery exploration to uncover more ore.
    unharvested = harvest.harvestable_ore()
    if not unharvested:
        return None
    w = map_info._width
    ores = []
    m = unharvested
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        ox, oy = n % w, n // w
        # squared distance to the nearest tile of our 2x2 core
        cx = min(max(ox, core.x), core.x + 1)
        cy = min(max(oy, core.y), core.y + 1)
        ores.append(((ox - cx) ** 2 + (oy - cy) ** 2, ox, oy))
    ores.sort(key=lambda t: t[0])
    for _d, ox, oy in ores:
        adj = units.builder._nearest_open(
            (ox + dx, oy + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        )
        if adj is not None:
            return adj
    return None

def _atk_explore_targets():
    """The ring one tile out from the (symmetry-predicted) enemy core's 2x2
    footprint (leaving a one-tile gap) — the Chebyshev-2 ring. Returns the set of
    open such tiles, or every in-bounds ring tile if none are open, or None when
    the enemy core isn't known yet."""
    core = map_info.atk_symmetry_target(units.builder._atk_index)
    if core is None:
        return None
    open_tiles = set()
    ring_tiles = set()
    for x in range(core.x - 2, core.x + 4):
        for y in range(core.y - 2, core.y + 4):
            # skip the footprint AND its immediately-adjacent ring so only the
            # one-out ring (with a gap to the core) remains
            if core.x - 1 <= x <= core.x + 2 and core.y - 1 <= y <= core.y + 2:
                continue
            if not map_info.in_bounds(Position(x, y)):
                continue
            ring_tiles.add(Position(x, y))
            if units.builder._tile_open(x, y):
                open_tiles.add(Position(x, y))
    return open_tiles or ring_tiles or None


def generate_explore_target():
    global explore_target
    w = map_info._width
    nlc = map_info._not_left_col
    nrc = map_info._not_right_col
    board = (1 << (w * map_info._height)) - 1
    if units.builder._atk_bot:
        # Attack bot: head for the ring of tiles adjacent to the enemy core;
        # nav paths to the nearest reachable one.
        ring = _atk_explore_targets()
        explore_target = ring if ring else Position(w // 2, map_info._height // 2)
        return
    avoid = map_info.get_avoid(False, False, False)
    # (Titan: Cambridge's poor-mode clause avoided seen-empty tiles because they
    # needed a road to walk on; empty tiles are free to walk now, so it's gone.)
    passable = ~avoid & board

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
        steps = abs(bx - tx) + abs(by - ty)  # cardinal movement steps
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
    frontier = recent_frontiers[0]
    count = frontier.bit_count()
    if count == 0:
        explore_target = Position(random.randint(0, map_info._width - 1),
                                  random.randint(0, map_info._height - 1))
        return
    pick = random.randint(0, count - 1)
    mask = frontier
    for _ in range(pick):
        mask &= mask - 1
    lsb = mask & -mask
    n = lsb.bit_length() - 1
    explore_target = Position(n % w, n // w)


def run():
    global explore_target, _explore_target_from_initial, target
    log("EXPLORE")

    if units.builder._economy_builder:
        # Econ bots always head straight for the open tile beside the unharvested
        # ore nearest our core, so an econ bot that can't harvest/route right now
        # walks to claim the closest ore rather than wandering. Only when no
        # unharvested ore is known do we fall through to discovery exploration.
        econ_dest = _closest_core_ore_target()
        if econ_dest is not None:
            explore_target = econ_dest
            _explore_target_from_initial = False
            target = explore_target
            nav.move_to(explore_target)
            return

    if units.builder._atk_bot:
        # Attackers head for the enemy core's adjacent ring (a set of tiles);
        # nav paths to the nearest reachable one. Skip the single-Position
        # distance / regeneration logic below, which can't take a target set.
        generate_explore_target()
        target = explore_target
        if explore_target is not None:
            nav.move_to(explore_target)
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
    target = explore_target
    attempts = 0
    while attempts < 1:
        if not nav.move_to(explore_target):
            generate_explore_target()
        else:
            break
        attempts += 1
