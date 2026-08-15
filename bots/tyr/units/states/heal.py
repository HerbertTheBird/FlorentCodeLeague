import map_info
import pathing
import units.builder
from fcode import *
from log import log

rc: Controller = None
nav = None

CONV_CHASE_CHEB = 8
ID_MASK = (1 << 12) - 1


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


def _conv_zone():
    # return units.builder._harvest_zone
    """Bitmask of tiles within CONV_CHASE_CHEB pathing distance of my conveyors."""
    my_team_idx = map_info._my_team_idx
    my_convs = map_info._bm_conveyors & map_info._bm_team[my_team_idx]
    my_convs |= map_info._bm_my_core_area
    if not my_convs:
        return 0
    w = map_info._width
    board = (1 << (w * map_info._height)) - 1
    avoid = map_info._bm_blocked
    passable = ~avoid & board
    nlc = map_info._not_left_col
    nrc = map_info._not_right_col
    visited = my_convs
    frontier = my_convs
    for _ in range(CONV_CHASE_CHEB):
        expanded = frontier | ((frontier & nrc) << 1) | ((frontier & nlc) >> 1) | (frontier << w) | (frontier >> w)
        frontier = expanded & passable & ~visited
        visited |= frontier
    return visited




def _turret_covered_mask():
    """Tiles already covered by friendly turrets — cheb-1 of any friendly
    launcher OR on a friendly gunner's current ray. Enemies on these tiles
    don't need a chase from us."""
    my_team_idx = map_info._my_team_idx
    my_launchers = map_info._bm_et[map_info._IDX_LAUNCHER] & map_info._bm_team[my_team_idx]
    launcher_cover = map_info.expand_chebyshev(my_launchers) if my_launchers else 0
    return launcher_cover | map_info._bm_my_gunner_claims




