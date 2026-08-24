"""Builder state: patrol the friendly conveyor network (our dedicated defence bot).

Only the 4th-spawned builder (units.builder.is_patrol_builder) runs this. It walks
our own conveyor belts from the core outward, picking a random branch each trip and
following it up-tree (toward the leaf conveyors / harvesters) until it reaches a
leaf, then turns around and heads home to start another random branch. Builders
can't attack, so the value is passive: while it walks a belt it sits beside our
conveyors/harvesters and the free per-turn _do_best_heal() repairs whatever a raider
is chipping, and its body contests the ground. Ported from the "patrol bot" idea in
Blue-Dragon's albiorix bot.

Ranks as the patrol bot's DEFAULT movement -- just above explore (1). Every real
defence (a heal target under attack, the core alarm, chasing a raider in our harvest
zone) outscores it, so patrol only runs when there is nothing more urgent to do.
"""
import random
import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import Controller, Position
from log import log

rc: Controller = None
nav: Pathing = None

# albiorix's SCOUT_DISTANCE / near-core gate are SQUARED (euclidean) distances.
SCOUT_DSQ = 4          # advance the frontier once we're within this of it
NEAR_CORE_DSQ = 8      # this close to the core counts as "home" -> pick a new branch

# Persistent per-unit state (each builder runs its own module instance): the belt
# tile we're currently walking toward, and the tile indices visited this trip (so a
# cyclic belt is detected and abandoned rather than looped forever).
_top: Position | None = None
_path: list[int] = []

MAX_SCORE = 1.5


def init(c: Controller):
    global rc, nav, _top, _path
    rc = c
    nav = units.builder.nav
    _top = None
    _path = []


def _dsq(a: Position, b: Position) -> int:
    dx, dy = a.x - b.x, a.y - b.y
    return dx * dx + dy * dy


def _nearest_core(pos: Position):
    """The tile of our 2x2 core nearest `pos`, or None if we have no core."""
    core = map_info._my_core
    if core is None:
        return None
    cx = core.x if pos.x < core.x else (core.x + 1 if pos.x > core.x + 1 else pos.x)
    cy = core.y if pos.y < core.y else (core.y + 1 if pos.y > core.y + 1 else pos.y)
    return Position(cx, cy)


def _feeders(tile_n: int):
    """Friendly conveyors whose output flows INTO tile `tile_n` -- the up-tree
    neighbours of that tile on our belt graph. `_conv_reverse[n]` is the both-teams
    set of conveyor buildings outputting to n; mask it to our team."""
    convs = map_info._conv_reverse[tile_n] & map_info._bm_team[map_info._my_team_idx]
    out = []
    w = map_info._width
    while convs:
        b = convs & -convs
        convs ^= b
        n = b.bit_length() - 1
        out.append(Position(n % w, n // w))
    return out


def _core_feeders():
    """Conveyors feeding a tile cardinally adjacent to our core -- the roots of every
    branch leaving the core."""
    core_adj = map_info.manhattan(map_info._bm_my_core_area)
    out = []
    m = core_adj
    while m:
        b = m & -m
        m ^= b
        out.extend(_feeders(b.bit_length() - 1))
    return out


def _has_belts() -> bool:
    return bool(map_info._bm_et[map_info._IDX_CONVEYOR] & map_info._bm_team[map_info._my_team_idx])


def score(can_move=True):
    if not can_move:
        return 0                                  # pure movement, nothing in place
    if not units.builder.is_patrol_builder():
        return 0
    # Need either an active branch to keep walking or some belt to start one on.
    if _top is None and not _has_belts():
        return 0
    return MAX_SCORE


def run(can_move=True):
    global _top, _path
    if not can_move or not units.builder.is_patrol_builder():
        return
    log("PATROL", _top)
    w = map_info._width
    my = map_info._my_pos
    core = _nearest_core(my)
    if core is None:
        return

    if _top is not None:
        # Still far from the frontier -> just keep walking toward it.
        if _dsq(my, _top) > SCOUT_DSQ:
            nav.move_adjacent(_top, allow_bots=True)
            return
        feeders = _feeders(_top.x + _top.y * w)
        if not feeders:
            # Leaf reached (a belt end / harvester) -> reset and head home.
            _top = None
            _path = []
            nav.move_adjacent(core, allow_bots=True)
            return
        # Push the frontier up-tree while we're still close to it, choosing a random
        # branch at each junction. Abandon a belt that loops back on itself.
        while feeders and _dsq(my, _top) <= SCOUT_DSQ:
            _top = random.choice(feeders)
            tn = _top.x + _top.y * w
            if tn in _path:
                _top = None
                _path = []
                nav.move_adjacent(core, allow_bots=True)
                return
            _path.append(tn)
            feeders = _feeders(tn)
        nav.move_adjacent(_top, allow_bots=True)
        return

    # No active branch. If we're home (at/near the core), start a fresh random branch;
    # otherwise walk home first.
    if _dsq(my, core) <= NEAR_CORE_DSQ:
        cf = _core_feeders()
        if cf:
            _top = random.choice(cf)
            _path = [_top.x + _top.y * w]
            nav.move_adjacent(_top, allow_bots=True)
        return
    nav.move_adjacent(core, allow_bots=True)
