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
from fcode import Controller, Position, GameConstants
from log import log

rc: Controller = None
nav: Pathing = None

# A builder bot's attack does 2 HP of damage.
BUILDER_ATTACK_DMG = 2
# A heal restores this much HP per turn.
HEAL_AMOUNT = GameConstants.HEAL_AMOUNT
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
    # A lightly-dented building (<= HEAL_MIN_DAMAGE) is only worth pursuing if an
    # enemy builder is adjacent to it -- that enemy will keep chipping it, so we
    # want to already be in position when the damage grows past the threshold.
    # A lightly-dented building with no enemy on it isn't worth a move (and
    # _do_best_heal won't top it off either), so drop it.
    over2 = _damage_over(damaged_any, HEAL_MIN_DAMAGE)
    if enemy_bots:
        light = damaged_any & ~over2                      # damage <= HEAL_MIN_DAMAGE
        damaged = over2 | (light & map_info.manhattan(enemy_bots))
    else:
        damaged = over2
    # Divide the damaged buildings across builders with the same Voronoi claim
    # every other state uses: only pursue the ones this builder is (weakly)
    # closest to, so several builders don't all converge on one building.
    claimed = pathing.claim_subset(my_bit, map_info._bm_friendly_bots, damaged,
                                   tie_self=True)
    # A full-HP conveyor we already stand beside that an enemy also stands
    # beside. Only worth guarding if that enemy has no damaged building of ours
    # adjacent -- if it does, it will chip THAT (and _do_best_heal covers the
    # damaged one), so a full-HP tile beside such an enemy is not a target.
    # (Uses any-damage here, so we don't guard a full-HP tile beside an enemy
    # that already has even a lightly-dented building to keep hitting.) Guard is
    # defined by MY position, so it is inherently local -- not Voronoi-claimed.
    guard = 0
    if enemy_bots:
        my_convs = map_info._bm_conveyors & my
        full_convs = my_convs & ~map_info._bm_damaged
        free_enemies = enemy_bots & ~map_info.manhattan(damaged_any)
        guard = (full_convs & map_info.manhattan(my_bit)
                 & map_info.manhattan(free_enemies))

    candidates = (claimed | guard) & map_info.expand_manhattan(my_bit, MAX_HEAL_DIST)
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


def _adj_enemy_count(x: int, y: int, enemy_bots: int) -> int:
    """Number of enemy builder bots cardinally adjacent to tile (x, y) -- i.e.
    how many are attacking whatever building sits there, at BUILDER_ATTACK_DMG
    each per turn."""
    w, h = map_info._width, map_info._height
    cnt = 0
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < w and 0 <= ny < h and (enemy_bots >> (nx + ny * w)) & 1:
            cnt += 1
    return cnt


def _mask_bfs_dist(src: int, dst: int, passable: int, cap: int) -> int:
    """Min BFS steps (moving through `passable`) from any tile in `src` to any tile
    in `dst`, capped at `cap` (returns cap+1 if not reachable within it). 0 if the
    two masks already overlap."""
    if src & dst:
        return 0
    frontier = src
    visited = src
    for step in range(1, cap + 1):
        frontier = map_info.expand_manhattan(frontier) & passable & ~visited
        if not frontier:
            return cap + 1
        if frontier & dst:
            return step
        visited |= frontier
    return cap + 1


