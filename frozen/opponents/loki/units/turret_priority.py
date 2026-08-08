"""Shared turret target-priority scoring used by sentinel firing and gunner
rotation decisions.

Titan priorities (lowest number = best). Conveyors CANNOT feed turrets in
Titan (turrets draw from the global ammo pool), so all of Cambridge's
feeder-chain reasoning is gone — what conveyors still do is carry the
enemy's titanium income to their core, so cutting lines is anti-economy:
  1. Threatening enemy turrets (a legal shot on one of my turrets right now).
  2. Other enemy turrets.
  3. Enemy harvesters (their income source).
  4. Enemy conveyors / splitters (their income lines).
  5. Enemy barriers or conveyor types cardinally adjacent to any harvester.
  6. Anything else with positive weight.

Nothing is "protected": there is no friendly feed pipeline the enemy could
be part of.

Tiebreaks within a bucket: one-shot first, then furthest from nearest enemy
builder bot, then weight.
"""

from fcode import EntityType, Position
import map_info
from log import log, DRAW_DEBUG


_TURRET_IDX_TO_ET = None


def _turret_et_for_idx(idx: int):
    global _TURRET_IDX_TO_ET
    if _TURRET_IDX_TO_ET is None:
        _TURRET_IDX_TO_ET = {
            map_info._IDX_GUNNER: EntityType.GUNNER,
            map_info._IDX_SENTINEL: EntityType.SENTINEL,
        }
    return _TURRET_IDX_TO_ET.get(idx)


def _enemy_turrets_mask() -> int:
    enemy_team = map_info._bm_team[1 - map_info._my_team_idx]
    return ((map_info._bm_et[map_info._IDX_GUNNER]
             | map_info._bm_et[map_info._IDX_SENTINEL]) & enemy_team)


def _my_turrets_mask() -> int:
    return map_info._bm_et[map_info._IDX_GUNNER] & map_info._bm_team[map_info._my_team_idx]


