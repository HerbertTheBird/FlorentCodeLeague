"""Builder state: save friendly buildings from being killed.

Ranks just below attack (everything else yields to it). For each damaged friendly
building we ask a single question: assuming the nearest enemy builder rushes it
and starts attacking the instant it is adjacent, can we get adjacent and start
healing before it dies?

Timing model (we move first, then they move, alternating; "distance" is the BFS
distance to a tile ADJACENT to the building, since that is where either side can
act). We reach and heal on our (my_dist + 1)-th action; the enemy lands its first
attack on its (enemy_dist + 1)-th action. Working the interleave out, the number
of enemy hits that land before our heal is

    attacks = max(0, my_dist - enemy_dist)

and a builder attack does 2 damage, so the building survives iff

    current_hp > 2 * attacks

If my_dist <= enemy_dist we win regardless of HP (0 hits land first). Buildings we
cannot save in time are dropped. A full-HP conveyor we are already standing next
to that an enemy is ALSO next to counts as a target too (guard it -- heal it back
up as they chip it). Among the survivors we pick: closest, then lowest HP, then
nearest our core by Manhattan.
"""
import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import Controller, Position
from log import log

rc: Controller = None
nav: Pathing = None

# A builder bot's attack does 2 HP of damage.
BUILDER_ATTACK_DMG = 2
# Don't bother evaluating a building we'd need more than this many moves to reach.
MAX_HEAL_DIST = 12
# Only heal a building missing MORE than this much HP -- a heal restores 4 HP and
# costs titanium + a cooldown, so topping off a 1-2 point dent is wasteful.
HEAL_MIN_DAMAGE = 2


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


# ---------------------------------------------------------------------------
# Opportunistic adjacent heal -- called every turn from builder.run(), free
# after the main action. Tops off the best-priority damaged building beside us.
# ---------------------------------------------------------------------------
_HEAL_PRIORITY = [1] * 16
_HEAL_PRIORITY[map_info._IDX_BARRIER] = 2
_HEAL_PRIORITY[map_info._IDX_SPLITTER] = 2
_HEAL_PRIORITY[map_info._IDX_CONVEYOR] = 3
_HEAL_PRIORITY[map_info._IDX_HARVESTER] = 4
_HEAL_PRIORITY[map_info._IDX_GUNNER] = 5
_HEAL_PRIORITY[map_info._IDX_SENTINEL] = 5
_HEAL_PRIORITY[map_info._IDX_LAUNCHER] = 5
_HEAL_PRIORITY[map_info._IDX_CORE] = 6


def _do_best_heal():
    w, h = map_info._width, map_info._height
    my = map_info._bm_team[map_info._my_team_idx]
    healable = my & map_info._bm_damaged
    my_pos = map_info._my_pos
    best = None
    best_score = -1
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        x, y = my_pos.x + dx, my_pos.y + dy
        if not (0 <= x < w and 0 <= y < h):
            continue
        n = x + y * w
        if not (healable >> n) & 1:
            continue
        p = Position(x, y)
        if not rc.can_heal(p):
            continue
        et = map_info._building_et_idx[n]
        if et < 0:
            continue
        damage = map_info._MAX_HP_BY_IDX[et] - map_info._building_hp[n]
        if damage <= HEAL_MIN_DAMAGE:
            continue                                  # not worth a heal
        s = damage * _HEAL_PRIORITY[et]
        if s > best_score:
            best_score = s
            best = p
    if best is not None:
        rc.heal(best)


# ---------------------------------------------------------------------------
# Targeting
# ---------------------------------------------------------------------------
def _adj_stand(n: int) -> int:
    """Cardinal neighbours of tile n a bot can stand on to act on it."""
    w, h = map_info._width, map_info._height
    x, y = n % w, n // w
    adj = 0
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < w and 0 <= ny < h:
            adj |= 1 << (nx + ny * w)
    return adj & map_info.passable()


