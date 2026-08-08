from fcode import *

import comms
import map_info
import pathing
from pathing import Pathing
import units.builder
from log import DRAW_DEBUG, log

rc: Controller = None
nav: Pathing = None

_SHIFT_PLAN_WIDTH = -1
_SHIFT_PLAN_HEIGHT = -1
_GUNNER_STEP_SHIFTS = ()

_GROUP_MASK_CACHE_VERSION = -1
_GROUP_MASK_CACHE_ENEMY = -1
_GUNNER_GROUP_MASKS = ()

_GUNNER_BLOCKED_CACHE_VERSION = -1
_GUNNER_BLOCKED_MASK = 0


_EMPTY_CANDIDATE_MASKS = (0,) * 8

_GUNNER_PER_DIR_CACHE_KEY = None
_GUNNER_PER_DIR_CACHE = None

_round_cache_round = -1
_round_cache_attack_candidates = 0
_round_cache_placement_masks = [None]  # [gunner_masks tuple]
_round_cache_gunner_planes = None      # list of 8 plane-lists, one per direction
_round_cache_can_afford_gun = False



def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav
    _ensure_attack_shift_plans()


# Gunners snipe single high-value lanes: big bonus for core + backline turrets,
# smaller gain on clustered infra (sentinels already out-damage them there).
GUNNER_BUILDING_SCORE = [0] * map_info._NUM_ET
GUNNER_BUILDING_SCORE[map_info._IDX_CORE] = 256
GUNNER_BUILDING_SCORE[map_info._IDX_HARVESTER] = 16
GUNNER_BUILDING_SCORE[map_info._IDX_GUNNER] = 32
GUNNER_BUILDING_SCORE[map_info._IDX_SENTINEL] = 32
GUNNER_BUILDING_SCORE[map_info._IDX_LAUNCHER] = 16
GUNNER_BUILDING_SCORE[map_info._IDX_CONVEYOR] = 6
GUNNER_BUILDING_SCORE[map_info._IDX_BARRIER] = 0
GUNNER_BUILDING_SCORE[map_info._IDX_SPLITTER] = 0

_NON_CORE_TYPE_INDICES = (
    map_info._IDX_GUNNER,
    map_info._IDX_SENTINEL,
    map_info._IDX_LAUNCHER,
    map_info._IDX_HARVESTER,
    map_info._IDX_CONVEYOR,
    map_info._IDX_BARRIER,
    map_info._IDX_SPLITTER,
)

_NUM_PLANES = 10  # max 1023; covers core + boosted conveyor + rotation bonus

SCORE_THRESHOLD_FACTOR = 0.25
MIN_ATTACK_SCORE = 16
THREAT_PENALTY = 8
CORE_CONVEYOR_LANE_BONUS = 64

# Gunner-only knobs. Distance discount: enemy at ray-step k counts as
# round(score * 0.9^k); k=0 is the tile directly in front of the gunner.
# Rotation bonus: each direction at a tile gains (sum_8_directions >> 3),
# approximating 0.1 * total — represents value accessible by rotating.
_DISCOUNT_NUM = 9
_DISCOUNT_DEN = 10
_MAX_DISCOUNT_STEPS = 3  # gunner ray length (vision radius squared 13)
_ROTATION_SHIFT = 3

cant_attack = 0
# cant_attack entries expire: reachability changes as buildings/turrets appear
# and die, so an unreachable-today tile must not be poisoned forever. (Khaos
# kept a permanent blacklist, but its candidates were few local tiles; with
# map-wide candidates one sealed pocket would permanently kill the whole
# attack-placement state.)
_cant_attack_log = []  # (round_added, mask)
CANT_ATTACK_TTL = 60


def _expire_cant_attack(cur_round):
    global cant_attack, _cant_attack_log
    if not _cant_attack_log:
        return
    keep = [(r, m) for r, m in _cant_attack_log if r + CANT_ATTACK_TTL > cur_round]
    if len(keep) != len(_cant_attack_log):
        _cant_attack_log = keep
        ca = 0
        for _, m in keep:
            ca |= m
        cant_attack = ca

