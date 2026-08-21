"""Shared turret target-priority scoring used by sentinel firing and gunner
rotation decisions.

Priority buckets (lowest number = best):
  1. Enemy sentinel/gunner that threatens one of my sentinels/gunners.
  2. Enemy sentinel/gunner.
  3. Enemy builder bot.
  4. Enemy harvester, or an enemy conveyor carrying ore
     (map_info._bm_ti_carrying — the up/downstream carrying set).
  5. Enemy launcher.
  6. Enemy core (last — chipped only when nothing better is in range).

Anything not in one of these six buckets is not targeted. Tiebreaks within a
bucket: one-shot first, then furthest from the nearest enemy builder bot, then
weight (HP as sub-tiebreak).
"""

from main import has_op
from fcode import EntityType, Position
import map_info
from log import DEBUG_LOGGING, log


def _turret_et_for_idx(idx: int):
    if idx == map_info._IDX_GUNNER:
        return EntityType.GUNNER
    if idx == map_info._IDX_SENTINEL:
        return EntityType.SENTINEL
    return None


def _threatening_enemy_turrets(rc, enemy_turrets: int, my_turrets: int) -> int:
    """Bitmask of enemy sentinels/gunners that would have a legal shot at any of
    my sentinels/gunners on the current map. Uses `rc.can_fire_from`, which for
    gunners enforces first-obstruction LOS through current occupancy and walls,
    so a gunner whose ray is blocked before reaching my turret is correctly NOT
    marked threatening. Sentinels use a pure geometric range/shape check."""
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
    """Return {1: mask, 2: mask, 3: mask, 4: mask, 5: mask} — see module docstring."""
    my_idx = map_info._my_team_idx
    enemy = map_info._bm_team[1 - my_idx]
    mine = map_info._bm_team[my_idx]
    bm_et = map_info._bm_et

    gs = bm_et[map_info._IDX_GUNNER] | bm_et[map_info._IDX_SENTINEL]
    enemy_gs = gs & enemy
    my_gs = gs & mine

    threatening = _threatening_enemy_turrets(rc, enemy_gs, my_gs)

    enemy_bots = map_info._bm_enemy_bots

    enemy_harvesters = bm_et[map_info._IDX_HARVESTER] & enemy
    enemy_carrying_conv = map_info._bm_conveyors & map_info._bm_ti_carrying & enemy

    enemy_launcher = bm_et[map_info._IDX_LAUNCHER] & enemy
    enemy_core = bm_et[map_info._IDX_CORE] & enemy

    return {
        1: threatening,
        2: enemy_gs,
        3: enemy_bots,
        4: enemy_harvesters | enemy_carrying_conv,
        5: enemy_launcher,
        6: enemy_core,
    }


def _apply_tiebreaks(pool, nav, one_shot_hp: int, enemy_bots: int, label: str):
    """Tiebreak within a single pool: one-shot, furthest-from-nearest-enemy-bot,
    then weight (HP as sub-tiebreak). Logs every step."""
    if not pool:
        return None

    if DEBUG_LOGGING:
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
        if DEBUG_LOGGING:
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


def _double_healer_ring(enemy_bots: int) -> int:
    """Tiles adjacent (8-dir) to >=2 enemy builder bots. Bots heal what we shoot,
    so two healers usually out-tempo a single shot; one healer alone we still try
    to outpace, so only >=2 counts."""
    w = map_info._width
    nlc = map_info._not_left_col
    nrc = map_info._not_right_col
    ntr = map_info._not_top_row
    nbr = map_info._not_bottom_row
    b = enemy_bots
    shifts = (
        (b & nlc) >> 1,                  # bot E of tile
        (b & nrc) << 1,                  # bot W of tile
        (b & nbr) << w,                  # bot N of tile
        (b & ntr) >> w,                  # bot S of tile
        (b & nlc & nbr) << (w - 1),      # bot NE
        (b & nrc & nbr) << (w + 1),      # bot NW
        (b & nlc & ntr) >> (w + 1),      # bot SE
        (b & nrc & ntr) >> (w - 1),      # bot SW
    )
    seen_one = 0
    seen_two = 0
    for s in shifts:
        seen_two |= seen_one & s
        seen_one |= s
    return seen_two & ~enemy_bots & map_info._board_mask


