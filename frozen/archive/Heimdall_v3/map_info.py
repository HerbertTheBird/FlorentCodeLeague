from __future__ import annotations
from fcode import Controller, Position, Environment, EntityType, Team, Direction, ResourceType, GameError, GameConstants
import pathing
import units.builder as builder
import comms
from log import log

_HAS_DIRECTION = frozenset({
    EntityType.CONVEYOR,
    EntityType.GUNNER,
    EntityType.SENTINEL,
    EntityType.SPLITTER,
})

_CONVEYOR_TYPES = frozenset({
    EntityType.CONVEYOR,
    EntityType.SPLITTER,
})

_ET_INT =   {t: i for i, t in enumerate(EntityType)}
_INT_ET =   {i: t for i, t in enumerate(EntityType)}
_NUM_ET = len(_ET_INT)

_ET_CORE        = EntityType.CORE
_ET_BUILDER_BOT = EntityType.BUILDER_BOT

_ENV_INT =  {t: i for i, t in enumerate(Environment)}
_DIR_INT =  {t: i for i, t in enumerate(Direction)}
_INT_DIR =  {i: t for i, t in enumerate(Direction)}
_TM_INT =   {t: i for i, t in enumerate(Team)}
_INT_TM =   {i: t for i, t in enumerate(Team)}

# Pre-computed indices for fast list access (real types)
_IDX_CONVEYOR          = _ET_INT[EntityType.CONVEYOR]
_IDX_SPLITTER          = _ET_INT[EntityType.SPLITTER]
_IDX_CORE              = _ET_INT[EntityType.CORE]
_IDX_HARVESTER         = _ET_INT[EntityType.HARVESTER]
_IDX_BARRIER           = _ET_INT[EntityType.BARRIER]
_IDX_GUNNER            = _ET_INT[EntityType.GUNNER]
_IDX_SENTINEL          = _ET_INT[EntityType.SENTINEL]
_IDX_LAUNCHER          = _ET_INT[EntityType.LAUNCHER]

_MAX_HP_BY_IDX = [0] * _NUM_ET
_MAX_HP_BY_IDX[_IDX_CONVEYOR]           = GameConstants.CONVEYOR_MAX_HP
_MAX_HP_BY_IDX[_IDX_SPLITTER]           = GameConstants.SPLITTER_MAX_HP
_MAX_HP_BY_IDX[_IDX_HARVESTER]          = GameConstants.HARVESTER_MAX_HP
_MAX_HP_BY_IDX[_IDX_BARRIER]            = GameConstants.BARRIER_MAX_HP
_MAX_HP_BY_IDX[_IDX_GUNNER]             = GameConstants.GUNNER_MAX_HP
_MAX_HP_BY_IDX[_IDX_SENTINEL]           = GameConstants.SENTINEL_MAX_HP
_MAX_HP_BY_IDX[_IDX_LAUNCHER]           = GameConstants.LAUNCHER_MAX_HP
_MAX_HP_BY_IDX[_IDX_CORE]               = GameConstants.CORE_MAX_HP

_IDX_ENV_EMPTY  = _ENV_INT[Environment.EMPTY]
_IDX_ENV_WALL   = _ENV_INT[Environment.WALL]
_IDX_ENV_ORE_TI = _ENV_INT[Environment.ORE_TITANIUM]
_NUM_TEAM = len(Team)
_NUM_ENV  = len(Environment)

# Bool lookup tables indexed by et_idx — avoid frozenset hashing in hot paths
_IS_CONVEYOR = [False] * _NUM_ET
for _e in _CONVEYOR_TYPES: _IS_CONVEYOR[_ET_INT[_e]] = True

_HAS_DIR = [False] * _NUM_ET
for _e in _HAS_DIRECTION: _HAS_DIR[_ET_INT[_e]] = True

_IS_BLOCKED = [False] * _NUM_ET
for _e in (EntityType.HARVESTER, EntityType.GUNNER,
           EntityType.SENTINEL, EntityType.LAUNCHER):
    _IS_BLOCKED[_ET_INT[_e]] = True
_DIRECTIONS = (
    Direction.NORTH, Direction.NORTHEAST, Direction.EAST, Direction.SOUTHEAST,
    Direction.SOUTH, Direction.SOUTHWEST, Direction.WEST, Direction.NORTHWEST,
)
_DIRECTION_DELTAS = {d: d.delta() for d in Direction}
# Int-indexed version: _DIRECTION_DELTAS_I[dir_int] = (dx, dy)
_DIRECTION_DELTAS_I = [d.delta() for d in Direction]

_DIR_N  = _DIR_INT[Direction.NORTH]
_DIR_E  = _DIR_INT[Direction.EAST]
_DIR_S  = _DIR_INT[Direction.SOUTH]
_DIR_W  = _DIR_INT[Direction.WEST]

def pos_add(pos: Position, d: Direction) -> Position:
    """Fast Position.add() replacement using cached deltas."""
    dx, dy = _DIRECTION_DELTAS[d]
    return Position(pos.x + dx, pos.y + dy)

def direction_to(src: Position, dst: Position) -> Direction:
    """Fast nearest-octant replacement for Position.direction_to()."""
    dx = dst.x - src.x
    dy = dst.y - src.y
    if dx == 0 and dy == 0:
        return Direction.CENTRE

    ax = dx if dx >= 0 else -dx
    ay = dy if dy >= 0 else -dy

    # tan(22.5 deg) ~= 0.41421356, using integer math to avoid trig.
    if ay * 100000 <= ax * 41422:
        return Direction.EAST if dx > 0 else Direction.WEST
    if ax * 100000 <= ay * 41422:
        return Direction.SOUTH if dy > 0 else Direction.NORTH
    if dx > 0:
        return Direction.SOUTHEAST if dy > 0 else Direction.NORTHEAST
    return Direction.SOUTHWEST if dy > 0 else Direction.NORTHWEST

_rc: Controller
_width = _height = 0

# Reserve enough Ti before any builder-bot build action that we can still
# spawn another builder bot afterwards. Constant is the base bot Ti cost;
# scaled at call time by the team's current cost scale.
BUILDER_BOT_TI_RESERVE = 0


def builder_ti_reserve() -> float:
    return BUILDER_BOT_TI_RESERVE * _rc.get_scale_percent() / 100

_prev_pos: Position = None
_my_pos: Position = None           # cached rc.get_position(), updated on move
_my_team: Team = None
_my_team_idx: int = 0

# Per-tile arrays (scalar values that can't be bitmasks)
_building_id: list[int] = []
_building_et_idx: list[int] = []
_building_hp: list[int] = []
_building_dir: list[int] = []
_building_conv_target: list[int] = []
_conv_reverse: list[int] = []   # reverse[tn] = bitmask of conveyor-type buildings (either team) with any output to tile tn

# Bitmask lists indexed by _ET_INT / _TM_INT / _ENV_INT
_bm_et: list[int] = []      # one bitmask per EntityType
_bm_team: list[int] = []    # one bitmask per Team
_bm_env: list[int] = []     # one bitmask per Environment
_bm_seen: int = 0           # seen tiles (observed OR derived via symmetry)
_bm_any_building: int = 0   # union of all tracked building bitmasks
_bm_dir: list[int] = []   # per facing

# Derived bitmasks
_bm_blocked: int = 0            # walls + non-passable buildings + enemy core area
_bm_conveyors: int = 0          # all conveyor-type buildings
_bm_conveyor_targets: int = 0   # output target tiles of conveyors
_bm_my_core_area: int = 0       # my core 2x2 footprint (Titan; update only in update)
_bm_their_core_area: int = 0    # enemy core 2x2 footprint
_bm_enemy_launch_adj: int = 0   # tiles adjacent to enemy launchers (update only in update)
_bm_route_targets: int = 0      # tiles route state can path toward (update only in update)
_bm_conv_ti: int = 0            # conveyors observed containing titanium
_bm_dead_end: int = 0           # possible places to route from: targets of any conveyor heading into nothing or a building that is not (conveyor type, my core, my sentinel, my gunner). also includes my conveyors pointing into an enemy building (update only in update)
_bm_enemy_soft_threat: int = 0    # tiles enemy sentinels can shoot (low dps) (update only in update)
_bm_enemy_hard_threat: int = 0    # tiles enemy gunners/breaches can shoot (high dps) (update only in update)
_bm_my_gunner_claims: int = 0     # tiles already covered by one of my gunners' current ray (update only in update)
_bm_conv_into_open_ore: int = 0   # CONVEYOR|ARMOURED_CONVEYOR tiles whose target is an open (non-landlocked) ore tile
_bm_conv_by_dir: list[int] = [0] * 8  # per facing: CONVEYOR|ARMOURED_CONVEYOR tiles with that direction
_bm_enemy_turret_threat: int = 0 # union of enemy soft + hard threat
_bm_others_3x3: int = 0          # 3x3 around other friendly builder bots
_bm_passable_FFF: int = 0        # cached (_board_mask & ~get_avoid(False, False, False))