def _manh_to_core(x: int, y: int) -> int:
    core = map_info._my_core
    if core is None:
        return 0
    cx = core.x if x < core.x else (core.x + 1 if x > core.x + 1 else x)
    cy = core.y if y < core.y else (core.y + 1 if y > core.y + 1 else y)
    return abs(x - cx) + abs(y - cy)


def _damage_over(mask: int, threshold: int) -> int:
    """Subset of the building tiles in `mask` missing strictly more than
    `threshold` HP."""
    result = 0
    m = mask
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        et = map_info._building_et_idx[n]
        if et < 0:
            continue
        if map_info._MAX_HP_BY_IDX[et] - map_info._building_hp[n] > threshold:
            result |= b
    return result


def _find_target():
    my = map_info._bm_team[map_info._my_team_idx]
    my_pos = map_info._my_pos
    w = map_info._width
    my_bit = 1 << (my_pos.x + my_pos.y * w)
    enemy_bots = map_info._bm_enemy_bots

    damaged_any = my & map_info._bm_damaged & map_info._bm_visible
    # Only pursue buildings missing MORE than HEAL_MIN_DAMAGE -- a 1-2 point dent
    # isn't worth moving to, and _do_best_heal won't top it off either.
    damaged = _damage_over(damaged_any, HEAL_MIN_DAMAGE)
    # A full-HP conveyor we already stand beside that an enemy also stands
    # beside. Only worth guarding if that enemy has no damaged building of ours
    # adjacent -- if it does, it will chip THAT (and _do_best_heal covers the
    # damaged one), so a full-HP tile beside such an enemy is not a target.
    # (Uses any-damage here, so we don't guard a full-HP tile beside an enemy
    # that already has even a lightly-dented building to keep hitting.)
    guard = 0
    if enemy_bots:
        my_convs = map_info._bm_conveyors & my
        full_convs = my_convs & ~map_info._bm_damaged
        free_enemies = enemy_bots & ~map_info.manhattan(damaged_any)
        guard = (full_convs & map_info.manhattan(my_bit)
                 & map_info.manhattan(free_enemies))

    candidates = (damaged | guard) & map_info.expand_manhattan(my_bit, MAX_HEAL_DIST)
    if not candidates:
        return None

    best = None
    best_key = None
    m = candidates
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        if not _adj_stand(n):
            continue                                  # walled in -- nobody can act on it
        # nav.closest already measures the distance to a tile ADJACENT to the
        # target, so pass the building tile itself -- NOT its neighbour ring.
        # (Passing the ring asks for adjacency-to-adjacency, an off-by-one that
        # reported a builder already beside the building as ~2 away instead of 0.)
        b_bit = 1 << n
        _, my_dist = nav.closest_within(b_bit, max_dist=MAX_HEAL_DIST)
        if my_dist < 0:
            continue                                  # too far / unreachable
        enemy_dist = 1 << 30
        if enemy_bots:
            _, ed = nav.closest_within(b_bit, pos=enemy_bots, max_dist=MAX_HEAL_DIST)
            if ed >= 0:
                enemy_dist = ed
        hp = map_info._building_hp[n]
        attacks = my_dist - enemy_dist                # hits that land before we heal
        if attacks > 0 and hp <= attacks * BUILDER_ATTACK_DMG:
            continue                                  # dies before we can save it
        key = (my_dist, hp, _manh_to_core(n % w, n // w))
        if best_key is None or key < best_key:
            best_key = key
            best = Position(n % w, n // w)
    return best


# Just below attack (9); above every other state.
MAX_SCORE = 8.75
_cached_target = None


def score():
    global _cached_target
    _cached_target = _find_target()
    return MAX_SCORE if _cached_target is not None else 0


def run():
    target = _cached_target
    if target is None:
        return
    log("HEAL", target)
    my_pos = map_info._my_pos
    if abs(target.x - my_pos.x) + abs(target.y - my_pos.y) != 1:
        # Move to a tile adjacent to the target; the actual heal is applied by
        # _do_best_heal() (builder.run) once we're adjacent and off cooldown.
        nav.move_adjacent(target, avoid_turret=False)