def _find_chase_target(damaged: bool = True):
    # log("find chase")
    """Find an unclaimed enemy builder bot within conv zone. Returns (uid, pos) or None.

    When `damaged` is True, only consider enemies sitting on one of our
    very-damaged buildings; if none, retry with `damaged=False`."""
    w = map_info._width
    # Filter enemy bots in zone, unclaimed
    raw_enemies = map_info._bm_enemy_bots
    cover = _turret_covered_mask()
    units.builder.draw_mask(cover, 255, 0, 0)
    covered_enemies = raw_enemies & cover
    if covered_enemies:
        positions = []
        m = covered_enemies
        while m:
            lsb = m & -m
            n = lsb.bit_length() - 1
            m ^= lsb
            positions.append(Position(n % w, n // w))
        log("turret-covered enemies filtered:", positions)
    enemy_bots = raw_enemies & ~cover

    if not enemy_bots:
        log("no enemies")
        return None

    if damaged:
        enemy_bots = enemy_bots & _very_damaged_targets()
        if not enemy_bots:
            return _find_chase_target(damaged=False)

    friendly_bots = map_info._bm_friendly_bots
    my_bit = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    other_friendly = friendly_bots & ~my_bit

    filtered = enemy_bots
    # Expand the enemy zone once and pre-filter the friendlies we iterate.
    # A friendly outside enemy_zone_4 has no enemy within 4 chebyshev, so
    # the per-friendly expansion below would be a no-op.
    enemy_zone_4 = map_info.expand_manhattan(enemy_bots, 4)
    mask = friendly_bots & ~my_bit & map_info._bm_visible & enemy_zone_4

    while mask:
        lsb = mask & -mask
        n = lsb.bit_length() - 1
        friend_zone = map_info.expand_manhattan(lsb, 4)
        nearby = filtered & friend_zone
        if not nearby:
            mask ^= lsb
            continue
        closest = nav.closest_within(nearby, lsb, 4)
        if closest[0]:
            log("filtering", closest[0], "because", n%w, n//2, closest[1])
            filtered ^= (1<<(closest[0].x+closest[0].y*w))
        # uid = map_info._bot_at.get(n)
        # if uid is not None:
        #     # if (uid & ID_MASK) not in claimed:
        #     #     filtered |= lsb
        #     # else:
        #     nearby_friendly = map_info.expand_chebyshev(lsb, 2) & other_friendly
        #     if not nearby_friendly:
        #         filtered |= lsb
        mask ^= lsb
    # log(map_info._bot_pos)
    # units.builder.draw_mask(enemy_bots, 255, 0, 0)
    # units.builder.draw_mask(friendly_bots, 0, 255, 0)

    if not filtered:
        filtered = enemy_bots
        return None

    nearby = filtered & map_info.expand_manhattan(my_bit, 8)
    if not nearby:
        log("too far")
        return None

    # Enumerate all reachable enemies within max_dist=8 by repeatedly removing
    # the previous closest. Then tiebreak the minimum-BFS-distance set by
    # chebyshev distance to my conveyors (lowest priority — closer wins).
    remaining = nearby
    enumerated = []  # list of (bfs_dist, pos)
    while remaining:
        pos, d = nav.closest_within(remaining, max_dist=8)
        if pos is None:
            break
        enumerated.append((d, pos))
        remaining ^= 1 << (pos.x + pos.y * w)
    if not enumerated:
        log("no closest")
        return None
    # min_d = min(d for d, _ in enumerated)
    tied = [p for d, p in enumerated]
    # if len(tied) == 1:
    #     return tied[0]
    my_convs = map_info._bm_conveyors & map_info._bm_team[map_info._my_team_idx]
    best = None
    best_cd = None
    for p in tied:
        cd = _conv_dist(1 << (p.x + p.y * w), my_convs)
        if best is None or cd < best_cd:
            best = p
            best_cd = cd
    return best


def _healable_mask():
    """Friendly buildings missing at least the full 4 HP a heal restores."""
    my_team_idx = map_info._my_team_idx
    return map_info._bm_team[my_team_idx] & map_info._bm_very_damaged


def _mutual_sentinel_threat():
    """Bitmask of MY sentinels that can shoot an enemy sentinel which can also
    shoot them back. Treated as 'very damaged' for heal priority so we rush in
    to keep them alive through the trade. Sentinels already adjacent (cheb 1)
    to a friendly builder bot are excluded — they're already covered."""
    my_idx = map_info._my_team_idx
    enemy_idx = 1 - my_idx
    my_sents = map_info._bm_et[map_info._IDX_SENTINEL] & map_info._bm_team[my_idx]
    my_sents &= ~map_info.expand_manhattan(map_info._bm_friendly_bots)
    enemy_sents = map_info._bm_et[map_info._IDX_SENTINEL] & map_info._bm_team[enemy_idx]
    if not my_sents or not enemy_sents:
        return 0
    w = map_info._width
    h = map_info._height
    bm_dir = map_info._bm_dir
    OFFSETS = map_info._SENTINEL_OFFSETS

    enemy_dir_at = {}
    m = enemy_sents
    while m:
        lsb = m & -m
        en = lsb.bit_length() - 1
        m ^= lsb
        for di in range(8):
            if bm_dir[di] & lsb:
                enemy_dir_at[en] = di
                break

    result = 0
    m = my_sents
    while m:
        lsb = m & -m
        mn = lsb.bit_length() - 1
        m ^= lsb
        my_x, my_y = mn % w, mn // w
        my_di = None
        for di in range(8):
            if bm_dir[di] & lsb:
                my_di = di
                break
        if my_di is None:
            continue
        attack_mask = 0
        for dx, dy in OFFSETS[my_di]:
            tx, ty = my_x + dx, my_y + dy
            if 0 <= tx < w and 0 <= ty < h:
                attack_mask |= 1 << (tx + ty * w)
        hit_enemies = attack_mask & enemy_sents
        if not hit_enemies:
            continue
        he = hit_enemies
        while he:
            elsb = he & -he
            en = elsb.bit_length() - 1
            he ^= elsb
            edi = enemy_dir_at.get(en)
            if edi is None:
                continue
            ex, ey = en % w, en // w
            for dx, dy in OFFSETS[edi]:
                if ex + dx == my_x and ey + dy == my_y:
                    result |= lsb
                    break
            if result & lsb:
                break
    return result


def _very_damaged_targets():
    """Bitmask of friendly buildings missing >=4 HP, plus any friendly sentinel
    locked in a mutual-shot exchange with an enemy sentinel."""
    base = _healable_mask() & map_info._bm_very_damaged & ~map_info._bm_my_core_area & map_info._bm_visible
    return (base
            | (_mutual_sentinel_threat() & _healable_mask() & map_info._bm_visible)
            | _hurt_core())


# The core is masked out of the tier-0 pool above, so a conveyor missing 3 HP
# outranks a core missing 300 for every builder standing next to both. Measured:
# on twins exactly 1 of 102 heals landed on the core, and on fjord we died at
# turn 65 having healed nothing at all. Repair is 4 HP for 1 titanium, so
# restoring a 500 HP core costs ~125 Ti -- the cheapest survivability in the
# game, on the one building whose loss ends the match. After a siege is broken
# the core is usually the only thing still badly hurt and nothing goes back in.
#
# Worth 3 points on its own (53.0% vs Champion_v47).
CORE_HEAL_MISSING = 60

def _hurt_core() -> int:
    core = map_info._bm_my_core_area & map_info._bm_damaged & map_info._bm_visible
    if not core:
        return 0
    w = map_info._width
    full = map_info._MAX_HP_BY_IDX[map_info._IDX_CORE]
    # A leased repairer heals core chip damage immediately. Ordinary economic
    # builders retain the old 60-HP emergency threshold so a stray scratch does
    # not pull the entire workforce off a nearly complete supply route.
    threshold = 4 if units.builder._repair_assigned else CORE_HEAL_MISSING
    for p in map_info.iter_mask(core):
        n = p.x + p.y * w
        if map_info._building_et_idx[n] == map_info._IDX_CORE and full - map_info._building_hp[n] >= threshold:
            return core
    return 0


def _heal_targets():
    """Bitmask of friendly buildings on which healing wastes no HP."""
    return _healable_mask() & map_info._bm_damaged & ~_very_damaged_targets()


_cached_chase_target = None  # retained for compatibility; Tyr_v1 does not chase enemies

# Route completion still wins over ordinary repair, but badly damaged
# infrastructure is now an emergency and any visible building missing a full
# heal creates a real job. Enemy builders no longer create one: following a mobile
# enemy repeatedly pulled builders away from unfinished supply lines.
MAX_SCORE = 12
NORMAL_HEAL_SCORE = 10


def _service_targets(urgent: bool) -> int:
    targets = _very_damaged_targets() if urgent else _heal_targets()
    targets &= map_info._bm_visible & ~map_info._bm_enemy_bots
    if units.builder._stay_near_core:
        targets &= units.builder.near_core_mask()
    if not targets:
        return 0
    w = map_info._width
    my_pos = map_info._my_pos
    my_bit = 1 << (my_pos.x + my_pos.y * w)
    return pathing.claim_subset(
        my_bit, map_info._bm_friendly_bots, targets, tie_self=True,
    )


def score():
    global _cached_chase_target
    _cached_chase_target = None
    if _service_targets(True):
        return MAX_SCORE
    if _service_targets(False):
        return NORMAL_HEAL_SCORE
    return 0


# def _try_barrier_dead_ends():
#     """Barrier any adjacent tiles that are dead-end conveyor targets."""
#     w = map_info._width
#     dead_ends = map_info._bm_dead_end
#     if not dead_ends:
#         return
#     # Only dead-end conveyors whose output is empty / marker / enemy building
#     my_team_idx = map_info._my_team_idx
#     enemy_idx = 1 - my_team_idx
#     enemy_any = map_info._bm_team[enemy_idx]
#     marker = map_info._bm_et[map_info._IDX_MARKER]
#     empty_mask = ~map_info._bm_any_building & ~map_info._bm_env[map_info._IDX_ENV_WALL]

#     targets = 0
#     mask = dead_ends
#     conv_target = map_info._building_conv_target
#     tiles = w * map_info._height
#     while mask:
#         lsb = mask & -mask
#         n = lsb.bit_length() - 1
#         tn = conv_target[n]
#         if tn and 0 <= tn < tiles:
#             tbit = 1 << tn
#             if (empty_mask & tbit) or (marker & tbit) or (enemy_any & tbit):
#                 targets |= lsb
#         mask ^= lsb
#     if not targets:
#         return
#     my_pos = map_info._my_pos
#     for d in map_info._DIRECTIONS:
#         dx, dy = map_info._DIRECTION_DELTAS[d]
#         p = Position(my_pos.x + dx, my_pos.y + dy)
#         if not map_info.in_bounds(p):
#             continue
#         pbit = 1 << (p.x + p.y * w)
#         if not (targets & pbit):
#             continue
#         if rc.get_action_cooldown() == 0:
#             if rc.can_destroy(p):
#                 rc.destroy(p)
#                 map_info.update_at(p)
#         if rc.can_build_barrier(p):
#             rc.build_barrier(p)
#             map_info.update_at(p)
#             return

_HEAL_PRIORITY = [1] * 16  # default low priority for unknown types
_HEAL_PRIORITY[map_info._IDX_BARRIER] = 2
_HEAL_PRIORITY[map_info._IDX_SPLITTER] = 2
_HEAL_PRIORITY[map_info._IDX_CONVEYOR] = 3
_HEAL_PRIORITY[map_info._IDX_HARVESTER] = 4
_HEAL_PRIORITY[map_info._IDX_GUNNER] = 5
_HEAL_PRIORITY[map_info._IDX_SENTINEL] = 5
_HEAL_PRIORITY[map_info._IDX_LAUNCHER] = 5
_HEAL_PRIORITY[map_info._IDX_CORE] = 6


def _conv_dist(pbit: int, source: int, cap: int = 12) -> int:
    """Chebyshev distance from `source` to the tile bit `pbit` via slow
    iterated bitwise expansion. Returns `cap + 1` if not reached within cap."""
    if not source:
        return cap + 1
    if pbit & source:
        return 0
    cur = source
    for d in range(1, cap + 1):
        cur = map_info.expand_manhattan(cur)
        if cur & pbit:
            return d
    return cap + 1


def _do_best_heal():
    """Heal the most-damaged adjacent friendly building. Mirrors the run-time
    pool ordering: tier 0 = _very_damaged_targets(), tier 1 = _heal_targets()
    (any other friendly missing at least 4 HP). Within a tier, tiebreak by
    damage * _HEAL_PRIORITY[et_idx]."""
    w = map_info._width
    h = map_info._height
    healable = _healable_mask() & map_info._bm_damaged
    very_damaged = _very_damaged_targets()
    best_heal = None
    best_tier = 99
    best_score = -1
    my_pos = map_info._my_pos
    my_x = my_pos.x
    my_y = my_pos.y
    for dx, dy in map_info._DIRECTION_DELTAS_I:
        if dx != 0 and dy != 0:
            continue
        if dx == 0 and dy == 0:
            continue
        x = my_x + dx
        y = my_y + dy
        if not (0 <= x < w and 0 <= y < h):
            continue
        n = x + y * w
        pbit = 1 << n
        if not (healable & pbit):
            continue
        p = Position(x, y)
        if not rc.can_heal(p):
            continue
        hp = map_info._building_hp[n]
        et_idx = map_info._building_et_idx[n]
        if et_idx < 0:
            continue
        damage = map_info._MAX_HP_BY_IDX[et_idx] - hp
        # Defensive action-time gate: state scoring and an earlier builder's
        # heal may have changed the target since selection. Never spend 1 Ti
        # unless this action can restore the full 4 HP.
        if damage < 4:
            continue
        score = damage * _HEAL_PRIORITY[et_idx]
        tier = 0 if (pbit & very_damaged) else 1
        if tier < best_tier or (tier == best_tier and score > best_score):
            best_tier = tier
            best_score = score
            best_heal = p
    if best_heal is not None:
        log("heal: do_best_heal", best_heal, "tier", best_tier, "score", best_score)
        rc.heal(best_heal)


def _try_chase(target):
    """Run chase logic for `target`. Returns True if it took an action and
    run() should return."""
    w = map_info._width
    avoid = map_info.get_avoid(False)
    en_n = target.x + target.y * w
    adj = map_info.expand_manhattan(1 << en_n) & ~(1 << en_n) & ~avoid
    reach_pos, _ = nav.closest_within(adj, max_dist=8, avoid=avoid)
    if reach_pos is not None:
        log("heal: chase target", target, "reachable via", reach_pos)
        nav.move_to(target)
        _do_best_heal()
        return True
    log("heal: chase target", target, "unreachable, no launcher T1 cached")
    return False


def _chase_on_damaged_conv(target) -> bool:
    if target is None:
        return False
    w = map_info._width
    my_team_idx = map_info._my_team_idx
    en_bit = 1 << (target.x + target.y * w)
    my_dam_convs = (map_info._bm_conveyors
                    & map_info._bm_team[my_team_idx]
                    & map_info._bm_damaged)
    return bool(en_bit & my_dam_convs)


def run():
    log("HEAL")
    urgent = _service_targets(True)
    targets = urgent if urgent else _service_targets(False)
    if not targets:
        _do_best_heal()
        return
    best, dist = nav.closest(targets)
    if best is not None:
        log("heal: service", "urgent" if urgent else "normal", best, "dist", dist)
        nav.move_adjacent(best, avoid_turret=not bool(urgent))
    _do_best_heal()