# Structural state version — bumped on any structural map change (build/destroy
# of a tracked building, or symmetry-solved insertion). Used to cheaply
# invalidate caches that only change on structural updates. HP /
# loaded-resource transitions are NOT counted.
_struct_version: int = 0
_board_mask: int = 0              # (1 << (w*h)) - 1, cached
_bm_visible: int = 0              # tiles visible this turn
_nearby_tiles: list = []           # cached rc.get_nearby_tiles() for this round
_bm_damaged: int = 0              # buildings not at full HP
_bm_very_damaged: int = 0         # buildings with > 2 damage
_bm_landlocked: int = 0

# Builder bot tracking
_bm_friendly_bots: int = 0       # bitmask of known friendly builder bot positions
_bm_enemy_bots: int = 0          # bitmask of known enemy builder bot positions
_bot_pos: dict[int, int] = {}    # uid -> tile index (both teams)
_bot_team: dict[int, int] = {}   # uid -> team_idx
_bot_at: dict[int, int] = {}    # tile index -> uid
_bot_last_seen: dict[int, int] = {}   # uid -> round it was last seen alive in vision

_nearby_tiles_pos: Position | None = None

_left_col: int = 0
_right_col: int = 0
_bottom_row: int = 0
_top_row: int = 0
_not_left_col: int = 0   # mask with all bits EXCEPT x=0 column
_not_right_col: int = 0  # mask with all bits EXCEPT x=width-1 column
_not_bottom_row: int = 0
_not_top_row: int = 0



_my_core: Position | None = None
_their_core: Position | None = None
_predicted_enemy_core: Position | None = None
_core_id: int | None = None
_hor_sym = True
_ver_sym = True
_rot_sym = True
_solved_sym = False
_rush_tiebroken = 0

def _precompute_sentinel_offsets():
    """Sentinel: cardinal=line of 4, diagonal=line of 3, each point expanded 3×3."""
    result = [[] for _ in range(8)]
    for di in range(8):
        ddx, ddy = _DIRECTION_DELTAS_I[di]
        is_cardinal = (ddx == 0 or ddy == 0)
        line_len = 4 if is_cardinal else 3
        tiles = set()
        for step in range(1, line_len + 1):
            cx, cy = ddx * step, ddy * step
            for ey in range(-1, 2):
                for ex in range(-1, 2):
                    px, py = cx + ex, cy + ey
                    if px == 0 and py == 0:
                        continue
                    tiles.add((px, py))
        result[di] = list(tiles)
    return result

def _precompute_gunner_rays():
    """Gunner: straight line rays in all 8 directions, ordered by distance.
    Returns dict keyed by facing dir_idx -> list of (ray_dir_idx, [(dx,dy)...])."""
    rays = []
    for di in range(8):
        ddx, ddy = _DIRECTION_DELTAS_I[di]
        ray = []
        for step in range(1, 4):
            px, py = ddx * step, ddy * step
            if px*px + py*py > GameConstants.GUNNER_VISION_RADIUS_SQ:
                break
            ray.append((px, py))
        rays.append(ray)
    return rays

_SENTINEL_OFFSETS = _precompute_sentinel_offsets()
_GUNNER_RAYS = _precompute_gunner_rays()



def ground_at(x, y):
    bit = 1 << (x + y * _width)
    if not _bm_seen&bit:
        return None
    if _bm_env[_IDX_ENV_WALL] & bit: return Environment.WALL
    if _bm_env[_IDX_ENV_ORE_TI] & bit: return Environment.ORE_TITANIUM
    return Environment.EMPTY
def type_at(x, y):
    et_idx = _building_et_idx[x + y * _width]
    if et_idx >= 0:
        return _INT_ET[et_idx]
    return None
def team_at(x, y):
    bit = 1 << (x + y * _width)
    if _bm_team[0] & bit: return _INT_TM[0]
    if _bm_team[1] & bit: return _INT_TM[1]
    return None
def in_bounds(pos: Position) -> bool:
    return 0 <= pos.x < _width and 0 <= pos.y < _height