# Never place a gunner if it would leave the bank below this — keeps enough for
# an emergency conveyor/barrier/heal after the build.
GUNNER_TI_FLOOR = 8


# ---------------------------------------------------------------------------
# Bit-sliced score plane helpers
# ---------------------------------------------------------------------------

def _bits_of(c):
    """Tuple of bit positions set in c. Pure function; cache at module load."""
    result = []
    x, i = c, 0
    while x:
        if x & 1:
            result.append(i)
        x >>= 1
        i += 1
    return tuple(result)


def _step_score(score, step):
    """Round-half-up integer of score * (_DISCOUNT_NUM / _DISCOUNT_DEN)^step."""
    val = score
    for _ in range(step):
        val = (val * _DISCOUNT_NUM + _DISCOUNT_DEN // 2) // _DISCOUNT_DEN
    return val


def _step_bits_tuple(score):
    """Tuple of bit-position tuples for the discounted score at each ray step."""
    return tuple(_bits_of(_step_score(score, k)) for k in range(_MAX_DISCOUNT_STEPS))


def _build_gunner_score_groups(score_table):
    """Same shape as _build_score_groups, but `bits` is a per-step tuple of
    bit-position tuples — one entry per ray step, holding the bits of the
    discounted score at that step."""
    groups: dict[int, list[int]] = {}
    for t_idx in _NON_CORE_TYPE_INDICES:
        s = score_table[t_idx]
        if s:
            groups.setdefault(s, []).append(t_idx)
    return [(s, _step_bits_tuple(s), tuple(idxs)) for s, idxs in groups.items()]


_GUNNER_SCORE_GROUPS = _build_gunner_score_groups(GUNNER_BUILDING_SCORE)

_THREAT_PENALTY_BITS = _bits_of(THREAT_PENALTY)
_CORE_CONVEYOR_LANE_BONUS_BITS = _bits_of(CORE_CONVEYOR_LANE_BONUS)
_GUN_CORE_BITS_BY_STEP = _step_bits_tuple(GUNNER_BUILDING_SCORE[map_info._IDX_CORE])

def _ensure_attack_shift_plans():
    """Precompute static shift plans used by the hot attack scorers."""
    global _SHIFT_PLAN_WIDTH, _SHIFT_PLAN_HEIGHT
    global _GUNNER_STEP_SHIFTS

    w = map_info._width
    h = map_info._height
    if _SHIFT_PLAN_WIDTH == w and _SHIFT_PLAN_HEIGHT == h:
        return

    shift_masks = map_info._turret_shift_masks

    gunner_plans = []
    for d in range(8):
        dx, dy = map_info._DIRECTION_DELTAS_I[d]
        sdx = -dx
        sdy = -dy
        sm = shift_masks.get((sdx, sdy))
        if sm is None:
            gunner_plans.append((0, 0, 0))
        else:
            gunner_plans.append((sm, sdx + sdy * w, len(map_info._GUNNER_RAYS[d])))

    _GUNNER_STEP_SHIFTS = tuple(gunner_plans)
    _SHIFT_PLAN_WIDTH = w
    _SHIFT_PLAN_HEIGHT = h


def _enemy_score_group_masks(enemy_team_bm):
    global _GROUP_MASK_CACHE_VERSION, _GROUP_MASK_CACHE_ENEMY
    global _GUNNER_GROUP_MASKS

    sv = map_info._struct_version
    if _GROUP_MASK_CACHE_VERSION == sv and _GROUP_MASK_CACHE_ENEMY == enemy_team_bm:
        return None, _GUNNER_GROUP_MASKS

    bm_et = map_info._bm_et
    gunner_groups = []
    for s, bits, idxs in _GUNNER_SCORE_GROUPS:
        bm_group = 0
        for t_idx in idxs:
            bm_group |= bm_et[t_idx]
        bm_group &= enemy_team_bm
        if bm_group:
            gunner_groups.append((s, bits, bm_group))

    _GROUP_MASK_CACHE_VERSION = sv
    _GROUP_MASK_CACHE_ENEMY = enemy_team_bm
    _GUNNER_GROUP_MASKS = tuple(gunner_groups)
    return None, _GUNNER_GROUP_MASKS


def _add_bits_to_planes(planes, bits, mask):
    """Bit-sliced: add the constant whose set bits are `bits` to counters."""
    if not bits or not mask:
        return
    for i in bits:
        carry = planes[i] & mask
        planes[i] ^= mask
        j = i + 1
        while carry and j < _NUM_PLANES:
            new_carry = planes[j] & carry
            planes[j] ^= carry
            carry = new_carry
            j += 1


def _add_planes_into(dst, src):
    """Bit-sliced plane-list sum: dst += src, tile-wise. Full-adder rippled
    across planes; top-plane overflow is discarded. Caller ensures totals fit
    in _NUM_PLANES bits."""
    carry = 0
    for i in range(_NUM_PLANES):
        a = dst[i]
        b = src[i]
        dst[i] = a ^ b ^ carry
        carry = (a & b) | (carry & (a ^ b))


def _read_score(planes, tile_n):
    """Read the integer score stored at `tile_n` across the planes."""
    score = 0
    for i in range(_NUM_PLANES):
        if (planes[i] >> tile_n) & 1:
            score |= 1 << i
    return score


def _max_score_in_mask(planes, mask):
    """Maximum counter value among tiles whose bit is set in `mask`. Bit-parallel."""
    if not mask:
        return 0
    max_val = 0
    cur = mask
    for i in range(_NUM_PLANES - 1, -1, -1):
        hi = planes[i] & cur
        if hi:
            max_val |= 1 << i
            cur = hi
    return max_val


def _ge_threshold_mask(planes, threshold, candidates):
    """Bitmask of tiles in `candidates` whose counter >= `threshold`. Bit-parallel."""
    if threshold <= 0:
        return candidates
    eq = candidates
    gt = 0
    for i in range(_NUM_PLANES - 1, -1, -1):
        p = planes[i]
        if (threshold >> i) & 1:
            eq &= p
        else:
            gt |= eq & p
            eq &= ~p
    return gt | eq

def _get_cached_gunner_per_dir(enemy_team_bm: int, threat: int, gunner_masks: tuple[int, ...]):
    """Gunner per-direction planes, cached across rounds by exact masks."""
    global _GUNNER_PER_DIR_CACHE_KEY, _GUNNER_PER_DIR_CACHE

    key = (
        map_info._struct_version,
        enemy_team_bm,
        threat,
        gunner_masks,
    )
    if key != _GUNNER_PER_DIR_CACHE_KEY:
        _GUNNER_PER_DIR_CACHE = _compute_gunner_dir_scores(
            enemy_team_bm, threat, gunner_masks
        )
        _GUNNER_PER_DIR_CACHE_KEY = key
    return _GUNNER_PER_DIR_CACHE


# ---------------------------------------------------------------------------
# Sentinel: returns 8 plane-lists, one per facing direction
# ---------------------------------------------------------------------------

def _gunner_ray_blocked_mask():
    """Tiles that block a gunner ray: walls + allied non-road, non-marker
    buildings. A gunner can't shoot through its own infrastructure."""
    global _GUNNER_BLOCKED_CACHE_VERSION, _GUNNER_BLOCKED_MASK

    sv = map_info._struct_version
    if _GUNNER_BLOCKED_CACHE_VERSION == sv:
        return _GUNNER_BLOCKED_MASK

    _GUNNER_BLOCKED_MASK = map_info._bm_env[map_info._IDX_ENV_WALL] | map_info._bm_team[map_info._my_team_idx]
    _GUNNER_BLOCKED_CACHE_VERSION = sv
    return _GUNNER_BLOCKED_MASK


def _compute_gunner_dir_scores(enemy_team_bm, threat, gunner_masks):
    """For each of 8 facing directions, compute a per-tile gunner score plane
    list. Returns: list of 8 plane-lists (list[list[int]]). Reading position n
    from the d-th inner list yields the gunner's score if placed at n facing
    direction d — but ONLY if n is a valid placement tile for that direction
    (per `gunner_masks[d]`); otherwise the score reads 0.

    Gunner rays are blocked by walls AND by allied non-road, non-marker
    buildings. Scores come from GUNNER_BUILDING_SCORE, applied with a per-step
    distance discount (round(score * 0.9^k) for an enemy at ray-step k from
    the gunner — k=0 is the adjacent tile). Each gunner tile additionally
    gains a rotation bonus equal to (sum_of_8_directions >> _ROTATION_SHIFT),
    weighting tiles whose other facings carry value too.

    Core is single-counted per gunner per direction at the closest hit step
    (matches prior behavior, just discounted by that step's factor).

    THREAT_PENALTY is baked exactly once per plane at the end on non-threat
    reached placeable tiles using the FINAL non_zero union."""
    bm_et = map_info._bm_et
    _ensure_attack_shift_plans()
    add_bits_to_planes = _add_bits_to_planes
    add_planes_into = _add_planes_into
    num_planes = _NUM_PLANES
    board_mask = map_info._board_mask
    not_blocked = board_mask & ~_gunner_ray_blocked_mask()
    step_shifts = _GUNNER_STEP_SHIFTS
    threat_penalty_bits = _THREAT_PENALTY_BITS
    core_conveyor_bonus_bits = _CORE_CONVEYOR_LANE_BONUS_BITS

    core_mask = bm_et[map_info._IDX_CORE] & enemy_team_bm
    conveyor_mask = bm_et[map_info._IDX_CONVEYOR] & enemy_team_bm
    gun_core_bits_by_step = _GUN_CORE_BITS_BY_STEP
    _, type_initial = _enemy_score_group_masks(enemy_team_bm)

    non_threat = board_mask & ~threat
    non_zero = 0
    all_planes = []
    append_planes = all_planes.append
    n_types = len(type_initial)
    type_bits_by_step_arr = [t[1] for t in type_initial]
    type_bm_initial = [t[2] for t in type_initial]
    type_bms = [0] * n_types
    for d in range(8):
        planes = [0] * num_planes
        mask_d = gunner_masks[d]
        sm, soff, max_step = step_shifts[d]
        if not sm or max_step == 0 or not mask_d:
            append_planes(planes)
            continue
        combined_sm = sm & not_blocked
        core_cur = core_mask
        conveyor_cur = conveyor_mask
        conveyor_seen = 0
        supply_cut_lanes = 0
        for j in range(n_types):
            type_bms[j] = type_bm_initial[j]
        core_seen = 0  # mask_d gunners that already had core scored at a closer step
        if soff >= 0:
            for step in range(max_step):
                if core_cur:
                    core_cur = (core_cur & combined_sm) << soff
                    if core_cur:
                        non_zero |= core_cur
                        first_hits = core_cur & mask_d & ~core_seen
                        if first_hits and gun_core_bits_by_step[step]:
                            supply_cut_lanes |= first_hits & conveyor_seen
                            add_bits_to_planes(planes, gun_core_bits_by_step[step], first_hits)
                            core_seen |= first_hits
                if conveyor_cur:
                    conveyor_cur = (conveyor_cur & combined_sm) << soff
                    conveyor_seen |= conveyor_cur & mask_d
                any_alive = False
                for j in range(n_types):
                    bm_t = type_bms[j]
                    if not bm_t:
                        continue
                    shifted = (bm_t & combined_sm) << soff
                    type_bms[j] = shifted
                    if shifted:
                        any_alive = True
                        non_zero |= shifted
                        restricted = shifted & mask_d
                        if restricted:
                            step_bits = type_bits_by_step_arr[j][step]
                            if step_bits:
                                add_bits_to_planes(planes, step_bits, restricted)
                if not core_cur and not any_alive:
                    break
        else:
            nsoff = -soff
            for step in range(max_step):
                if core_cur:
                    core_cur = (core_cur & combined_sm) >> nsoff
                    if core_cur:
                        non_zero |= core_cur
                        first_hits = core_cur & mask_d & ~core_seen
                        if first_hits and gun_core_bits_by_step[step]:
                            supply_cut_lanes |= first_hits & conveyor_seen
                            add_bits_to_planes(planes, gun_core_bits_by_step[step], first_hits)
                            core_seen |= first_hits
                if conveyor_cur:
                    conveyor_cur = (conveyor_cur & combined_sm) >> nsoff
                    conveyor_seen |= conveyor_cur & mask_d
                any_alive = False
                for j in range(n_types):
                    bm_t = type_bms[j]
                    if not bm_t:
                        continue
                    shifted = (bm_t & combined_sm) >> nsoff
                    type_bms[j] = shifted
                    if shifted:
                        any_alive = True
                        non_zero |= shifted
                        restricted = shifted & mask_d
                        if restricted:
                            step_bits = type_bits_by_step_arr[j][step]
                            if step_bits:
                                add_bits_to_planes(planes, step_bits, restricted)
                if not core_cur and not any_alive:
                    break
        if supply_cut_lanes:
            add_bits_to_planes(
                planes,
                core_conveyor_bonus_bits,
                supply_cut_lanes,
            )
        append_planes(planes)

    # Rotation bonus: add (sum_8_dirs >> _ROTATION_SHIFT) to each direction's
    # plane. Approximates 0.1 * total. Computed before threat penalty so the
    # bonus reflects raw enemy-damage potential, not threat-tile preference.
    summed = [0] * num_planes
    for d_planes in all_planes:
        add_planes_into(summed, d_planes)
    bonus_planes = [0] * num_planes
    for i in range(num_planes - _ROTATION_SHIFT):
        bonus_planes[i] = summed[i + _ROTATION_SHIFT]
    if any(bonus_planes):
        for d_planes in all_planes:
            add_planes_into(d_planes, bonus_planes)

    if threat_penalty_bits:
        baked_base = non_threat & non_zero
        for d, planes in enumerate(all_planes):
            baked = baked_base & gunner_masks[d]
            if baked:
                add_bits_to_planes(planes, threat_penalty_bits, baked)
    return all_planes


def _placement_candidates():
    """Tuple of 8 identical placement bitmasks (one per facing direction).

    Loki placement criteria: any tile that is
      1. seen (known to us — direct observation, symmetry mirror, or comms), and
      2. has no building on it (either team).
    Walls are excluded (unbuildable), as are tiles on a friendly gunner's
    current ray (a turret there would block that gunner's line of fire) and
    tiles in the cant_attack TTL blacklist (recently proven unreachable).
    Facing is unrestricted — direction choice is left to the scorer. Engine
    can_build_gunner, affordability, and score>0 still gate the actual build."""
    _ensure_attack_shift_plans()
    candidates = (
        map_info._bm_seen
        & ~map_info._bm_any_building
        & ~map_info._bm_env[map_info._IDX_ENV_WALL]
        & ~map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        & ~map_info._bm_friendly_bots
        & ~map_info._bm_enemy_bots
        & ~map_info._bm_my_gunner_claims
        & ~cant_attack
    )
    if not candidates:
        return _EMPTY_CANDIDATE_MASKS
    return tuple(candidates for _ in range(8))


def _threshold_attack_candidates(gunner_masks, gun_planes_by_dir):
    """Tiles surviving Hermod's max-score-relative placement threshold."""
    gunner_any = 0
    for mask in gunner_masks:
        gunner_any |= mask
    if not gunner_any:
        return 0

    gun_max = 0
    for d in range(8):
        if gunner_masks[d]:
            sc = _max_score_in_mask(gun_planes_by_dir[d], gunner_masks[d])
            if sc > gun_max:
                gun_max = sc
    if gun_max < MIN_ATTACK_SCORE + THREAT_PENALTY:
        return 0

    threshold = max(
        int(gun_max * SCORE_THRESHOLD_FACTOR),
        MIN_ATTACK_SCORE + THREAT_PENALTY,
    )
    keep = 0
    for d in range(8):
        mask = gunner_masks[d]
        if mask:
            keep |= _ge_threshold_mask(gun_planes_by_dir[d], threshold, mask)
    return gunner_any & keep


def _get_attack_candidates():
    if not _round_cache_can_afford_gun:
        _round_cache_placement_masks[0] = _EMPTY_CANDIDATE_MASKS
        return 0
    enemy_idx = 1 - map_info._my_team_idx
    if not map_info._bm_team[enemy_idx]:
        _round_cache_placement_masks[0] = _EMPTY_CANDIDATE_MASKS
        return 0

    gunner_masks = _placement_candidates()
    _round_cache_placement_masks[0] = gunner_masks
    if not any(gunner_masks):
        return 0

    _ensure_gunner_scores()
    return _threshold_attack_candidates(gunner_masks, _round_cache_gunner_planes)


def _defensive() -> bool:
    """Economy builders run a DEFENSIVE variant of attack placement: identical
    scoring, except the enemy core is never a valid gunner target (so their
    gunners defend our territory / infra rather than siege the enemy base)."""
    return units.builder._economy_builder


def _round_cache_enemy_inputs():
    enemy_idx = 1 - map_info._my_team_idx
    enemy_core = map_info._bm_et[map_info._IDX_CORE] & map_info._bm_team[enemy_idx]
    if _defensive():
        # Defensive attackers never target the enemy core: drop it from the
        # scorable enemy set entirely.
        enemy_team_bm = map_info._bm_team[enemy_idx] & ~map_info._bm_my_gunner_claims & ~enemy_core
    else:
        # Prefer fresh targets by removing everything an allied gunner already
        # covers. Keep the core eligible because one gunner cannot out-damage
        # its health and repairs by itself.
        enemy_team_bm = map_info._bm_team[enemy_idx] & (~map_info._bm_my_gunner_claims | enemy_core)
    threat = (map_info._bm_enemy_soft_threat | map_info._bm_enemy_hard_threat)
    return enemy_team_bm, threat


def _ensure_gunner_scores():
    """Lazily build gunner per-direction score planes once per round."""
    global _round_cache_gunner_planes
    if _round_cache_gunner_planes is not None:
        return
    enemy_team_bm, threat = _round_cache_enemy_inputs()
    gunner_masks = _round_cache_placement_masks[0]
    _round_cache_gunner_planes = _get_cached_gunner_per_dir(enemy_team_bm, threat, gunner_masks)


def _ensure_round_cache():
    global _round_cache_round, _round_cache_attack_candidates
    global _round_cache_gunner_planes
    global _round_cache_can_afford_gun
    r = rc.get_current_round()
    if _round_cache_round == r:
        return
    _round_cache_round = r
    _expire_cant_attack(r)
    _round_cache_gunner_planes = None
    ti = rc.get_global_resources()
    reserve = map_info.builder_ti_reserve()
    _round_cache_can_afford_gun = ti >= rc.get_gunner_cost() + max(reserve, GUNNER_TI_FLOOR)
    _round_cache_attack_candidates = _get_attack_candidates()
    if DRAW_DEBUG and _round_cache_attack_candidates:
        _draw_attack_candidates(_round_cache_attack_candidates)


def _draw_attack_candidates(filtered):
    for p in map_info.iter_mask(filtered):
        rc.draw_indicator_dot(p, 255, 165, 0)


# ---------------------------------------------------------------------------
# Per-tile "best direction / best type" pick
# ---------------------------------------------------------------------------

def get_best_direction(pos):
    w = map_info._width
    n = pos.x + pos.y * w
    bit = 1 << n
    _ensure_gunner_scores()
    gun_planes_by_dir = _round_cache_gunner_planes
    gunner_masks = _round_cache_placement_masks[0]
    directions = map_info._DIRECTIONS
    best_dir, best_score = Direction.NORTH, -1
    for d in range(8):
        if not (gunner_masks[d] & bit):
            continue
        sc = _read_score(gun_planes_by_dir[d], n)
        if sc > best_score:
            best_score = sc
            best_dir = directions[d]
    return best_dir, EntityType.GUNNER, best_score


def _enemy_core_facing(pos: Position, preferred: Direction | None = None):
    """Exact facing from ``pos`` that can hit the enemy core's 2x2 footprint.

    Cardinal rays contain three tiles and diagonal rays contain two, as defined
    by map_info._GUNNER_RAYS. Enemy structures are valid intermediate targets:
    destroying a conveyor in front of the core opens the same lane on a later
    turn. Prefer such supply-cutting lanes, then preserve the scorer's facing.
    """
    core_area = map_info._bm_their_core_area
    if not core_area:
        origin = map_info._their_core or map_info._predicted_enemy_core
        if origin is not None:
            for x in range(origin.x, origin.x + 2):
                for y in range(origin.y, origin.y + 2):
                    target = Position(x, y)
                    if map_info.in_bounds(target):
                        core_area |= 1 << (x + y * map_info._width)
    if not core_area:
        return None

    enemy_idx = 1 - map_info._my_team_idx
    enemy_conveyors = (
        map_info._bm_et[map_info._IDX_CONVEYOR]
        & map_info._bm_team[enemy_idx]
    )
    allied_blockers = map_info._bm_team[map_info._my_team_idx]
    choices = []
    for direction_index, direction in enumerate(map_info._DIRECTIONS):
        clips_conveyor = False
        reaches_core = False
        for dx, dy in map_info._GUNNER_RAYS[direction_index]:
            target = Position(pos.x + dx, pos.y + dy)
            if not map_info.in_bounds(target):
                break
            bit = 1 << (target.x + target.y * map_info._width)
            if (map_info._bm_env[map_info._IDX_ENV_WALL] | allied_blockers) & bit:
                break
            clips_conveyor |= bool(enemy_conveyors & bit)
            if core_area & bit:
                reaches_core = True
                break
        if reaches_core:
            choices.append((clips_conveyor, direction == preferred, direction))
    if not choices:
        return None
    return max(choices, key=lambda item: (item[0], item[1]))[2]


def _my_claims():
    w = map_info._width
    my_mask = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    _ensure_round_cache()
    return pathing.claim_subset(
        my_mask,
        map_info._bm_friendly_bots,
        _round_cache_attack_candidates,
        passable=map_info._bm_passable_FFF,
        tie_self=True,
    )


_cached_claims = 0
target = None  # tile we're placing at / moving to, for status logging
last_gunner_pos = None  # this unit's most recently placed gunner (for atk target)
MAX_SCORE = 9

def score():
    global _cached_claims
    _cached_claims = _my_claims()
    return 9 if _cached_claims else 0


def run():
    global cant_attack, last_gunner_pos
    log("ATTACK")
    preferred = _cached_claims
    if not preferred:
        return

    width = map_info._width
    my_team_idx = map_info._my_team_idx

    best, _ = nav.closest(preferred)
    if best is None:
        cant_attack |= preferred
        _cant_attack_log.append((rc.get_current_round(), preferred))
        return

    launchers = map_info._bm_et[map_info._IDX_LAUNCHER] & map_info._bm_team[my_team_idx]
    barriers = map_info._bm_et[map_info._IDX_BARRIER] & map_info._bm_team[my_team_idx]
    enemy_block = map_info.expand_chebyshev(launchers) | launchers | barriers

    best_lead = None
    best_lead_tile = None
    remaining = preferred
    while remaining:
        cand, my_d = nav.closest_within(remaining, max_dist=2)
        if cand is None:
            break
        remaining &= ~(1 << (cand.x + cand.y * width))
        _, d_to_cand = nav.closest(
            map_info._bm_enemy_bots,
            pos=cand,
            avoid=enemy_block,
            side=False,
        )
        if d_to_cand == -1:
            their_d = 1 << 30
        elif d_to_cand == 0:
            their_d = 1
        else:
            their_d = d_to_cand - 1
        lead = their_d - my_d
        if best_lead is None or lead > best_lead:
            best_lead = lead
            best_lead_tile = cand
    if best_lead_tile is not None:
        best = best_lead_tile

    global target
    target = best
    direction, _, _ = get_best_direction(best)
    if not _defensive():
        core_facing = _enemy_core_facing(best, preferred=direction)
        if core_facing is not None:
            direction = core_facing
    log(f"Attack: best={best}, dir={direction}")
    nav.move_adjacent(best)
    if rc.can_build_gunner(best, direction) and rc.get_global_resources() >= rc.get_gunner_cost() + max(map_info.builder_ti_reserve(), GUNNER_TI_FLOOR):
        rc.build_gunner(best, direction)
        last_gunner_pos = best
        comms.note_gunner_built()
        map_info.update_at(best)
