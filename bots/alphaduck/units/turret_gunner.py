from fcode import Controller, Direction, EntityType, Position, Team, Environment, GameConstants
import map_info
from pathing import Pathing
from log import log
import units.turret_priority as turret_priority

ONE_SHOT_HP = GameConstants.GUNNER_DAMAGE  # 7

rc: Controller = None
nav: Pathing = None
my_pos: Position = None
my_team: Team = None
_attackable_by_dir: dict = {}
# Each firing direction's ray as a bitmask (constant — a turret never moves),
# plus their union and the longest ray length, used by _find_trap_rotation.
_ray_mask_by_dir: dict = {}
_ray_union: int = 0
_max_ray_len: int = 0


def init(c: Controller):
    global rc, nav, my_pos, my_team, _attackable_by_dir
    global _ray_mask_by_dir, _ray_union, _max_ray_len
    rc = c
    nav = Pathing(c)
    my_pos = rc.get_position()
    my_team = map_info._my_team
    _attackable_by_dir = {
        d: set(rc.get_attackable_tiles_from(my_pos, d, EntityType.GUNNER))
        for d in map_info._DIRECTIONS
    }
    w = map_info._width
    _ray_mask_by_dir = {}
    _ray_union = 0
    _max_ray_len = 0
    for d, tiles in _attackable_by_dir.items():
        m = 0
        for p in tiles:
            m |= 1 << (p.x + p.y * w)
        _ray_mask_by_dir[d] = m
        _ray_union |= m
        if len(tiles) > _max_ray_len:
            _max_ray_len = len(tiles)


def _enemy_bot_cardinally_adjacent(pos) -> bool:
    """True if an enemy builder bot sits on a tile cardinally adjacent to `pos`. Such
    a bot repairs/heals whatever is there faster than a gunner can break it, so we
    don't spend shots on economy/barriers it can keep alive."""
    bit = 1 << (pos.x + pos.y * map_info._width)
    neighbours = map_info.expand_manhattan(bit) & ~bit
    return bool(neighbours & map_info._bm_enemy_bots)


def _turret_threatens_me(bid, pos, et) -> bool:
    """True if the enemy turret `bid` (at `pos`, type `et`) has a legal shot at me."""
    try:
        d = rc.get_direction(bid)
    except Exception:
        return False
    return rc.can_fire_from(pos, d, et, my_pos)


# A gunner shoots the FIRST thing on the ray it faces, so only that one tile matters
# per direction. Classify it.
def _ray_first_hit(direction):
    """(kind, pos, bid, threatens) for the first real obstruction on `direction`'s
    ray from my_pos, or None (wall / friendly / clear ray). A bot on a tile is hit
    before the building under it. kind is one of: 'gunner', 'sentinel', 'bot', 'core',
    'harvester', 'launcher', 'barrier', 'conveyor' (conveyor covers splitters too).
    `threatens` is True only for a gunner that has a legal shot at me (used to give
    threatening gunners the top priority; False for everything else)."""
    attackable = _attackable_by_dir.get(direction)
    if not attackable:
        return None
    cur = map_info.pos_add(my_pos, direction)
    while map_info.in_bounds(cur) and cur in attackable:
        if map_info.ground_at(cur.x, cur.y) == Environment.WALL:
            return None
        bot_id = rc.get_tile_builder_bot_id(cur)
        bid = rc.get_tile_building_id(cur)
        if bot_id is None and bid is None:
            cur = map_info.pos_add(cur, direction)
            continue
        if bot_id is not None:
            if rc.get_team(bot_id) == my_team:
                return None                          # friendly bot blocks the ray
            return ('bot', cur, None, False)
        if rc.get_team(bid) == my_team:
            return None                              # friendly building blocks
        et = rc.get_entity_type(bid)
        if et == EntityType.GUNNER:
            return ('gunner', cur, bid, _turret_threatens_me(bid, cur, et))
        if et == EntityType.SENTINEL:
            return ('sentinel', cur, bid, False)
        if et == EntityType.CORE:
            return ('core', cur, bid, False)
        if et == EntityType.HARVESTER:
            return ('harvester', cur, bid, False)
        if et == EntityType.LAUNCHER:
            return ('launcher', cur, bid, False)
        if et == EntityType.BARRIER:
            return ('barrier', cur, bid, False)
        if et == EntityType.CONVEYOR or et == EntityType.SPLITTER:
            return ('conveyor', cur, bid, False)
        return None                                  # unknown enemy building -> ignore
    return None


def _find_trap_rotation():
    """A trapped enemy builder: face the direction that pins it.

    For each visible enemy builder bot, flood its reachable area on the enemy
    movement graph (unknown tiles treated as passable — an unseen tile carries
    no recorded wall/building, so it is open by default). If that whole area is
    a closed region sitting entirely on ONE of our firing rays, the bot cannot
    move off the line: return that direction so we rotate to pin it.

    Only a fallback — run() calls this only when there is nothing to fire at and
    no higher-priority rotation, so we never trade a real shot for a pin."""
    enemy_bots = map_info._bm_enemy_bots
    if not enemy_bots or not _ray_union:
        return None
    current = rc.get_direction()

    # Enemy movement graph. get_avoid(False, enemy_pov=True) blocks walls, both
    # cores and non-conveyor buildings (incl. this gunner) but not enemy bots;
    # unseen tiles carry no blocker, so they stay passable ("unknown passable").
    passable = map_info._board_mask & ~map_info.get_avoid(False, enemy_pov=True)
    passable |= enemy_bots  # a bot's own square / other enemy bots don't block it

    bots = enemy_bots
    while bots:
        b = bots & -bots
        bots ^= b
        if not (b & _ray_union):
            continue  # not even on a ray — cannot be pinned under one
        region = b
        frontier = b
        trapped = True
        while frontier:
            frontier = map_info.expand_manhattan(frontier) & passable & ~region
            if not frontier:
                break
            region |= frontier
            if (region & ~_ray_union) or region.bit_count() > _max_ray_len:
                trapped = False
                break
        if not trapped:
            continue
        for d, m in _ray_mask_by_dir.items():
            if d == current:
                continue  # already facing it — no rotation needed
            if region & ~m == 0 and m:
                return d
    return None