def iter_mask(mask):
    """Yield Positions from a bitmask."""
    w = _width
    while mask:
        lsb = mask & -mask
        n = lsb.bit_length() - 1
        yield Position(n % w, n // w)
        mask ^= lsb


def expand_chebyshev(mask: int, times:int = 1) -> int:
    w = _width
    for i in range(times):
        h = mask | ((mask & _not_right_col) << 1) | ((mask & _not_left_col) >> 1)
        mask = (h | (h << w) | (h >> w)) & _board_mask
    return mask


def expand_manhattan(mask: int, times:int = 1) -> int:
    w = _width
    for i in range(times):
        mask = (mask | ((mask & _not_right_col) << 1) | ((mask & _not_left_col) >> 1) | (mask << w) | (mask >> w)) & _board_mask
    return mask


# Shift masks for turret aggregate computation (initialized in init())
_turret_shift_masks: dict[tuple[int,int], int] = {}

def _build_turret_shift_masks():
    """Build column-aware shift masks for each unique (dx,dy) offset used by turrets."""
    global _turret_shift_masks
    w = _width
    h = _height
    offsets = set()
    for di in range(8):
        for dx, dy in _SENTINEL_OFFSETS[di]:
            offsets.add((dx, dy))
    _turret_shift_masks = {}
    for dx, dy in offsets:
        x0 = max(0, -dx)
        x1 = min(w, w - dx)
        y0 = max(0, -dy)
        y1 = min(h, h - dy)
        row_bits = ((1 << (x1 - x0)) - 1) << x0
        nrows = y1 - y0
        block = row_bits * ((1 << (nrows * w)) - 1) // ((1 << w) - 1)
        _turret_shift_masks[(dx, dy)] = block << (y0 * w)

_turret_threat_cache_version: int = -1
_turret_threat_cache: tuple[int, int] = (0, 0)


def _compute_enemy_turret_threat() -> tuple[int, int]:
    """Compute (soft, hard) threat bitmasks.

    Soft: sentinels (low dps).
    Hard: gunners + breaches (high dps).

    Sentinel/breach use bitmask shifting (no wall blocking).
    Gunner uses per-turret ray in current facing only (wall blocking)."""
    global _turret_threat_cache_version, _turret_threat_cache
    if _struct_version == _turret_threat_cache_version:
        return _turret_threat_cache

    w = _width
    h = _height
    enemy_idx = 1 - _my_team_idx
    soft = 0
    hard = 0
    bm_team_enemy = _bm_team[enemy_idx]

    for turret_idx, offsets_table, is_hard in (
        (_IDX_SENTINEL, _SENTINEL_OFFSETS, False),
    ):
        turrets = _bm_et[turret_idx] & bm_team_enemy
        if not turrets:
            continue
        acc = 0
        for di in range(8):
            dm = turrets&_bm_dir[di]
            if not dm:
                continue
            for dx, dy in offsets_table[di]:
                shift_mask = _turret_shift_masks.get((dx, dy))
                offset = dx + dy * w
                if offset > 0:
                    acc |= (dm & shift_mask) << offset
                else:
                    acc |= (dm & shift_mask) >> (-offset)
        if is_hard:
            hard |= acc
        else:
            soft |= acc

    gunners = _bm_et[_IDX_GUNNER] & bm_team_enemy
    if gunners:
        not_walls = _board_mask & ~_bm_env[_IDX_ENV_WALL]
        acc = 0
        for di in range(8):
            dm = gunners&_bm_dir[di]
            if not dm:
                continue
            dx, dy = _DIRECTION_DELTAS_I[di]
            length = 3 - di%2
            shift_mask = _turret_shift_masks.get((dx, dy))
            for i in range(1, length+1):
                offset = dx + dy * w
                if offset > 0:
                    dm = ((dm & shift_mask) << offset) & not_walls
                else:
                    dm = ((dm & shift_mask) >> (-offset)) & not_walls
                acc |= dm
        hard |= acc

    _turret_threat_cache_version = _struct_version
    _turret_threat_cache = (soft, hard)
    return _turret_threat_cache


_my_gunner_claims_cache_version: int = -1
_my_gunner_claims_cache: int = 0


def _compute_my_gunner_claims() -> int:
    """Bitmask of tiles already covered by one of my gunners' current ray."""
    global _my_gunner_claims_cache_version, _my_gunner_claims_cache
    if _struct_version == _my_gunner_claims_cache_version:
        return _my_gunner_claims_cache

    w = _width
    gunners = _bm_et[_IDX_GUNNER] & _bm_team[_my_team_idx]
    claimed = 0
    if gunners:
        not_walls = _board_mask & ~_bm_env[_IDX_ENV_WALL]
        for di in range(8):
            dm = gunners&_bm_dir[di]
            if not dm:
                continue
            dx, dy = _DIRECTION_DELTAS_I[di]
            length = 3 - di%2
            shift_mask = _turret_shift_masks.get((dx, dy))
            for i in range(1, length+1):
                offset = dx + dy * w
                if offset > 0:
                    dm = ((dm & shift_mask) << offset) & not_walls
                else:
                    dm = ((dm & shift_mask) >> (-offset)) & not_walls
                claimed |= dm
    _my_gunner_claims_cache_version = _struct_version
    _my_gunner_claims_cache = claimed
    return claimed


def _splitter_side_output_mask(source_mask: int) -> int:
    splitters = source_mask & _bm_et[_IDX_SPLITTER]
    if not splitters:
        return 0
    dir_mask = _bm_dir
    ew_splitters = splitters & (dir_mask[_DIR_E] | dir_mask[_DIR_W])
    ns_splitters = splitters & (dir_mask[_DIR_N] | dir_mask[_DIR_S])
    return (
        ((ew_splitters & _not_top_row) >> _width)
        | ((ew_splitters & _not_bottom_row) << _width)
        | ((ns_splitters & _not_right_col) << 1)
        | ((ns_splitters & _not_left_col) >> 1)
    ) & _board_mask


def _conv_output_mask(source_n: int, et_idx: int, dir_idx: int, target_n: int) -> int:
    if target_n < 0:
        return 0
    tiles = _width * _height
    outputs = (1 << target_n) if target_n < tiles else 0
    if et_idx != _IDX_SPLITTER or dir_idx < 0:
        return outputs

    sx = source_n % _width
    sy = source_n // _width
    for side_idx in ((dir_idx + 2) & 7, (dir_idx + 6) & 7):
        dx, dy = _DIRECTION_DELTAS_I[side_idx]
        x = sx + dx
        y = sy + dy
        if 0 <= x < _width and 0 <= y < _height:
            outputs |= 1 << (x + y * _width)
    return outputs


def _update_conv_reverse_outputs(
    conv_reverse: list[int],
    source_n: int,
    et_idx: int,
    dir_idx: int,
    target_n: int,
    source_bit: int,
    add: bool,
) -> None:
    outputs = _conv_output_mask(source_n, et_idx, dir_idx, target_n)
    while outputs:
        lsb = outputs & -outputs
        out_n = lsb.bit_length() - 1
        if add:
            conv_reverse[out_n] |= source_bit
        else:
            conv_reverse[out_n] &= ~source_bit
        outputs ^= lsb


def _conveyor_target_tiles(source_mask: int) -> int:
    """Return the union of output target tiles for the given conveyor-like
    sources. Splitters include their primary output and both side outputs;
    bridges fall back to their arbitrary target lookup."""
    if not source_mask:
        return 0

    w = _width
    board = _board_mask
    dir_mask = _bm_dir
    cardinal = source_mask & (
        _bm_et[_IDX_CONVEYOR]
        | _bm_et[_IDX_SPLITTER]
    )
    targets = (
        ((cardinal & dir_mask[_DIR_E] & _not_right_col) << 1)
        | ((cardinal & dir_mask[_DIR_W] & _not_left_col) >> 1)
        | ((cardinal & dir_mask[_DIR_S] & _not_bottom_row) << w)
        | ((cardinal & dir_mask[_DIR_N] & _not_top_row) >> w)
    ) & board
    targets |= _splitter_side_output_mask(source_mask)
    return targets


def _compute_predicted_enemy_core() -> Position | None:
    """Return the enemy core position when known. Symmetry-based prediction is
    left to `update()`, since the flip helpers aren't available here."""
    if _my_core is None:
        return None
    if _their_core is not None:
        return _their_core
    return _predicted_enemy_core


_conv_by_dir_cache_version: int = -1
_conv_by_dir_cache: list[int] = [0] * 8


def _compute_conv_by_dir() -> list[int]:
    """Per facing (0..7): CONVEYOR|ARMOURED_CONVEYOR tiles with that output
    direction. Cached on _struct_version — only rebuilt on structural changes
    (conveyor build/destroy/redirect)."""
    global _conv_by_dir_cache_version, _conv_by_dir_cache
    if _struct_version == _conv_by_dir_cache_version:
        return _conv_by_dir_cache

    convs = _bm_et[_IDX_CONVEYOR]
    result = [convs & _bm_dir[d] for d in range(8)]

    _conv_by_dir_cache_version = _struct_version
    _conv_by_dir_cache = result
    return result


_conv_into_open_ore_cache_version: int = -1
_conv_into_open_ore_cache: int = 0


def _compute_conv_into_open_ore() -> int:
    """CONVEYOR|ARMOURED_CONVEYOR tiles whose target is a non-landlocked ore tile."""
    global _conv_into_open_ore_cache_version, _conv_into_open_ore_cache
    if _struct_version == _conv_into_open_ore_cache_version:
        return _conv_into_open_ore_cache
    convs = _bm_et[_IDX_CONVEYOR]
    if not convs:
        _conv_into_open_ore_cache_version = _struct_version
        _conv_into_open_ore_cache = 0
        return 0
    w = _width
    ore = _bm_env[_IDX_ENV_ORE_TI] & ~_bm_landlocked
    right = convs & _bm_dir[_DIR_E] & ((_not_right_col & ore) >> 1)
    left = convs & _bm_dir[_DIR_W] & ((_not_left_col & ore) << 1)
    up = convs & _bm_dir[_DIR_N] & ((_not_bottom_row & ore) << w)
    down = convs & _bm_dir[_DIR_S] & ((_not_top_row & ore) >> w)
    result = right | left | up | down
    _conv_into_open_ore_cache_version = _struct_version
    _conv_into_open_ore_cache = result
    return result


def update_at(pos: Position) -> None:
    """Re-scan a single tile from the controller and update all per-tile state.

    Maintains env/seen/symmetry tracking, raw building state, marker decoding,
    core detection, and conveyor resource observation. Does NOT touch derived
    bitmasks rebuilt by `recompute_derived()` (e.g. `_bm_blocked`,
    `_bm_conveyors`, `_bm_conveyor_targets`); callers are expected to call `recompute_derived()`
    after iterating.
    """
    global _bm_seen, _bm_any_building
    global _bm_conv_ti
    global _bm_damaged, _bm_very_damaged
    global _hor_sym, _ver_sym, _rot_sym
    global _my_core, _their_core, _core_id, _predicted_enemy_core
    global _struct_version

    rc = _rc
    width = _width
    height = _height
    x = pos.x
    y = pos.y
    n = x + y * width
    bit = 1 << n
    bm_env = _bm_env
    bm_et = _bm_et
    bm_team = _bm_team
    bm_dir = _bm_dir
    building_id = _building_id
    building_et_idx = _building_et_idx
    building_hp = _building_hp
    building_dir = _building_dir
    building_conv_target = _building_conv_target
    conv_reverse = _conv_reverse
    has_dir = _HAS_DIR
    is_conveyor = _IS_CONVEYOR
    get_tile_env = rc.get_tile_env
    get_tile_building_id = rc.get_tile_building_id
    get_hp = rc.get_hp
    get_entity_type = rc.get_entity_type
    get_team = rc.get_team
    get_direction = rc.get_direction
    get_stored_resource = rc.get_stored_resource

    nbit = ~bit

    # Core-area tiles are owned by build_core_areas(); leave their structural
    # state alone, but still refresh shared core HP / damage flags from any
    # visible core tile.
    if (_bm_my_core_area | _bm_their_core_area) & bit:
        entity_id = get_tile_building_id(pos)
        et_idx = building_et_idx[n]
        if entity_id is not None and et_idx == _IDX_CORE:
            hp = get_hp(entity_id)
            building_hp[n] = hp
            max_hp = _MAX_HP_BY_IDX[et_idx]
            if hp < max_hp:
                _bm_damaged |= bit
            else:
                _bm_damaged &= nbit
            if hp < max_hp - 2:
                _bm_very_damaged |= bit
            else:
                _bm_very_damaged &= nbit
        return

    # --- Environment / seen / symmetry tracking ---
    if not (_bm_seen & bit):
        env_idx = _ENV_INT[get_tile_env(pos)]
        bm_env[env_idx] |= bit
        _bm_seen |= bit
        if _solved_sym:
            if _hor_sym:
                fx, fy = width - 1 - x, y
            elif _ver_sym:
                fx, fy = x, height - 1 - y
            else:
                fx, fy = width - 1 - x, height - 1 - y
            fbit = 1 << (fx + fy * width)
            bm_env[env_idx] |= fbit
            _bm_seen |= fbit
        else:
            rx = width - 1 - x
            ry = height - 1 - y
            if _hor_sym:
                fbit = 1 << (rx + y * width)
                if (_bm_seen & fbit) and not (bm_env[env_idx] & fbit):
                    _hor_sym = False
            if _ver_sym:
                fbit = 1 << (x + ry * width)
                if (_bm_seen & fbit) and not (bm_env[env_idx] & fbit):
                    _ver_sym = False
            if _rot_sym:
                fbit = 1 << (rx + ry * width)
                if (_bm_seen & fbit) and not (bm_env[env_idx] & fbit):
                    _rot_sym = False
        # Newly-observed walls block gunner rays in _compute_enemy_turret_threat
        # and _compute_my_gunner_claims, so invalidate those caches.
        if env_idx == _IDX_ENV_WALL:
            _struct_version += 1

    # Walls can never hold buildings and never change. Skip the controller
    # building lookup and all building-state work — saves get_tile_building_id
    # calls (one of the heaviest controller methods in the profile).
    if bm_env[_IDX_ENV_WALL] & bit:
        return

    # --- Building state ---
    entity_id = get_tile_building_id(pos)

    old_et_idx = building_et_idx[n]
    if entity_id is None:
        # No building — clear old
        if old_et_idx >= 0:
            bm_et[old_et_idx] &= nbit
            _bm_any_building &= nbit
            bm_team[0] &= nbit
            bm_team[1] &= nbit
            if has_dir[old_et_idx]:
                bm_dir[building_dir[n]] &= nbit
            if is_conveyor[old_et_idx]:
                _update_conv_reverse_outputs(
                    conv_reverse,
                    n,
                    old_et_idx,
                    building_dir[n],
                    building_conv_target[n],
                    bit,
                    False,
                )
                _bm_conv_ti &= nbit
            building_id[n] = 0
            building_et_idx[n] = -1
            building_hp[n] = 0
            building_dir[n] = -1
            building_conv_target[n] = -1
            _struct_version += 1
        _bm_damaged &= nbit
        _bm_very_damaged &= nbit
        return

    # Fast path: same building as before — skip re-reading type/team/direction
    if building_id[n] == entity_id:
        et_idx = old_et_idx
        # Gunners are the only entity that can change direction without
        # changing entity_id (rotate). Re-read so the threat mask stays fresh.
        if et_idx == _IDX_GUNNER:
            new_dir_idx = _DIR_INT[get_direction(entity_id)]
            old_dir_idx = building_dir[n]
            if new_dir_idx != old_dir_idx:
                bm_dir[old_dir_idx] &= nbit
                bm_dir[new_dir_idx] |= bit
                building_dir[n] = new_dir_idx
                _struct_version += 1
        hp = get_hp(entity_id)
        building_hp[n] = hp
        max_hp = _MAX_HP_BY_IDX[et_idx]
        if hp < max_hp:
            _bm_damaged |= bit
        else:
            _bm_damaged &= nbit
        if hp < max_hp - 2:
            _bm_very_damaged |= bit
        else:
            _bm_very_damaged &= nbit
        if is_conveyor[et_idx]:
            res = get_stored_resource(entity_id)
            if res is not None:
                _bm_conv_ti |= bit
            else:
                _bm_conv_ti &= nbit
        return

    # (Titan: tile markers removed — the marker-decode branch that lived here is gone.)
    et = get_entity_type(entity_id)

    # Different building — clear old state before writing new
    if old_et_idx >= 0:
        bm_et[old_et_idx] &= nbit
        _bm_any_building &= nbit
        bm_team[0] &= nbit
        bm_team[1] &= nbit
        if has_dir[old_et_idx]:
            bm_dir[building_dir[n]] &= nbit
        if is_conveyor[old_et_idx]:
            _update_conv_reverse_outputs(
                conv_reverse,
                n,
                old_et_idx,
                building_dir[n],
                building_conv_target[n],
                bit,
                False,
            )
            _bm_conv_ti &= nbit

    et_idx = _ET_INT[et]
    direction = get_direction(entity_id) if has_dir[et_idx] else None
    team_val = get_team(entity_id)
    team_idx = _TM_INT[team_val]

    target = None
    if is_conveyor[et_idx] and direction is not None:
        dx, dy = _DIRECTION_DELTAS_I[_DIR_INT[direction]]
        target = Position(x + dx, y + dy)

    building_id[n] = entity_id
    building_et_idx[n] = et_idx
    hp = get_hp(entity_id)
    building_hp[n] = hp
    new_dir_idx = _DIR_INT[direction] if direction is not None else -1
    building_dir[n] = new_dir_idx
    new_tn = (target.x + target.y * width) if target is not None else -1
    building_conv_target[n] = new_tn

    bm_et[et_idx] |= bit
    bm_team[team_idx] |= bit
    _bm_any_building |= bit
    if direction is not None:
        bm_dir[new_dir_idx] |= bit

    if is_conveyor[et_idx] and new_tn >= 0:
        _update_conv_reverse_outputs(
            conv_reverse,
            n,
            et_idx,
            new_dir_idx,
            new_tn,
            bit,
            True,
        )

    max_hp = _MAX_HP_BY_IDX[et_idx]
    if hp < max_hp:
        _bm_damaged |= bit
    else:
        _bm_damaged &= nbit
    if hp < max_hp - 2:
        _bm_very_damaged |= bit
    else:
        _bm_very_damaged &= nbit

    if is_conveyor[et_idx]:
        res = get_stored_resource(entity_id)
        if res is not None:
            _bm_conv_ti |= bit
        else:
            _bm_conv_ti &= nbit

    # First-sight core detection
    if et is _ET_CORE:
        if _my_core is None and team_val == _my_team:
            _my_core = core_origin(entity_id, pos)
            _core_id = entity_id
            build_core_areas()
            _predicted_enemy_core = _compute_predicted_enemy_core()
        elif _their_core is None and team_val != _my_team:
            _their_core = core_origin(entity_id, pos)
            build_core_areas()
            _predicted_enemy_core = _compute_predicted_enemy_core()

    # Different-building path always writes new structural state.
    _struct_version += 1

def update_move() -> None:
    """After moving, re-scan tiles that are now visible but weren't from the previous position."""
    global _bm_visible, _prev_pos, _nearby_tiles, _nearby_tiles_pos, _my_pos
    rc = _rc
    new_pos = rc.get_position()
    _my_pos = new_pos
    if new_pos == _prev_pos:
        return
    _prev_pos = new_pos

    width = _width
    if _nearby_tiles_pos == new_pos:
        nearby = _nearby_tiles
    else:
        nearby = rc.get_nearby_tiles()
        _nearby_tiles = nearby
        _nearby_tiles_pos = new_pos
    old_visible = _bm_visible
    new_visible = 0
    saw_newly_visible = False
    for tile in nearby:
        bit = 1 << (tile.x + tile.y * width)
        new_visible |= bit
        if not (old_visible & bit):
            update_at(tile)
            saw_newly_visible = True
    _bm_visible = new_visible

    if not saw_newly_visible:
        return

    pathing.rebuild_broken_barriers(rc)
    recompute_derived()


def init(c: Controller):
    global _rc, _width, _height
    global _my_team, _my_team_idx
    global _prev_pos, _my_pos
    global _my_team, _my_team_idx
    global _building_id, _building_et_idx, _building_hp, _building_dir, _building_conv_target, _conv_reverse
    global _bm_et, _bm_team, _bm_env
    global _left_col, _right_col, _bottom_row, _top_row, _not_left_col, _not_right_col, _not_bottom_row, _not_top_row
    global _board_mask, _bm_dir
    global _struct_version
    global _turret_threat_cache_version, _turret_threat_cache
    global _my_gunner_claims_cache_version, _my_gunner_claims_cache
    global _conv_by_dir_cache_version, _conv_by_dir_cache
    global _route_targets_cache_key, _route_targets_cache
    global _route_reaches_core_cache_version, _route_reaches_core_cache
    global _recompute_structural_cache_version, _recompute_visible_cache_key
    global _bot_pos, _bot_team, _bot_at, _bot_last_seen
    _rc = c
    _my_team = _rc.get_team()
    _my_team_idx = _TM_INT[_my_team]
    _width = _rc.get_map_width()
    _height = _rc.get_map_height()
    tiles = _width * _height
    _board_mask = (1 << tiles) - 1
    _building_id          = [0] * tiles
    _building_et_idx      = [-1] * tiles
    _building_hp          = [-1] * tiles
    _building_dir         = [-1] * tiles
    _building_conv_target = [-1] * tiles
    _conv_reverse         = [0] * tiles

    _bm_et   = [0] * _NUM_ET
    _bm_team = [0] * _NUM_TEAM
    _bm_env  = [0] * _NUM_ENV
    _bm_dir  = [0] * len(Direction)

    _struct_version = 0
    _turret_threat_cache_version = -1
    _turret_threat_cache = (0, 0)
    _my_gunner_claims_cache_version = -1
    _my_gunner_claims_cache = 0
    _conv_by_dir_cache_version = -1
    _conv_by_dir_cache = [0] * 8
    _route_targets_cache_key = None
    _route_targets_cache = (0, 0, 0)
    _route_reaches_core_cache_version = -1
    _route_reaches_core_cache = (0, [])
    _recompute_structural_cache_version = -1
    _recompute_visible_cache_key = None
    _bot_pos = {}
    _bot_team = {}
    _bot_at = {}
    _bot_last_seen = {}

    # Column masks for safe bit-shifting (prevent wrap-around)
    _left_col = _board_mask//((1<<_width)-1)
    _right_col = _left_col << (_width-1)
    _not_left_col = _board_mask & ~_left_col
    _not_right_col = _board_mask & ~_right_col
    _top_row = (1<<_width)-1
    _bottom_row = _top_row << (_width*(_height-1))
    _not_top_row = _board_mask & ~_top_row
    _not_bottom_row = _board_mask & ~_bottom_row
    _build_turret_shift_masks()

def update_symmetry_from_comms(sym_bits):
    """Update symmetry from comms. Each bit represents a possible symmetry."""
    global _hor_sym, _ver_sym, _rot_sym
    if not (sym_bits & 1):
        _hor_sym = False
    if not (sym_bits & 2):
        _ver_sym = False
    if not (sym_bits & 4):
        _rot_sym = False


def local_solved_symmetry_code() -> int:
    """1=hor, 2=ver, 3=rot when we've locally confirmed exactly one symmetry,
    else 0. Used to broadcast the solved symmetry through the opening-role slot
    (the board's symmetry byte can't reach far launchers — see comms)."""
    if not _solved_sym:
        return 0
    if _hor_sym and not _ver_sym and not _rot_sym:
        return 1
    if _ver_sym and not _hor_sym and not _rot_sym:
        return 2
    if _rot_sym and not _hor_sym and not _ver_sym:
        return 3
    return 0


def note_shared_symmetry(code: int) -> None:
    """Adopt a teammate's confirmed symmetry: narrow our flags to just it and,
    once our core is known (locally or via comms), fix the enemy core. Lets a
    far launcher throw toward the real core instead of the diagonal guess."""
    global _hor_sym, _ver_sym, _rot_sym, _solved_sym, _their_core
    if code == 1:
        _hor_sym, _ver_sym, _rot_sym = True, False, False
    elif code == 2:
        _hor_sym, _ver_sym, _rot_sym = False, True, False
    elif code == 3:
        _hor_sym, _ver_sym, _rot_sym = False, False, True
    else:
        return
    _solved_sym = True
    if _their_core is None:
        core = _my_core or _shared_core
        if core is not None:
            if code == 1:
                _their_core = hor_flip_core(core)
            elif code == 2:
                _their_core = ver_flip_core(core)
            else:
                _their_core = rot_flip_core(core)


def apply_shared_tile(n: int, env_idx: int) -> bool:
    """Fold a teammate-shared tile observation (from comms) into local state.

    Faithful mirror of the local-vision environment path in update_at(): record
    the tile's env + seen bit, then either mirror it across the confirmed axis
    (post-solve) or run symmetry elimination against already-seen mirror tiles
    (pre-solve). Sharing *known-empty* tiles — not just walls/ore — is what lets
    pooled observations eliminate a symmetry, since elimination needs both a
    tile and its mirror in _bm_seen. Returns True if this tile was new."""
    global _bm_seen, _hor_sym, _ver_sym, _rot_sym, _struct_version
    bit = 1 << n
    if _bm_seen & bit:
        return False
    width = _width
    height = _height
    x = n % width
    y = n // width
    _bm_env[env_idx] |= bit
    _bm_seen |= bit
    if _solved_sym:
        if _hor_sym:
            fx, fy = width - 1 - x, y
        elif _ver_sym:
            fx, fy = x, height - 1 - y
        else:
            fx, fy = width - 1 - x, height - 1 - y
        fbit = 1 << (fx + fy * width)
        _bm_env[env_idx] |= fbit
        _bm_seen |= fbit
    else:
        rx = width - 1 - x
        ry = height - 1 - y
        if _hor_sym:
            fbit = 1 << (rx + y * width)
            if (_bm_seen & fbit) and not (_bm_env[env_idx] & fbit):
                _hor_sym = False
        if _ver_sym:
            fbit = 1 << (x + ry * width)
            if (_bm_seen & fbit) and not (_bm_env[env_idx] & fbit):
                _ver_sym = False
        if _rot_sym:
            fbit = 1 << (rx + ry * width)
            if (_bm_seen & fbit) and not (_bm_env[env_idx] & fbit):
                _rot_sym = False
    if env_idx == _IDX_ENV_WALL:
        _struct_version += 1
    return True

# --- 2x2-core-aware reflections -------------------------------------------------
# The plain flips above reflect a *point* (correct for mirroring walls/ore). The
# core is a 2x2 block whose top-left origin reflects onto the enemy block's FAR
# corner, so the enemy origin is (size-2-coord) on each reversed axis, not
# (size-1-coord). Use these whenever flipping a core ORIGIN.
def hor_flip_core(pos: Position) -> Position:
    return Position(_width - 2 - pos.x, pos.y)
def ver_flip_core(pos: Position) -> Position:
    return Position(pos.x, _height - 2 - pos.y)
def rot_flip_core(pos: Position) -> Position:
    return Position(_width - 2 - pos.x, _height - 2 - pos.y)
def flip_core(pos: Position) -> Position | None:
    if not _solved_sym:
        return None
    if _hor_sym:
        return hor_flip_core(pos)
    if _ver_sym:
        return ver_flip_core(pos)
    if _rot_sym:
        return rot_flip_core(pos)
    return None

_shared_core: Position | None = None   # core position learned via comms


def note_shared_core(pos: Position) -> None:
    """Record the core position shared over comms so units that never saw the
    core themselves (e.g. a launcher built far out) can still locate it."""
    global _shared_core
    _shared_core = pos


def atk_symmetry_target(atk_index):
    """Enemy-core target for attack builder ``atk_index``.
      - Before the symmetry is known, both attackers assume the DIAGONAL
        (rotational) core.
      - If rotational is eliminated, the two split to the axes (index 0 ->
        horizontal, index 1 -> vertical) while both axes are still possible.
      - Once only one symmetry remains it is known, so both target the real core.
    Returns None until our own core is known (locally or via comms). Shared by
    the attack builder (explore target) and the launcher (throw target) so both
    derive the same destination from the same symmetry/map state."""
    core = _my_core or _shared_core
    if core is None:
        return None
    if _their_core is not None:
        return _their_core
    if _rot_sym:
        return rot_flip_core(core)          # diagonal first
    if _hor_sym and _ver_sym:               # rotational gone -> split the axes
        return ver_flip_core(core) if atk_index == 1 else hor_flip_core(core)
    if _hor_sym:                            # only one axis left -> known core
        return hor_flip_core(core)
    if _ver_sym:
        return ver_flip_core(core)
    return _predicted_enemy_core


def core_origin(core_id: int, tile: Position) -> Position | None:
    """Top-left origin of the 2x2 core block containing an observed core tile.

    Titan cores are 2x2 (get_position() returns the top-left). From an arbitrary
    observed core tile we step to the min corner: if the left/up neighbour is also
    this core, the observed tile is in the right column / bottom row."""
    def is_core(x: int, y: int) -> bool:
        p = Position(x, y)
        return in_bounds(p) and _rc.is_in_vision(p) and _rc.get_tile_building_id(p) == core_id
    ox = tile.x - 1 if is_core(tile.x - 1, tile.y) else tile.x
    oy = tile.y - 1 if is_core(tile.x, tile.y - 1) else tile.y
    return Position(ox, oy)

def build_core_areas() -> None:
    global _bm_my_core_area, _bm_their_core_area, _bm_conveyors, _bm_any_building
    _bm_my_core_area = 0
    _bm_their_core_area = 0
    bm_et = _bm_et
    bm_team = _bm_team
    num_et = _NUM_ET
    num_team = _NUM_TEAM
    if _my_core is not None:
        n = _my_core.x+_my_core.y*_width
        my_team_idx = _my_team_idx
        for x in range(_my_core.x, _my_core.x + 2):
            for y in range(_my_core.y, _my_core.y + 2):
                m = x+y*_width
                bit = 1 << m
                # Clear any old entity/team bits at this tile
                for i in range(num_et):
                    bm_et[i] &= ~bit
                for i in range(num_team):
                    bm_team[i] &= ~bit
                _building_id[m] = _core_id
                _building_et_idx[m] = _IDX_CORE
                _building_hp[m] = _building_hp[n]
                _bm_my_core_area |= bit
                _bm_any_building |= bit
                bm_et[_IDX_CORE] |= bit
                bm_team[my_team_idx] |= bit
    if _their_core is not None:
        n = _their_core.x+_their_core.y*_width
        enemy_team_idx = 1 - _my_team_idx
        for x in range(_their_core.x, _their_core.x + 2):
            for y in range(_their_core.y, _their_core.y + 2):
                    m = x+y*_width
                    bit = 1 << m
                    for i in range(num_et):
                        bm_et[i] &= ~bit
                    for i in range(num_team):
                        bm_team[i] &= ~bit
                    _building_id[m] = _building_id[n]
                    _building_et_idx[m] = _IDX_CORE
                    _building_hp[m] = _building_hp[n]
                    _bm_their_core_area |= bit
                    _bm_any_building |= bit
                    bm_et[_IDX_CORE] |= bit
                    bm_team[enemy_team_idx] |= bit

_route_targets_cache_key: tuple | None = None
_route_targets_cache: tuple[int, int] = (0, 0)  # (route_targets, dead_end)
_route_reaches_core_cache_version: int = -1
_route_reaches_core_cache: tuple[int, list[int]] = (0, [])


def _compute_route_reaches_core() -> tuple[int, tuple[int, ...]]:
    global _route_reaches_core_cache_version, _route_reaches_core_cache
    if _struct_version == _route_reaches_core_cache_version:
        return _route_reaches_core_cache

    my_convs = _bm_conveyors & _bm_team[_my_team_idx]
    reverse = _conv_reverse
    reaches_core = 0
    order: list[int] = []

    layer = 0
    c_mask = _bm_my_core_area
    while c_mask:
        lsb = c_mask & -c_mask
        n = lsb.bit_length() - 1
        layer |= reverse[n] & my_convs
        c_mask ^= lsb

    # Single walk per layer: append to `order` and accumulate `next_layer`
    # in the same LSB-extraction loop, instead of two separate passes.
    order_append = order.append
    while layer:
        reaches_core |= layer
        next_layer = 0
        m = layer
        while m:
            lsb = m & -m
            n = lsb.bit_length() - 1
            order_append(n)
            next_layer |= reverse[n]
            m ^= lsb
        layer = next_layer & my_convs & ~reaches_core

    # Cache as list — callers only iterate, no need to pay for tuple().
    result = (reaches_core, order)
    _route_reaches_core_cache_version = _struct_version
    _route_reaches_core_cache = result
    return result


def _compute_route_targets() -> int:
    """Bitmask of tiles the route state can path toward.

    Route targets = my conveyors whose downstream chain reaches my core area,
    minus any that are part of a connected run of 4+ believed-loaded conveyors,
    My core area is always routable.

    Side effect: sets `_bm_dead_end` to the targets of any *loaded* conveyor
    whose output is nothing or a building not in (conveyor-type, my core,
    my sentinel, my gunner, my breach). Also includes my conveyors pointing
    into an enemy non-road non-marker building.
    """
    my_team_idx = _my_team_idx
    bm_my = _bm_team[my_team_idx]
    my_convs = _bm_conveyors & bm_my
    loaded_union = _bm_conv_ti
    visible_loaded_mine = my_convs & loaded_union & _bm_visible
    global _bm_dead_end
    global _route_targets_cache_key, _route_targets_cache
    key = (
        _struct_version,
        _bm_conv_ti,
        visible_loaded_mine,
    )
    if key == _route_targets_cache_key:
        rt, de = _route_targets_cache
        _bm_dead_end = de
        return rt
    conv_target = _building_conv_target
    tiles = _width * _height
    reverse = _conv_reverse
    all_convs = _bm_conveyors

    accepting = (
        _bm_et[_IDX_CONVEYOR] | _bm_et[_IDX_SPLITTER]
        | ((_bm_et[_IDX_CORE] | _bm_et[_IDX_SENTINEL]
            | _bm_et[_IDX_GUNNER]) & bm_my)
    )
    enemy_hard = _bm_team[1 - my_team_idx]
    enemy_bm = _bm_team[1 - my_team_idx]
    enemy_fed_turret = (
        _bm_et[_IDX_GUNNER] | _bm_et[_IDX_SENTINEL]
    ) & enemy_bm
    hard_block = (
        enemy_fed_turret
        | ((_bm_et[_IDX_LAUNCHER] | _bm_et[_IDX_BARRIER] | _bm_et[_IDX_CORE]) & enemy_bm)
        | _bm_et[_IDX_HARVESTER]
    )

    loaded_sources = all_convs & loaded_union

    # --- Dead-ends: targets of any *loaded* conveyor whose output isn't
    # accepting (or, for my conveyors, points into enemy non-road non-marker).
    dead_ends = 0
    mask = loaded_sources
    while mask:
        lsb = mask & -mask
        n = lsb.bit_length() - 1
        tn = conv_target[n]
        tbit = 1 << tn
        if hard_block & tbit:
            dead_ends |= lsb
        elif not (accepting & tbit):
            dead_ends |= tbit
        elif (bm_my & lsb) and (enemy_hard & tbit):
            dead_ends |= tbit
        mask ^= lsb
    _bm_dead_end = dead_ends

    # --- Overlay loaded/visible state on top of the structural conveyor graph.
    reaches_core, reaches_core_order = _compute_route_reaches_core()
    loaded_mine = my_convs & loaded_union
    unroutable = 0
    if loaded_mine:
        run_loaded_arr = [0] * tiles
        ext_roots = 0
        run_visible_arr = [0] * tiles if visible_loaded_mine else None

        for n in reaches_core_order:
            lsb = 1 << n
            p = conv_target[n]
            if p >= 0 and (reaches_core & (1 << p)):
                p_loaded = run_loaded_arr[p]
                p_visible = run_visible_arr[p] if run_visible_arr is not None else 0
            else:
                p_loaded = 0
                p_visible = 0
            if loaded_mine & lsb:
                rl = p_loaded + 1
                run_loaded_arr[n] = rl
            else:
                rl = 0
            if run_visible_arr is not None and (visible_loaded_mine & lsb):
                rv = p_visible + 1
                run_visible_arr[n] = rv
                if rv >= 4:
                    ext_roots |= lsb
            if rl >= 4:
                if rl == 4:
                    cur = n
                    for _ in range(4):
                        unroutable |= 1 << cur
                        cur = conv_target[cur]
                        if cur < 0:
                            break
                else:
                    unroutable |= lsb

        # builder.draw_mask(unroutable, 255, 0, 0)

        # --- A visible 4-run jams the full chain: extend through all my conveyors
        # both upstream and downstream from each ext_root.
        if ext_roots:
            extended = ext_roots
            frontier = ext_roots
            while frontier:
                new_frontier = 0
                m = frontier
                while m:
                    lb = m & -m
                    n = lb.bit_length() - 1
                    tn = conv_target[n]
                    if 0 <= tn < tiles:
                        tbit = 1 << tn
                        if (my_convs & tbit) and not (extended & tbit):
                            new_frontier |= tbit
                    new_frontier |= reverse[n] & my_convs & ~extended
                    m ^= lb
                extended |= new_frontier
                frontier = new_frontier
            # builder.draw_mask(extended & ~unroutable, 255, 255, 255)
            unroutable |= extended

    # Color conveyors by unroutability reason (later draws win when overlapping):
    #   red     = part of a loaded run of 4+ along the chain toward core
    #   white   = propagated from a visible 4-run (already drawn above)
    #   orange  = my conveyor whose chain does not reach the core
    # builder.draw_mask(my_convs & ~reaches_core, 255, 128, 0)

    # Dead ends must never be enemy conveyor tiles themselves (targets of enemy
    # conveyors are still allowed).
    _bm_dead_end &= ~(_bm_conveyors & ~bm_my)

    result = _bm_my_core_area | (reaches_core & ~unroutable)
    _route_targets_cache_key = key
    _route_targets_cache = (result, _bm_dead_end)
    return result

_recompute_structural_cache_version: int = -1
_recompute_visible_cache_key: tuple | None = None


def _recompute_derived_structural() -> None:
    global _bm_blocked, _bm_conveyors, _bm_conveyor_targets
    global _bm_enemy_launch_adj
    global _bm_enemy_turret_threat, _bm_enemy_soft_threat, _bm_enemy_hard_threat
    global _bm_my_gunner_claims, _bm_conv_by_dir, _bm_conv_into_open_ore
    global _bm_passable_FFF
    global _recompute_structural_cache_version

    if _struct_version == _recompute_structural_cache_version:
        return
    _recompute_structural_cache_version = _struct_version

    width = _width
    height = _height
    my_team_idx = _my_team_idx
    bm_et = _bm_et
    bm_team = _bm_team
    bm_env = _bm_env

    _bm_conveyors = (
        bm_et[_IDX_CONVEYOR]
        | bm_et[_IDX_SPLITTER]
    )
    _bm_conveyor_targets = _conveyor_target_tiles(_bm_conveyors)

    _bm_blocked = bm_env[_IDX_ENV_WALL]
    _bm_blocked |= bm_et[_IDX_HARVESTER]
    _bm_blocked |= bm_et[_IDX_GUNNER] | bm_et[_IDX_SENTINEL]
    _bm_blocked |= bm_et[_IDX_LAUNCHER]
    _bm_blocked |= bm_et[_IDX_BARRIER] & ~bm_team[my_team_idx]
    # Titan: cores are never walkable — OURS included (Cambridge's 3x3 core was
    # ally-walkable; the 2x2 Titan core is not).
    _bm_blocked |= _bm_my_core_area | _bm_their_core_area

    enemy_launchers = bm_et[_IDX_LAUNCHER] & ~bm_team[my_team_idx]
    _bm_enemy_launch_adj = 0
    mask = enemy_launchers
    while mask:
        lsb = mask & -mask
        ln = lsb.bit_length() - 1
        lx = ln % width
        ly = ln // width
        for dx, dy in _DIRECTION_DELTAS_I:
            nx = lx + dx
            ny = ly + dy
            if 0 <= nx < width and 0 <= ny < height:
                _bm_enemy_launch_adj |= 1 << (nx + ny * width)
        mask ^= lsb

    _bm_enemy_soft_threat, _bm_enemy_hard_threat = _compute_enemy_turret_threat()
    _bm_enemy_turret_threat = _bm_enemy_soft_threat | _bm_enemy_hard_threat
    _bm_my_gunner_claims = _compute_my_gunner_claims()
    _bm_conv_by_dir = _compute_conv_by_dir()
    _bm_conv_into_open_ore = _compute_conv_into_open_ore()
    _bm_passable_FFF = _board_mask & ~(_bm_blocked | _bm_enemy_launch_adj)


def _recompute_derived_visible() -> None:
    global _bm_route_targets, _recompute_visible_cache_key

    key = (_struct_version, _bm_conv_ti, _bm_visible)
    if key == _recompute_visible_cache_key:
        return
    _recompute_visible_cache_key = key
    _bm_route_targets = _compute_route_targets()


def recompute_derived() -> None:
    """Rebuild derived bitmasks from the current tracked map state."""
    _recompute_derived_structural()
    _recompute_derived_visible()


def update(recompute: bool = True) -> None:
    global _my_core, _their_core, _core_id, _solved_sym
    global _hor_sym, _ver_sym, _rot_sym
    global _rush_tiebroken, _predicted_enemy_core
    global _bm_any_building
    global _bm_seen, _bm_visible, _prev_pos, _nearby_tiles, _nearby_tiles_pos, _my_pos
    global _bm_friendly_bots, _bm_enemy_bots
    global _bm_others_3x3
    global _struct_version
    rc = _rc
    building_id = _building_id
    building_et_idx = _building_et_idx
    building_hp = _building_hp

    bm_et = _bm_et
    bm_team = _bm_team
    bm_env = _bm_env

    width = _width
    height = _height

    my_team_idx   = _my_team_idx
    my_pos        = rc.get_position()
    _my_pos       = my_pos

    visible_cached = (_nearby_tiles_pos == my_pos)
    if visible_cached:
        visible_tiles = _nearby_tiles
    else:
        visible_tiles = rc.get_nearby_tiles()
        _nearby_tiles = visible_tiles
        _nearby_tiles_pos = my_pos
    _prev_pos = my_pos

    if visible_cached:
        bm_visible = _bm_visible
        for tile in visible_tiles:
            update_at(tile)
    else:
        bm_visible = 0
        for tile in visible_tiles:
            bit = 1 << (tile.x + tile.y * width)
            bm_visible |= bit
            update_at(tile)
        _bm_visible = bm_visible

    possible_syms = int(_hor_sym) + int(_ver_sym) + int(_rot_sym)
    if possible_syms == 1 and not _solved_sym:
        _solved_sym = True
        if _my_core:
            _their_core = flip_core(_my_core)
            if _their_core is not None:
                pos = _their_core.x+_their_core.y*width
                pbit = 1 << pos
                building_id[pos] = -1
                building_et_idx[pos] = _IDX_CORE
                bm_et[_IDX_CORE] |= pbit
                _bm_any_building |= pbit
                enemy_team_idx = 1 - my_team_idx
                bm_team[enemy_team_idx] |= pbit
                building_hp[pos] = GameConstants.CORE_MAX_HP
            build_core_areas()
        bm_seen = _bm_seen
        for x in range(width):
            for y in range(height):
                n = x+y*width
                nbit = 1 << n
                if bm_seen & nbit:
                    if _ver_sym:
                        flipped = (x)+(height-1-y)*width
                    elif _hor_sym:
                        flipped = (width-1-x)+(y)*width
                    else:
                        flipped = (width-1-x)+(height-1-y)*width
                    fbit = 1 << flipped
                    if not (bm_seen & fbit):
                        # Copy env from source tile to flipped tile
                        for env_i in range(_NUM_ENV):
                            if bm_env[env_i] & nbit:
                                bm_env[env_i] |= fbit
                                break
                        bm_seen |= fbit
        _bm_seen = bm_seen
        # Symmetry-solve mirrored walls into _bm_env[WALL] and may have inserted
        # the predicted enemy core into _bm_et[CORE] / _bm_team / _building_*.
        # Both can affect cached compute_* outputs, so invalidate.
        _struct_version += 1

    if _my_core:
        if _their_core:
            _predicted_enemy_core = _their_core
        elif _hor_sym or _ver_sym:
            # Prefer an AXIS (horizontal/vertical) guess over the diagonal
            # rotational one whenever an axis symmetry is still possible — the
            # enemy core sitting straight across is the far likelier layout, and
            # a rotational guess sends the rush across the wrong diagonal.
            hsym_core = hor_flip_core(_my_core)
            vsym_core = ver_flip_core(_my_core)
            if _rush_tiebroken == 1 and _ver_sym:
                _predicted_enemy_core = vsym_core
            elif _rush_tiebroken == 2 and _hor_sym:
                _predicted_enemy_core = hsym_core
            elif _ver_sym and _hor_sym:
                if abs(my_pos.x - hsym_core.x) + abs(my_pos.y - hsym_core.y) < abs(my_pos.x - vsym_core.x) + abs(my_pos.y - vsym_core.y):
                    _predicted_enemy_core = hsym_core
                    _rush_tiebroken = 2
                    log("Tiebreaking enemy core sym - HORIZONTAL")
                else:
                    _predicted_enemy_core = vsym_core
                    _rush_tiebroken = 1
                    log("Tiebreaking enemy core sym - VERTICAL")
            elif _ver_sym:
                _predicted_enemy_core = vsym_core
            else:
                _predicted_enemy_core = hsym_core
        elif _rot_sym:
            # Only fall back to the rotational (diagonal) guess when both axis
            # symmetries have been eliminated.
            _predicted_enemy_core = rot_flip_core(_my_core)

    # --- Update builder bot tracking ---
    _bm_friendly_bots = 0
    _bm_enemy_bots = 0
    seen_uids = set()
    cur_round = rc.get_current_round()
    self_id = rc.get_id()
    for uid in rc.get_nearby_units():
        if rc.get_entity_type(uid) != _ET_BUILDER_BOT:
            continue
        if uid == self_id:
            continue
        ep = rc.get_position(uid)
        n = ep.x + ep.y * width
        team_idx = _TM_INT[rc.get_team(uid)]
        # If tracked at a different position, clear old
        old_n = _bot_pos.get(uid)
        if old_n is not None and old_n != n:
            if _bot_at.get(old_n) == uid:
                del _bot_at[old_n]
        _bot_pos[uid] = n
        _bot_team[uid] = team_idx
        _bot_at[n] = uid
        _bot_last_seen[uid] = cur_round
        seen_uids.add(uid)
    # Single pass: purge stale bots (visible-but-gone after grace) and rebuild
    # the friendly/enemy bitmasks from the survivors.
    grace_cutoff = cur_round - 1
    to_remove = []
    for uid, n in _bot_pos.items():
        bit = 1 << n
        if uid not in seen_uids and (bm_visible & bit):
            if _bot_last_seen.get(uid, -1) < grace_cutoff:
                to_remove.append(uid)
                continue
        if _bot_team[uid] == my_team_idx:
            _bm_friendly_bots |= bit
        else:
            _bm_enemy_bots |= bit
    for uid in to_remove:
        n = _bot_pos[uid]
        if _bot_at.get(n) == uid:
            del _bot_at[n]
        del _bot_pos[uid]
        del _bot_team[uid]
        _bot_last_seen.pop(uid, None)

    # Precompute other-bots zone masks for cant_claim().
    # expand_chebyshev distributes over OR, so one call per layer suffices.
    my_bit = 1 << (my_pos.x + my_pos.y * width)
    friendly_others = _bm_friendly_bots & ~my_bit
    if friendly_others:
        _bm_others_3x3 = expand_manhattan(friendly_others)  # Manhattan r1 (name is legacy)
    else:
        _bm_others_3x3 = 0

    if recompute:
        recompute_derived()


def is_tile_empty(pos: Position):
    if not in_bounds(pos):
        return False
    return _rc.is_tile_empty(pos)


def is_passable(pos: Position):
    if not in_bounds(pos): return False
    n = pos.x + pos.y * _width
    bit = 1 << n
    if _bm_env[_IDX_ENV_WALL] & bit: return False
    if _building_id[n] == 0: return True
    my_team_idx = _my_team_idx
    # Titan: conveyors/splitters are walkable; own barriers are break-through-able
    # for pathing purposes; cores are NOT walkable (either team).
    return bool(
        (_bm_et[_IDX_CONVEYOR] | _bm_et[_IDX_SPLITTER]
         | (_bm_et[_IDX_BARRIER] & _bm_team[my_team_idx])
        ) & bit
    )

def get_avoid(
    avoid_conveyors: bool,
    avoid_builders: bool,
    avoid_ore: bool,
    enemy_pov = False
) -> int:
    """Return a bitmask of tiles to avoid during pathfinding."""
    # avoid_core = _rc.get_tile_building_id(_rc.get_position()) != _core_id
    mask = _bm_blocked
    if enemy_pov:
        mask &= ~_bm_their_core_area
        mask |= _bm_my_core_area
    if avoid_conveyors:
        mask |= (_bm_conveyors&~_bm_conv_into_open_ore) | _bm_conveyor_targets | _bm_my_core_area
        # Friendly barriers cardinally adjacent to a wall — these form
        # tight defensive chokes; don't path through (destroy) them.
        friendly_barriers = _bm_et[_IDX_BARRIER] & _bm_team[_my_team_idx]
        mask |= friendly_barriers & expand_manhattan(_bm_env[_IDX_ENV_WALL])
    if avoid_ore:
        ore = _bm_env[_IDX_ENV_ORE_TI]
        w = _width
        landlocking = ore | ~_bm_seen&_board_mask
        landlocked = landlocking & (landlocking >> 1 & _not_right_col) & (landlocking << 1 & _not_left_col) & (landlocking >> w) & (landlocking << w)
        mask |= ore & ~landlocked & builder._harvest_zone
    # if avoid_core:
    #     mask |= _bm_my_core_area
    if avoid_builders:
        mask |= _bm_friendly_bots | _bm_enemy_bots
    if not enemy_pov:
        pos = _my_pos
        my_bit = 1 << (pos.x + pos.y * _width)
        # Always avoid enemy gunner fire lines. The builder's own tile is
        # excluded so that a builder already being shot can still step off the
        # ray (rather than the whole mask being dropped, which let it linger).
        mask |= _bm_enemy_hard_threat & ~my_bit
        mask |= _bm_enemy_launch_adj
        # Same for our own gunners' fire lines — don't block their shots.
        mask |= _bm_my_gunner_claims & ~my_bit
    return mask
