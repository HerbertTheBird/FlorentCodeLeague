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


def _nearest_core_tile(ex: int, ey: int):
    """The core tile (of our 2x2) nearest the enemy at (ex, ey), or None if we have
    no core. Clamp the enemy coords into the core's [cx, cx+1] x [cy, cy+1] box."""
    core = map_info._my_core
    if core is None:
        return None
    ncx = core.x if ex < core.x else (core.x + 1 if ex > core.x + 1 else ex)
    ncy = core.y if ey < core.y else (core.y + 1 if ey > core.y + 1 else ey)
    return ncx, ncy


def _core_step(ex: int, ey: int):
    """(rdx, rdy, axis_priority): one cardinal step from the enemy toward our core,
    along the DOMINANT axis (horizontal wins ties). (0, 0, None) if we have no core
    or the enemy sits on it. Shared by the claim geometry and run()'s body-block."""
    nc = _nearest_core_tile(ex, ey)
    if nc is None:
        return 0, 0, None
    dx, dy = nc[0] - ex, nc[1] - ey
    if dx == 0 and dy == 0:
        return 0, 0, None
    if abs(dx) >= abs(dy):                       # horizontal most relevant -> slide vertically
        return (1 if dx > 0 else -1), 0, 'vertical'
    return 0, (1 if dy > 0 else -1), 'horizontal'


def _interpose_n(ex: int, ey: int) -> int:
    """Tile index one cardinal step from the enemy toward our core -- where we'd stand
    to body-block its advance. Falls back to the enemy's own tile if it sits on the
    core or that step leaves the board. This is the point claims measure distance to."""
    w, h = map_info._width, map_info._height
    rdx, rdy, _ = _core_step(ex, ey)
    tx, ty = ex + rdx, ey + rdy
    if (rdx or rdy) and 0 <= tx < w and 0 <= ty < h:
        return tx + ty * w
    return ex + ey * w


def _interpose_map(enemies: int):
    """(interpose_mask, {interpose_tile_n: enemy Position}) for every enemy bit in
    `enemies`. Claims/closest run on the interpose tiles; the winner maps back to the
    enemy it blocks. (Two enemies mapping to one tile just keep the last -- harmless.)"""
    w = map_info._width
    mask = 0
    emap = {}
    m = enemies
    while m:
        b = m & -m
        m ^= b
        en = b.bit_length() - 1
        inp = _interpose_n(en % w, en // w)
        mask |= 1 << inp
        emap[inp] = Position(en % w, en // w)
    return mask, emap


def _my_claims():
    """(claimed interpose-tile mask, enemy-by-interpose-tile map). Empty (0, {}) when
    there is nothing to chase."""
    zone = units.builder._harvest_zone
    if not zone:
        return 0, {}
    enemies = map_info._bm_enemy_bots & zone
    if not enemies:
        return 0, {}
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
            return 0, {}
    w = map_info._width
    my_pos = map_info._my_pos
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    # Voronoi-partition the INTERPOSE tiles (enemy + step toward core), not the raw
    # enemy tiles: a builder claims the raider whose block-spot it is closest to.
    interpose_mask, emap = _interpose_map(enemies)
    claimed = pathing.claim_subset(my_mask, map_info._bm_friendly_bots, interpose_mask, tie_self=False)
    return claimed, emap


NORMAL_SCORE = 5.9
LOCK_SCORE = 11          # lone early rusher (enemy id 3/4, only 1 ever seen) + I'm closest
MAX_SCORE = 11
_cached_target = None


def _lone_rusher_id_ok() -> bool:
    """The enemy has only EVER shown one builder, and its id is 3 or 4."""
    ev = map_info._enemy_ids_ever
    return len(ev) == 1 and next(iter(ev)) in (3, 4)


def _closest_lock_target():
    """The enemy builder I lock onto (as a Position) if I'm the friendly closest to
    its interpose tile, else None."""
    enemies = map_info._bm_enemy_bots
    if not enemies:
        return None
    w = map_info._width
    my_pos = map_info._my_pos
    my_mask = 1 << (my_pos.x + my_pos.y * w)
    interpose_mask, emap = _interpose_map(enemies)
    mine = pathing.claim_subset(my_mask, map_info._bm_friendly_bots, interpose_mask, tie_self=False)
    if not mine:
        return None
    tgt, _ = nav.closest(mine)                # closest interpose tile I claimed
    if tgt is None:
        return None
    return emap.get(tgt.x + tgt.y * w, tgt)   # map it back to the enemy it blocks


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
    claims, emap = _my_claims()             # claimed INTERPOSE tiles + enemy-by-tile map
    if claims:
        w = map_info._width
        tgt, _ = nav.closest(claims)        # nearest reachable interpose (block-spot)
        if tgt is not None:
            _cached_target = emap.get(tgt.x + tgt.y * w, tgt)   # -> the raider it blocks
    return NORMAL_SCORE if _cached_target is not None else 0


def run(can_move=True):
    if not can_move or _cached_target is None:
        return
    log("CHASE", _cached_target)
    enemy = _cached_target
    w, h = map_info._width, map_info._height

    # The cardinal direction from the enemy toward our core -- the way they'd advance
    # to reach it. We want to stand on the far side of them (one tile toward the core)
    # to body-block that advance. Horizontal wins ties. (Same geometry the claim uses.)
    rdx, rdy, axis_priority = _core_step(enemy.x, enemy.y)

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
