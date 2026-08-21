from main import has_op
from fcode import Controller, Direction, EntityType, Position, Team, Environment, GameConstants
import map_info
import pathing
from pathing import Pathing
from log import log
import units.turret_priority as turret_priority
import units.states.attack as attack

ONE_SHOT_HP = GameConstants.GUNNER_DAMAGE  # 7

# Rotate to a different facing only when its attack.py score beats this. Same
# scale as the placement scorer (GUNNER_BUILDING_SCORE et al.).
ROTATE_SCORE_THRESHOLD = 26

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

CARDINAL_OFFSETS = [(0, 1), (0, -1), (-1, 0), (1, 0)]


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
    a bot repairs a barrier there faster than a gunner can break it, so the barrier is
    effectively unkillable."""
    bit = 1 << (pos.x + pos.y * map_info._width)
    neighbours = map_info.expand_manhattan(bit) & ~bit
    return bool(neighbours & map_info._bm_enemy_bots)


def _scan_ray(direction, attackable, allow_builder_bots: bool,
              bot_must_be_on_my_conveyor: bool = False):
    """Walk forward from my_pos in `direction`. The first non-empty tile is the
    stopping tile.

    Returns (target_etype, fire_at) where:
      - target_etype: the EntityType of the *enemy* thing motivating the shot
        (used for rotation scoring).
      - fire_at: the Position to pass to rc.fire — the first real game-side
        obstruction on the ray.
    Returns None if firing is not desired in this direction.

    Rules:
      - Wall: ray blocked, no fire.
      - Friendly building / friendly builder bot: blocks, no fire.
      - Enemy building: fire.
      - Enemy builder bot: fire only if `allow_builder_bots`. If
        `bot_must_be_on_my_conveyor`, additionally require the bot to stand on a
        friendly conveyor tile that is already damaged with no friendly bot
        adjacent to heal it."""
    w = map_info._width
    cur = map_info.pos_add(my_pos, direction)
    fire_at = None
    while map_info.in_bounds(cur) and cur in attackable:
        n = cur.x + cur.y * w
        if map_info.ground_at(cur.x, cur.y) == Environment.WALL:
            return None

        bot_id = rc.get_tile_builder_bot_id(cur)
        bid = rc.get_tile_building_id(cur)

        # Empty
        if bot_id is None and bid is None:
            cur = map_info.pos_add(cur, direction)
            continue

        # First real obstruction (the engine will resolve fire to this)
        if fire_at is None:
            fire_at = cur

        if bot_id is not None:
            if rc.get_team(bot_id) == my_team:
                return None
            if not allow_builder_bots:
                return None
            if bot_must_be_on_my_conveyor:
                my_convs = map_info._bm_conveyors & map_info._bm_team[map_info._my_team_idx]
                if not (my_convs & (1 << n)):
                    return None
                # Only commit a rotation to clear an enemy bot off our line if
                # the conveyor is already taking damage AND no friendly builder
                # is within Chebyshev 1 (a friendly bot can heal/repair, so let
                # it handle the trespasser instead of burning a rotation).
                if bid is None:
                    return None
                if rc.get_hp(bid) >= rc.get_max_hp(bid):
                    return None
                cheb = map_info.expand_chebyshev(1 << n)
                if cheb & map_info._bm_friendly_bots:
                    return None
            return EntityType.BUILDER_BOT, fire_at

        # Building only
        bid_etype = rc.get_entity_type(bid)
        if rc.get_team(bid) == my_team:
            return None
        # A barrier with an enemy builder bot cardinally adjacent is repaired faster
        # than a gunner can break it -- unkillable -- and it blocks the rest of the
        # ray, so treat this direction as having nothing to shoot.
        if bid_etype == EntityType.BARRIER and _enemy_bot_cardinally_adjacent(cur):
            return None
        return bid_etype, fire_at

    return None


def _decide_fire():
    direction = rc.get_direction()
    if direction == Direction.CENTRE:
        return None
    attackable = _attackable_by_dir[direction]
    res = _scan_ray(direction, attackable, allow_builder_bots=True)
    return None if res is None else res[1]


def _best_scored_dir(current):
    """Score every facing with attack.py's gunner reasoning and return the
    (direction, score) of the best. Ties prefer the current facing, so we only
    ever rotate when a DIFFERENT direction is strictly better."""
    scores = attack.gunner_dir_scores_at(my_pos)   # [(Direction, score), ...]
    best_dir = current
    best_score = -1
    for d, s in scores:
        if d == current:
            best_score = s
            break
    for d, s in scores:
        if s > best_score:
            best_dir, best_score = d, s
    return best_dir, best_score


def _find_trap_rotation():
    """A trapped enemy builder: face the direction that pins it.

    For each visible enemy builder bot, flood its reachable area on the enemy
    movement graph (unknown tiles treated as passable — an unseen tile carries
    no recorded wall/building, so it is open by default). If that whole area is
    a closed region sitting entirely on ONE of our firing rays, the bot cannot
    move off the line: return that direction so we rotate to pin it.

    Only a fallback — run() calls this only when there is nothing to fire at and
    no ordinary rotation target, so we never trade a real shot for a pin."""
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
        # Flood the reachable region. Bail the moment it leaves the ray-union or
        # grows past the longest ray: either way it cannot fit under one ray.
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


def run():
    map_info.update()

    if rc.get_action_cooldown() > 0:
        return

    rotate_cost = GameConstants.GUNNER_ROTATE_COST + GameConstants.GUNNER_AMMO_COST
    current = rc.get_direction()
    fire_target = _decide_fire()

    # Primary turn logic: score every facing with attack.py's reasoning and turn
    # to the best if it clears the threshold and isn't where we already point.
    # (Ties prefer current, so a good current target keeps us firing rather than
    # spinning.) This can outrank firing a low-value target on the current ray --
    # the score already credits the current facing's own ray, so we only turn
    # away when another facing is genuinely better.
    best_dir, best_score = _best_scored_dir(current)
    if (best_score > ROTATE_SCORE_THRESHOLD and best_dir != current
            and rc.get_global_resources() >= rotate_cost and rc.can_rotate(best_dir)):
        rc.rotate(best_dir)
        log(f"gunner rotated toward {best_dir} (score {best_score})")
        return

    if fire_target is not None and rc.can_fire(fire_target):
        rc.fire(fire_target)
        log(f"gunner fired at {fire_target}")
        return

    # Nothing worth turning to and nothing to shoot: as a last resort, pin a
    # trapped enemy builder confined to a single ray (a guaranteed kill).
    pin_dir = _find_trap_rotation()
    if (pin_dir is not None
            and rc.get_global_resources() >= rotate_cost and rc.can_rotate(pin_dir)):
        rc.rotate(pin_dir)
        log(f"gunner rotating to pin trapped builder toward {pin_dir}")
        return

    # Truly idle -- nothing to shoot and no enemy bots tracked anywhere. Enemy
    # BUILDINGS are not considered (the old comment claimed they were; that check
    # never existed, and adding it measured -0.0081). Recycles the build scale.
    if fire_target is None:
        turret_priority.scrap_if_idle(rc)