def _detour_target(primary: Position):
    """When we're already adjacent to `primary`, look for a MORE-URGENT building
    (lower current HP) that we can dash to, heal to full, and return to heal
    `primary` before it dies -- accounting for enemies attacking both throughout.
    Returns the best such detour tile (lowest HP), or `primary` if none is worth
    it. Re-run every turn, so if the situation shifts the plan self-corrects.

    Timing model (per turn: my heal = +HEAL_AMOUNT, each adjacent enemy =
    -BUILDER_ATTACK_DMG):
      - Detour tile C: net heal = HEAL_AMOUNT - DMG*enemies_on_C; must be > 0 or C
        never reaches full. turns_to_full = ceil(damage_C / net).
      - Round trip = walk-to-C + turns_to_full + walk-back (~2*walk).
      - `primary` (losing DMG*enemies_on_P/turn, unhealed while we're gone) must
        still be alive when we get back."""
    my_pos = map_info._my_pos
    if abs(primary.x - my_pos.x) + abs(primary.y - my_pos.y) != 1:
        return primary                                # not adjacent -> no detour

    w = map_info._width
    my = map_info._bm_team[map_info._my_team_idx]
    my_bit = 1 << (my_pos.x + my_pos.y * w)
    enemy_bots = map_info._bm_enemy_bots

    p_n = primary.x + primary.y * w
    hp_p = map_info._building_hp[p_n]
    enemies_on_p = _adj_enemy_count(primary.x, primary.y, enemy_bots) if enemy_bots else 0

    # All visible damaged friendly buildings within heal range, excluding primary.
    cand = (my & map_info._bm_damaged & map_info._bm_visible
            & map_info.expand_manhattan(my_bit, MAX_HEAL_DIST) & ~(1 << p_n))
    best = None
    best_hp = None
    m = cand
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        hp_c = map_info._building_hp[n]
        if hp_c >= hp_p:                              # not more urgent than primary
            continue
        et = map_info._building_et_idx[n]
        if et < 0:
            continue
        cx, cy = n % w, n // w
        enemies_on_c = _adj_enemy_count(cx, cy, enemy_bots) if enemy_bots else 0
        net = HEAL_AMOUNT - BUILDER_ATTACK_DMG * enemies_on_c
        if net <= 0:                                  # can't out-heal -> never full
            continue
        _, walk = nav.closest_within(b, max_dist=MAX_HEAL_DIST)
        if walk < 0:
            continue                                  # unreachable in range
        # Urgency gate: only detour when it is NOW-or-never for C -- if I keep
        # healing the primary this turn instead, C would die before I could then
        # reach it. C loses dps_c HP/turn; going now I arrive in `walk` turns,
        # waiting one turn I arrive in walk+1. So detour only if going now still
        # finds it alive (hp_c > dps_c*walk) but waiting one more turn would not
        # (hp_c <= dps_c*(walk+1)). If nothing is attacking C, it isn't dying --
        # no urgency, don't detour.
        dps_c = BUILDER_ATTACK_DMG * enemies_on_c
        if dps_c == 0 or not (dps_c * walk < hp_c <= dps_c * (walk + 1)):
            continue
        # Round trip = walk out to a tile adjacent to C, heal C to full, then walk
        # back to a tile adjacent to the primary. The return leg is NOT the same
        # length as the outbound one: we only need to get adjacent to the primary
        # again, not back to the exact tile we started on, and the nearest
        # primary-adjacent tile may be much closer to C. If a passable tile is
        # adjacent to BOTH, the return leg is 0 -- we heal both from it and never
        # abandon the primary at all -- so there's no round trip to survive.
        passable_m = map_info.passable()
        p_adj = map_info.manhattan(1 << p_n) & passable_m
        c_adj = map_info.manhattan(b) & passable_m
        if not (p_adj & c_adj):
            walk_back = _mask_bfs_dist(c_adj, p_adj, passable_m, MAX_HEAL_DIST)
            damage_c = map_info._MAX_HP_BY_IDX[et] - hp_c
            turns_to_full = -(-damage_c // net)           # ceil division
            total_away = walk + turns_to_full + walk_back
            # Primary must survive the whole round trip (unhealed while we're gone).
            if enemies_on_p and hp_p - BUILDER_ATTACK_DMG * enemies_on_p * total_away <= 0:
                continue
        if best_hp is None or hp_c < best_hp:         # lowest HP wins
            best_hp = hp_c
            best = Position(cx, cy)
    return best if best is not None else primary


# MAX_SCORE is the cap the state machine orders on. We sit ABOVE attack (9) so
# that we get evaluated first, but normally only RETURN NORMAL_SCORE (just below
# attack). The one exception: when we're wedged between 2+ of our own not-full-HP
# buildings, healing both beats attacking, so we return the full MAX_SCORE and
# outrank attack.
MAX_SCORE = 9.5
NORMAL_SCORE = 8.75
_cached_target = None


def _adjacent_multi_damaged() -> bool:
    """True if I'm cardinally adjacent to at least 2 of my own buildings that are
    not at full HP."""
    my = map_info._bm_team[map_info._my_team_idx]
    my_pos = map_info._my_pos
    my_bit = 1 << (my_pos.x + my_pos.y * map_info._width)
    adj_damaged = map_info.manhattan(my_bit) & my & map_info._bm_damaged
    return adj_damaged.bit_count() >= 2


def score(can_move=True):
    global _cached_target
    if not can_move:
        # Heal's in-place action (topping off an adjacent building) already runs
        # every turn via _do_best_heal(), so there's nothing extra for the retry.
        _cached_target = None
        return 0
    target = _find_target()
    if target is not None:
        target = _detour_target(target)       # only detours when adjacent to primary
    _cached_target = target
    # Beside 2+ of our own damaged buildings: staying to heal them outranks
    # attack. (_do_best_heal tops them off each turn; the target above just keeps
    # us in position / lets run() finish the move if we aren't adjacent yet.)
    if _adjacent_multi_damaged():
        return MAX_SCORE
    return NORMAL_SCORE if _cached_target is not None else 0


def _hold_or_flee():
    """In-place heal mode: we're staying beside 2+ damaged buildings with no walk
    target (heal scored MAX_SCORE via _adjacent_multi_damaged). Normally we just
    stand still and let _do_best_heal() top them off -- but if our tile is lethal
    THIS turn, none of the buildings' own neighbours may be safe, so flee to the
    nearest safe tile instead. Surviving to heal next turn beats dying in place."""
    my_pos = map_info._my_pos
    n = my_pos.x + my_pos.y * map_info._width
    if not (map_info.lethal_mask(rc.get_hp()) >> n) & 1:
        return                                  # safe -> stay put and heal in place
    die = map_info.lethal_mask(rc.get_hp())
    safe = (map_info.passable()
            & ~die
            & ~map_info._bm_friendly_bots
            & ~map_info._bm_enemy_bots
            & ~(1 << n))
    if not safe:
        return                                  # boxed in -- nowhere safe to go
    dest, d = nav.closest(safe)
    if dest is not None:
        nav.move_to(dest)


def run(can_move=True):
    if not can_move:
        return              # heal never wins the in-place retry (see score())
    target = _cached_target
    if target is None:
        # No walk target, but heal may have been selected to stand and heal in
        # place (adjacent to 2+ damaged buildings). Hold position when safe, flee
        # when our tile is lethal.
        _hold_or_flee()
        return
    log("HEAL", target)
    # ALWAYS ask to move toward the heal target. If we're already adjacent and
    # our tile is safe, bfs_move keeps us put (it won't move) and _do_best_heal()
    # tops the building up; but if our tile is now lethal, this same call is what
    # force-steps us off it to safety. "Won't move if it doesn't need to, moves
    # if it does."
    nav.move_adjacent(target)
