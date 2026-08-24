"""Builder state: chase enemy builders that have entered our harvest zone.

Enemy builders raiding our half come to chip our conveyors and harvesters. We
can't damage them (builders only heal), but following the nearest raider keeps a
builder next to it -- so the free per-turn _do_best_heal() repairs whatever it
attacks, and we contest the ground. Enemies are Voronoi-partitioned across our
builders like every other target, so we spread out over multiple raiders.

Ranks above harvest and below route: worth interrupting a harvest run to defend
the economy, but not our active routing.
"""
import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import Controller, Position
from log import log

rc: Controller = None
nav: Pathing = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _my_claims() -> int:
    zone = units.builder._harvest_zone
    if not zone:
        return 0
    enemies = map_info._bm_enemy_bots & zone
    if not enemies:
        return 0
    # Drop any raider that already has a (different) friendly bot within BFS 2 --
    # that friendly is on it, so we shouldn't pile a second chaser on. Using
    # _bm_friendly_bots (which excludes self) means an enemy is only removed for
    # OTHER builders; the friendly actually next to it still keeps chasing.
    friendly = map_info._bm_friendly_bots
    if friendly:
        passable = map_info.passable()
        reach = friendly
        near = 0
        for _ in range(2):                    # BFS layers 1 and 2
            nxt = map_info.expand_manhattan(reach)
            near |= nxt & enemies
            reach = nxt & passable & ~reach
        enemies &= ~near
        if not enemies:
            return 0
    w = map_info._width
    my_pos = map_info._my_pos
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    return pathing.claim_subset(my_mask, map_info._bm_friendly_bots, enemies, tie_self=False)


NORMAL_SCORE = 5.9
LOCK_SCORE = 11          # lone early rusher (enemy id 3/4, only 1 ever seen) + I'm closest
MAX_SCORE = 11
_cached_target = None


def _lone_rusher_id_ok() -> bool:
    """The enemy has only EVER shown one builder, and its id is 3 or 4."""
    ev = map_info._enemy_ids_ever
    return len(ev) == 1 and next(iter(ev)) in (3, 4)


def _closest_lock_target():
    """The current enemy builder if I'm the friendly closest to it, else None."""
    enemies = map_info._bm_enemy_bots
    if not enemies:
        return None
    w = map_info._width
    my_pos = map_info._my_pos
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    mine = pathing.claim_subset(my_mask, map_info._bm_friendly_bots, enemies, tie_self=False)
    if not mine:
        return None
    tgt, _ = nav.closest(mine)
    return tgt


def score(can_move=True):
    global _cached_target
    _cached_target = None
    if not can_move:
        return 0                            # chasing is pure movement
    # Lone-rusher lock (bypasses the rush-mode gate): the enemy has only EVER fielded
    # one builder, id 3 or 4. Whichever friendly is closest to it locks on at top
    # priority (11) so that lone rusher never slips away.
    if _lone_rusher_id_ok():
        tgt = _closest_lock_target()
        if tgt is not None:
            _cached_target = tgt
            return LOCK_SCORE
    # Otherwise a rush-mode builder stays fully committed to the enemy core -- it never
    # peels back to chase raiders (the old early-raider exception is dropped; only the
    # lone-rusher lock above can pull a rush builder off the enemy core).
    if units.builder.in_rush_mode():
        return 0
    claims = _my_claims()
    if claims:
        target, _ = nav.closest(claims)     # nearest reachable raider
        if target is not None:
            _cached_target = target
    return NORMAL_SCORE if _cached_target is not None else 0


def _nearest_core_tile(ex: int, ey: int):
    """The core tile (of our 2x2) nearest the enemy at (ex, ey), or None if we have
    no core. Clamp the enemy coords into the core's [cx, cx+1] x [cy, cy+1] box."""
    core = map_info._my_core
    if core is None:
        return None
    ncx = core.x if ex < core.x else (core.x + 1 if ex > core.x + 1 else ex)
    ncy = core.y if ey < core.y else (core.y + 1 if ey > core.y + 1 else ey)
    return ncx, ncy


def run(can_move=True):
    if not can_move or _cached_target is None:
        return
    log("CHASE", _cached_target)
    enemy = _cached_target
    w, h = map_info._width, map_info._height

    # The cardinal direction from the enemy toward our core -- the way they'd advance
    # to reach it. We want to stand on the far side of them (one tile toward the core)
    # to body-block that advance. Horizontal wins ties.
    nc = _nearest_core_tile(enemy.x, enemy.y)
    rdx = rdy = 0
    axis_priority = None
    if nc is not None:
        dx, dy = nc[0] - enemy.x, nc[1] - enemy.y
        if dx == 0 and dy == 0:
            pass                                    # enemy sitting on the core
        elif abs(dx) >= abs(dy):                    # horizontal is most relevant
            rdx, axis_priority = (1 if dx > 0 else -1), 'vertical'   # slide perp to close in
        else:                                       # vertical is most relevant
            rdy, axis_priority = (1 if dy > 0 else -1), 'horizontal'

    # Interpose target = EITHER one tile toward the core from the enemy, OR two tiles
    # (both on the core-side, blocking the advance). Keep whichever are real, open
    # tiles and let move_to pick the nearer; if neither is valid, just tail the enemy.
    my = map_info._my_pos
    targets = set()
    if rdx or rdy:
        for mult in (1, 2):
            tp = Position(enemy.x + rdx * mult, enemy.y + rdy * mult)
            if not (0 <= tp.x < w and 0 <= tp.y < h):
                continue
            if tp == my:
                targets.add(tp)             # already standing on it -> valid; lets us stay
                continue
            # Otherwise it must be open terrain with no OTHER bot on it.
            if (map_info.is_passable(tp)
                    and not (rc.is_in_vision(tp)
                             and rc.get_tile_builder_bot_id(tp) is not None)):
                targets.add(tp)
    if targets:
        nav.move_to(targets, axis_priority=axis_priority)
    else:
        nav.move_adjacent(enemy)