def select_best(candidates, priority_sets, nav, one_shot_hp: int,
                bot_ring_mode: str = 'strict',
                ring_override_mask: int = 0):
    """Pick the best candidate from `candidates`. Each candidate is a tuple whose
    first five elements are (tile, n, weight, hp, etype); extra fields (e.g. the
    gunner's direction) pass through unchanged.

    Candidates are bucketed into priorities 1-5 (see module docstring) by tile,
    tried in order; the first non-empty bucket's tiebreak winner is returned.
    Tiles occupied by a friendly builder bot are never fired on. Anything not in
    a priority bucket is skipped.

    A *non-bot* candidate whose tile is adjacent to >=2 enemy builder bots (see
    `_double_healer_ring`) is demoted to a fallback tier tried only after every
    normal pool is empty. `bot_ring_mode` controls this:
      - 'strict'            -> demote unconditionally.
      - 'one_shot_override' -> demote unless the candidate is a one-shot
                               (hp <= one_shot_hp) or its tile is in
                               `ring_override_mask` (sentinel kill-assists).
      - 'off'               -> never demote (gunner).
    Enemy builder bots (priority 3) are never demoted."""
    log(f"select_best: {len(candidates)} raw candidates, one_shot_hp={one_shot_hp}")
    if not candidates:
        log("  empty candidate list")
        return None

    friendly_bots = map_info._bm_friendly_bots
    enemy_bots = map_info._bm_enemy_bots
    bot_ring = _double_healer_ring(enemy_bots) if enemy_bots else 0
    log(f"  bot_ring={'set' if bot_ring else '0'} ring_mode={bot_ring_mode} "
        f"ring_override={'set' if ring_override_mask else '0'}")

    buckets = {1: [], 2: [], 3: [], 4: [], 5: [], 6: []}
    fb_buckets = {1: [], 2: [], 3: [], 4: [], 5: [], 6: []}
    drop_friendly = 0
    for cand in candidates:
        bit = 1 << cand[1]
        if bit & friendly_bots:
            drop_friendly += 1
            continue
        p = None
        for pp in (1, 2, 3, 4, 5, 6):
            if priority_sets[pp] & bit:
                p = pp
                break
        if p is None:
            continue
        demote = False
        if cand[4] != EntityType.BUILDER_BOT and (bit & bot_ring):
            if bot_ring_mode == 'off':
                demote = False
            elif bit & ring_override_mask:
                demote = False
            elif bot_ring_mode == 'one_shot_override' and cand[3] <= one_shot_hp:
                demote = False
            else:
                demote = True
        (fb_buckets if demote else buckets)[p].append(cand)
    log(f"  buckets: p1={len(buckets[1])} p2={len(buckets[2])} p3={len(buckets[3])} "
        f"p4={len(buckets[4])} p5={len(buckets[5])} p6={len(buckets[6])} "
        f"(dropped {drop_friendly} friendly-bot) "
        f"| fb: p1={len(fb_buckets[1])} p2={len(fb_buckets[2])} p3={len(fb_buckets[3])} "
        f"p4={len(fb_buckets[4])} p5={len(fb_buckets[5])} p6={len(fb_buckets[6])}")

    pools_in_order = (
        [(f"p{p}", buckets[p]) for p in (1, 2, 3, 4, 5, 6)]
        + [(f"fb_p{p}", fb_buckets[p]) for p in (1, 2, 3, 4, 5, 6)]
    )
    for label, pool in pools_in_order:
        if not pool:
            continue
        log(f"trying case {label}")
        chosen = _apply_tiebreaks(pool, nav, one_shot_hp, enemy_bots, label)
        if chosen is not None:
            log(f"select_best: case={label} winner=({chosen[0].x},{chosen[0].y}) "
                f"et={chosen[4].value} w={chosen[2]} hp={chosen[3]}")
            return chosen
    log("select_best: no winner")
    return None


def scrap_if_idle(rc) -> bool:
    """Self-destruct a turret that has become dead weight: the caller has nothing
    to shoot this turn and we are tracking no enemy bots anywhere. Returns True
    if it self-destructed. `rc.self_destruct()` refunds the turret's BUILD SCALE
    contribution (+10% gunner/launcher, +20% sentinel) but no titanium.

    NOTE the guard is weaker than it looks, and deliberately left that way:
      * `map_info._bm_enemy_bots` is the team's REMEMBERED GLOBAL enemy-bot set,
        not this turret's own vision.
      * Enemy BUILDINGS are not considered at all. An earlier version of this
        docstring promised "at most 2 enemy buildings in sight"; that check never
        existed, and measurement says it is not worth adding -- we scrap with 3,
        5, 6 and 10 enemy buildings standing and it costs nothing.

    Measured (24-game screens vs the shipped bot, then an 86-game panel):
        implement the "<= 2 buildings" check   -0.0081
        never scrap a turret covering either core ring   +0.0267
        never scrap at all   +0.2767 on a self-play screen, but only +0.0345 on
          86 games, and it SPLITS by opponent: +0.128 vs Tyr_Jython, -0.083 vs
          V6_earlysiege.
    All neutral. The behaviour is worth roughly nothing either way; do not spend
    more on it. 13 scraps in 4 games, 0 of them with an enemy turret still alive
    -- we scrap after the job is done."""
    if map_info._bm_enemy_bots:
        return False
    rc.self_destruct()
    return True