# Fire/turn priority, highest first. "fire" = shoot the first thing on our CURRENT
# ray (no rotation); "turn" = rotate to a facing whose first ray obstruction is the
# wanted target (we fire it next turn).
#   1. fire at a GUNNER that threatens me
#   2. turn to a GUNNER that threatens me
#   3. fire at a sentinel/gunner
#   4. turn to a sentinel/gunner
#   5. fire at a builder bot, OR a harvester/launcher/barrier/conveyor/splitter with
#      no enemy bot cardinally adjacent, OR the core
#   6. turn to pin a trapped builder bot
#   7. turn to a conveyor/splitter cardinally adjacent to the enemy core
#   8. turn to a LOADED conveyor/splitter
#   9. turn to a harvester
#  10. turn to a launcher
#  11. turn to a barrier
#  12. turn to any conveyor/splitter (only while the enemy core is visible)
#  13. turn to the core
def run():
    map_info.update()
    if rc.get_action_cooldown() > 0:
        return

    current = rc.get_direction()
    rotate_cost = GameConstants.GUNNER_ROTATE_COST + GameConstants.GUNNER_AMMO_COST
    can_rotate = rc.get_global_resources() >= rotate_cost + map_info.ti_reserve()
    w = map_info._width
    loaded = map_info._bm_ti_carrying

    hits = {}
    for d in map_info._DIRECTIONS:
        if d == Direction.CENTRE:
            continue
        hits[d] = _ray_first_hit(d)
    cur_hit = hits.get(current)

    def fire(pos) -> bool:
        if turret_priority.should_hold_fire(rc, pos):
            return False                             # conserve ammo -- wasted shot
        if rc.can_fire(pos):
            rc.fire(pos)
            log(f"gunner fired at {pos}")
            return True
        return False

    def turn(d) -> bool:
        if d is not None and d != current and can_rotate and rc.can_rotate(d):
            rc.rotate(d)
            log(f"gunner rotated toward {d}")
            return True
        return False

    def turn_to(pred):
        """First non-current facing whose ray hit matches `pred` and isn't a hold-fire
        (wasted) target, else None -- so we don't spend rotate-ammo to line up a shot
        we'd then decline to take."""
        for d, h in hits.items():
            if d == current or h is None:
                continue
            if pred(h) and not turret_priority.should_hold_fire(rc, h[1]):
                return d
        return None

    def econ_fireable(h) -> bool:
        return (h[0] in ('harvester', 'launcher', 'barrier', 'conveyor')
                and not _enemy_bot_cardinally_adjacent(h[1]))

    def is_loaded_conv(h) -> bool:
        return h[0] == 'conveyor' and (loaded >> (h[1].x + h[1].y * w)) & 1

    # 'conveyor' kind already covers splitters (see _ray_first_hit).
    core_adj = map_info.manhattan(map_info._bm_their_core_area)   # cardinal neighbours of enemy core
    def is_core_adj_conv(h) -> bool:
        return h[0] == 'conveyor' and (core_adj >> (h[1].x + h[1].y * w)) & 1

    cur_kind = cur_hit[0] if cur_hit else None
    cur_threat = cur_hit[3] if cur_hit else False

    # 1: fire at a threatening gunner on our ray.
    if cur_kind == 'gunner' and cur_threat and fire(cur_hit[1]):
        return
    # 2: turn to a threatening gunner.
    if turn(turn_to(lambda h: h[0] == 'gunner' and h[3])):
        return
    # 3: fire at any turret (gunner/sentinel) on our ray.
    if cur_kind in ('gunner', 'sentinel') and fire(cur_hit[1]):
        return
    # 4: turn to any turret.
    if turn(turn_to(lambda h: h[0] in ('gunner', 'sentinel'))):
        return
    # 5: fire at a bot / fireable economy / the core on our ray.
    if cur_hit and (cur_kind in ('bot', 'core') or econ_fireable(cur_hit)) and fire(cur_hit[1]):
        return
    # 6: turn to pin a trapped builder bot.
    if turn(_find_trap_rotation()):
        return
    # 7: turn to a conveyor/splitter cardinally adjacent to the enemy core (a core feed).
    if turn(turn_to(is_core_adj_conv)):
        return
    # 8: turn to a loaded conveyor/splitter.
    if turn(turn_to(is_loaded_conv)):
        return
    # 9: turn to a harvester.
    if turn(turn_to(lambda h: h[0] == 'harvester')):
        return
    # 10: turn to a launcher.
    if turn(turn_to(lambda h: h[0] == 'launcher')):
        return
    # 11: turn to a barrier.
    if turn(turn_to(lambda h: h[0] == 'barrier')):
        return
    # 12: turn to any conveyor/splitter -- but ONLY while we can SEE the enemy core (a
    # siege gunner cutting belts that feed the core it's aimed at). Loaded and core-
    # adjacent conveyors are already handled at tiers 7-8.
    sees_enemy_core = bool(map_info._bm_their_core_area & map_info._bm_visible)
    if sees_enemy_core and turn(turn_to(lambda h: h[0] == 'conveyor')):
        return
    # 13: turn to the core.
    if turn(turn_to(lambda h: h[0] == 'core')):
        return

    # Nothing to fire at or turn to -- recycle if idle (kept if it can hit the core).
    turret_priority.scrap_if_idle(rc)