def _threatening_enemy_turrets(rc, enemy_turrets: int, my_turrets: int) -> int:
    """Bitmask of enemy turrets that would have a legal shot at any of my
    turrets on the current map. Uses `rc.can_fire_from`, which for gunners
    enforces first-obstruction LOS through current occupancy and walls — so
    a gunner whose ray is blocked by a wall or another building before
    reaching my turret is correctly NOT marked threatening. Sentinels and
    breaches still use a pure geometric range/shape check."""
    if not enemy_turrets or not my_turrets:
        return 0
    w = map_info._width

    my_positions = []
    m = my_turrets
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        my_positions.append(Position(n % w, n // w))

    result = 0
    m = enemy_turrets
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        et = _turret_et_for_idx(map_info._building_et_idx[n])
        if et is None:
            continue
        bid = map_info._building_id[n]
        if bid is None:
            continue
        try:
            d = rc.get_direction(bid)
        except Exception:
            continue
        pos = Position(n % w, n // w)
        for mp in my_positions:
            if rc.can_fire_from(pos, d, et, mp):
                result |= lsb
                break
    return result


def compute_priority_sets(rc) -> dict:
    """Return {1..5: mask} — see module docstring."""
    enemy_team = map_info._bm_team[1 - map_info._my_team_idx]
    bm_et = map_info._bm_et

    enemy_turrets = _enemy_turrets_mask()
    my_turrets = _my_turrets_mask()
    threatening = _threatening_enemy_turrets(rc, enemy_turrets, my_turrets)

    conv_types = bm_et[map_info._IDX_CONVEYOR] | bm_et[map_info._IDX_SPLITTER]
    all_harvesters = bm_et[map_info._IDX_HARVESTER]
    harvester_cardinal = map_info.expand_manhattan(all_harvesters) & ~all_harvesters

    p1 = threatening
    p2 = enemy_turrets & ~threatening
    p3 = all_harvesters & enemy_team
    p4 = conv_types & enemy_team
    p5 = (bm_et[map_info._IDX_BARRIER] | conv_types) & enemy_team & harvester_cardinal

    return {1: p1, 2: p2, 3: p3, 4: p4, 5: p5}

def _apply_tiebreaks(pool, nav, one_shot_hp: int, enemy_bots: int, label: str):
    """Tiebreak within a single pool: one-shot, furthest-from-nearest-enemy-bot,
    then weight (HP as sub-tiebreak). Logs every step."""
    if not pool:
        return None

    def _dist(c):
        if not enemy_bots:
            return None
        _, d = nav.closest(enemy_bots, pos=c[0])
        return d  # -1 means unreachable

    def _fmt_dist(d):
        if d is None:
            return "n/a"
        if d == -1:
            return "inf"
        return str(d)

    log(f"  [{label}] pool size={len(pool)}: " + ", ".join(
        f"({c[0].x},{c[0].y}) et={c[4].value} w={c[2]} hp={c[3]} d={_fmt_dist(_dist(c))}"
        for c in pool
    ))
    one_shots = [c for c in pool if c[3] <= one_shot_hp]
    if one_shots:
        log(f"  [{label}] one-shot filter ({one_shot_hp}): {len(one_shots)}/{len(pool)} kept")
        pool = one_shots
    if len(pool) == 1:
        c = pool[0]
        log(f"  [{label}] sole survivor → ({c[0].x},{c[0].y}) et={c[4].value}")
        return pool[0]
    if enemy_bots:
        scored = []
        for c in pool:
            _, dist = nav.closest(enemy_bots, pos=c[0])
            if dist == -1:
                dist = 1 << 30
            scored.append((dist, c))
        log(f"  [{label}] dist-to-enemy-bot: " + ", ".join(
            f"({c[0].x},{c[0].y})={d if d < (1<<29) else 'inf'}" for d, c in scored
        ))
        max_dist = max(s[0] for s in scored)
        pool = [c for d, c in scored if d == max_dist]
        log(f"  [{label}] furthest-from-bot ({max_dist if max_dist < (1<<29) else 'inf'}): {len(pool)} kept")
        if len(pool) == 1:
            c = pool[0]
            log(f"  [{label}] sole survivor → ({c[0].x},{c[0].y}) et={c[4].value}")
            return pool[0]
    if any(c[3] <= one_shot_hp for c in pool):
        pool.sort(key=lambda c: (-c[2], -c[3]))
        log(f"  [{label}] sort by (-weight, -hp) for one-shot")
    else:
        pool.sort(key=lambda c: (-c[2], c[3]))
        log(f"  [{label}] sort by (-weight, hp)")
    c = pool[0]
    log(f"  [{label}] picked → ({c[0].x},{c[0].y}) et={c[4].value} w={c[2]} hp={c[3]}")
    return pool[0]


def select_best(candidates, priority_sets, nav, one_shot_hp: int):
    """Pick the best candidate from `candidates`. Each candidate is a tuple
    whose first five elements are (tile, n, weight, hp, etype); extra fields
    (e.g. direction for the gunner) are passed through unchanged.

    Pool order tried: priorities 1, 2, 3, 4, then enemy builder bots, then
    priorities 5, 6. Within each pool the same tiebreak chain is applied:
    one-shot, furthest-from-nearest-bot, weight.
    """
    log(f"select_best: {len(candidates)} raw candidates, one_shot_hp={one_shot_hp}")
    if not candidates:
        log("  empty candidate list")
        return None

    enemy_bots = map_info._bm_enemy_bots
    friendly_bots = map_info._bm_friendly_bots

    bot_pool = []
    non_bots = []
    drop_friendly = 0
    for cand in candidates:
        if (1 << cand[1]) & friendly_bots:
            drop_friendly += 1
        elif cand[4] == EntityType.BUILDER_BOT:
            bot_pool.append(cand)
        else:
            non_bots.append(cand)
    log(f"  prefilter: {len(non_bots)} non-bots, {len(bot_pool)} bots, "
        f"dropped {drop_friendly} friendly-bot")

    def _bucket_for(cand):
        bit = 1 << cand[1]
        for p in (1, 2, 3, 4, 5):
            if priority_sets[p] & bit:
                return p
        if cand[2] <= 0:
            return None
        return 6

    buckets = {1: [], 2: [], 3: [], 4: [], 5: [], 6: []}
    for cand in non_bots:
        b = _bucket_for(cand)
        if b is not None:
            buckets[b].append(cand)
    log(f"  buckets: p1={len(buckets[1])} p2={len(buckets[2])} p3={len(buckets[3])} "
        f"p4={len(buckets[4])} bots={len(bot_pool)} p5={len(buckets[5])} p6={len(buckets[6])}")

    pools_in_order = (
        ('p1', buckets[1]),
        ('p2', buckets[2]),
        ('p3', buckets[3]),
        ('p4', buckets[4]),
        ('bots', bot_pool),
        ('p5', buckets[5]),
        ('p6', buckets[6]),
    )
    for label, pool in pools_in_order:
        if not pool:
            continue
        chosen = _apply_tiebreaks(pool, nav, one_shot_hp, enemy_bots, label)
        if chosen is not None:
            log(f"select_best: case={label} winner=({chosen[0].x},{chosen[0].y}) "
                f"et={chosen[4].value} w={chosen[2]} hp={chosen[3]}")
            return chosen
    log("select_best: no winner")
    return None
