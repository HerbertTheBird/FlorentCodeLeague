"""TRAP LAUNCHER (econ) — build a launcher near an enemy builder that CANNOT
avoid being launched, so the launcher flings it (and any later enemy builder) into
the killbox centre, where the killbox gunner kills it.

Only fires when the killbox is armed (its gunner is up) and there is a placement
where the enemy is genuinely inescapable — every tile it could move to (or stay
on) is either caught by my gunner fire, in range of an existing launcher, blocked
by an impassible structure/bot, or in pickup range of the launcher we place. See
_find_launcher_tile for the exact test.
"""

import map_info
import units.builder
import units.killbox_plan as killbox_plan
from fcode import Controller, Position, EntityType, Direction
from log import log
from pathing import Pathing

rc: Controller = None
nav: Pathing = None

MAX_SCORE = 11  # defensive: preempts routine economy (like counter_mirror)
_THROW_RANGE_SQ = 26

target: Position | None = None  # launcher tile to build


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _has_mine(pos: Position, et_idx: int) -> bool:
    bit = 1 << (pos.x + pos.y * map_info._width)
    return bool(map_info._bm_et[et_idx] & map_info._bm_team[map_info._my_team_idx] & bit)


def _can_afford_launcher() -> bool:
    return rc.get_global_resources() >= rc.get_launcher_cost()


def _enemy_builders():
    my_team = rc.get_team()
    out = []
    for eid in rc.get_nearby_units():
        if rc.get_entity_type(eid) != EntityType.BUILDER_BOT:
            continue
        if rc.get_team(eid) == my_team:
            continue
        out.append(rc.get_position(eid))
    return out


def _can_reach_center(tx: int, ty: int, center: Position) -> bool:
    """True if a launcher on (tx, ty) could throw into the killbox centre — using
    the engine's actual launcher attack pattern (direction is ignored)."""
    reach = rc.get_attackable_tiles_from(Position(tx, ty), Direction.NORTH, EntityType.LAUNCHER)
    return any(t.x == center.x and t.y == center.y for t in reach)


def _buildable(x: int, y: int) -> bool:
    if not (0 <= x < map_info._width and 0 <= y < map_info._height):
        return False
    bit = 1 << (x + y * map_info._width)
    if map_info._bm_env[map_info._IDX_ENV_WALL] & bit:
        return False
    if map_info._bm_env[map_info._IDX_ENV_ORE_TI] & bit:
        return False
    if map_info._bm_any_building & bit:
        return False
    if (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & bit:
        return False
    return True


def _find_launcher_tile():
    """The best tile to build a trap launcher on, or None.

    We only build a trap when the enemy builder LITERALLY CANNOT avoid being
    launched. Consider every tile the enemy could be on next turn: staying put,
    or stepping to a passable, unoccupied neighbour. The enemy is doomed iff, for
    a launcher we place at T, every one of those tiles is "caught":
      * covered by one of my gunners' fire (it won't/can't safely stand there),
      * in pickup range of a launcher of mine that ALREADY exists,
      * within Chebyshev 1 of T, i.e. our new launcher would fling it, or
      * == T itself (T becomes an impassible building, blocking that step).
    Impassible structures / occupied tiles are simply not reachable, so they are
    never an escape. Standing still counts as an option, so the enemy's own tile
    must be caught too.

    A single launcher at T catches exactly the 3x3 centred on T (its own tile,
    now a building, plus its Chebyshev-1 pickup ring). So the tiles the enemy
    could flee to that aren't ALREADY caught must all fit within one such 3x3 —
    T must lie within Chebyshev 1 of every uncaught escape tile. We check all
    such placement positions and keep the one closest to the killbox centre (most
    reliable throw), that is buildable and can actually throw into the centre.
    """
    p = killbox_plan.plan()
    if p is None:
        return None
    # Only worth it once the killbox is armed (its gunner exists).
    if not _has_mine(p["gunner"], map_info._IDX_GUNNER):
        return None
    center = p["center"]
    w, h = map_info._width, map_info._height
    board = map_info._board_mask
    my_launchers = map_info._bm_et[map_info._IDX_LAUNCHER] & map_info._bm_team[map_info._my_team_idx]

    # Tiles from which a bot is already caught WITHOUT any new launcher: covered
    # by my gunner fire, or in pickup range of an existing launcher of mine.
    already_caught = map_info._bm_my_gunner_claims | map_info.expand_chebyshev(my_launchers, 1)
    # Tiles an enemy builder cannot step onto: walls, any building (incl. cores),
    # or a tile occupied by a bot.
    blocked = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
    )

    best = None
    best_key = None
    for e in _enemy_builders():
        # Every tile the enemy could occupy next turn: stay put, or step to a
        # passable, unoccupied neighbour.
        esc = [(e.x, e.y)]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                x, y = e.x + dx, e.y + dy
                if not (0 <= x < w and 0 <= y < h):
                    continue  # off board — unreachable
                if blocked & (1 << (x + y * w)):
                    continue  # structure / occupied — unreachable
                esc.append((x, y))
        # Escape tiles a NEW launcher must account for (the rest are already caught).
        uncaught = [(x, y) for (x, y) in esc if not (already_caught >> (x + y * w)) & 1]
        if not uncaught:
            continue  # already inescapable — no new launcher needed

        # T must lie within Chebyshev 1 of EVERY uncaught tile: the intersection
        # of their 3x3 neighbourhoods. Empty intersection => can't trap with one.
        cand = board
        for (x, y) in uncaught:
            cand &= map_info.expand_chebyshev(1 << (x + y * w), 1)
            if not cand:
                break

        m = cand
        while m:
            lsb = m & -m
            n = lsb.bit_length() - 1
            m ^= lsb
            tx, ty = n % w, n // w
            if not _buildable(tx, ty):
                continue
            if (tx - center.x) ** 2 + (ty - center.y) ** 2 > _THROW_RANGE_SQ:
                continue  # cheap range filter
            # Authoritative: a launcher here must actually be able to throw into
            # the killbox centre (else it's a useless trap).
            if not _can_reach_center(tx, ty, center):
                continue
            key = (
                (tx - center.x) ** 2 + (ty - center.y) ** 2,
                (tx - map_info._my_pos.x) ** 2 + (ty - map_info._my_pos.y) ** 2,
                n,
            )
            if best_key is None or key < best_key:
                best_key = key
                best = Position(tx, ty)
    return best


def score() -> int:
    global target
    target = None
    if not units.builder._economy_builder:
        return 0
    if not killbox_plan.active():
        return 0  # only trap into a live (gunner-only) killbox
    if not _can_afford_launcher():
        return 0
    t = _find_launcher_tile()
    if t is None:
        return 0
    target = t
    return MAX_SCORE


def run() -> None:
    log("TRAP LAUNCHER")
    if target is None:
        return
    if map_info._my_pos.distance_squared(target) != 1:
        nav.move_adjacent(target)
        return
    if _can_afford_launcher() and rc.can_build_launcher(target):
        rc.build_launcher(target)
        map_info.update_at(target)
