from __future__ import annotations
from main import has_op
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

_ACCEPT_ORE = frozenset({
    EntityType.CONVEYOR,
    EntityType.SPLITTER,
    EntityType.CORE,
})

_TURRET_TYPES = frozenset({
    EntityType.LAUNCHER,
    EntityType.GUNNER,
    EntityType.SENTINEL,
})

_ET_BARRIER           = EntityType.BARRIER
_ET_CONVEYOR          = EntityType.CONVEYOR
_ET_SPLITTER          = EntityType.SPLITTER
_ET_CORE              = EntityType.CORE
_ET_BUILDER_BOT       = EntityType.BUILDER_BOT
_ET_HARVESTER         = EntityType.HARVESTER
_ET_LAUNCHER          = EntityType.LAUNCHER
_ET_GUNNER            = EntityType.GUNNER
_ET_SENTINEL          = EntityType.SENTINEL

_RT_TITANIUM          = ResourceType.TITANIUM

_ENV_EMPTY   = Environment.EMPTY
_ENV_WALL    = Environment.WALL
_ENV_ORE_TI  = Environment.ORE_TITANIUM

_ET_INT =   {t: i for i, t in enumerate(EntityType)}
_INT_ET =   {i: t for i, t in enumerate(EntityType)}
_RT_INT =   {t: i for i, t in enumerate(ResourceType)}
_INT_RT =   {i: t for i, t in enumerate(ResourceType)}
_ENV_INT =  {t: i for i, t in enumerate(Environment)}
_INT_ENV =  {i: t for i, t in enumerate(Environment)}
_DIR_INT =  {t: i for i, t in enumerate(Direction)}
_INT_DIR =  {i: t for i, t in enumerate(Direction)}
_TM_INT =   {t: i for i, t in enumerate(Team)}
_INT_TM =   {i: t for i, t in enumerate(Team)}

# Pre-computed indices for fast list access
_IDX_CONVEYOR          = _ET_INT[EntityType.CONVEYOR]
_IDX_SPLITTER          = _ET_INT[EntityType.SPLITTER]
_IDX_CORE              = _ET_INT[EntityType.CORE]
_IDX_HARVESTER         = _ET_INT[EntityType.HARVESTER]
_IDX_BARRIER           = _ET_INT[EntityType.BARRIER]
_IDX_GUNNER            = _ET_INT[EntityType.GUNNER]
_IDX_SENTINEL          = _ET_INT[EntityType.SENTINEL]
_IDX_LAUNCHER          = _ET_INT[EntityType.LAUNCHER]

_IDX_BUILDER_BOT       = _ET_INT[EntityType.BUILDER_BOT]

_MAX_HP_BY_IDX = [0] * len(EntityType)
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

_NUM_ET   = len(EntityType)
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
_CARDINAL = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)
_DIRECTION_DELTAS = {d: d.delta() for d in Direction}
# Int-indexed version: _DIRECTION_DELTAS_I[dir_int] = (dx, dy)
_DIRECTION_DELTAS_I = [d.delta() for d in Direction]

_DIR_N  = _DIR_INT[Direction.NORTH]
_DIR_NE = _DIR_INT[Direction.NORTHEAST]
_DIR_E  = _DIR_INT[Direction.EAST]
_DIR_SE = _DIR_INT[Direction.SOUTHEAST]
_DIR_S  = _DIR_INT[Direction.SOUTH]
_DIR_SW = _DIR_INT[Direction.SOUTHWEST]
_DIR_W  = _DIR_INT[Direction.WEST]
_DIR_NW = _DIR_INT[Direction.NORTHWEST]
_DIR_C  = _DIR_INT[Direction.CENTRE]

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

# Titanium every build action must leave in the bank, so the core can always
# answer a sentry alarm with a defender. Heimdall's defence is spawned on demand,
# so "couldn't afford a builder this round" is the one way it fails outright.
#
# Capped rather than tracking the full scaled builder cost: builders scale +20%
# each, so by mid-game the true spawn cost passes 150 Ti and reserving all of it
# would strangle the economy far more than a missed block costs.
# The cap matters: the true cost rises past 150 Ti by mid-game and reserving all
# of it strangles the economy far more than a missed block costs.
#
# Holding it unconditionally (rather than only while a threat is live) measured
# better on both opponents tested — an on-demand version cost ~10 points against
# Khaos and gained nothing against Heimdall v3 — so this stays simple.
TI_RESERVE_CAP = 60
# Tried and rejected: scaling the reserve to current titanium
# (min(cap, resources/3)) so it could never freeze construction when poor. The
# reasoning looked sound — ladder replays show antler games where we sit at
# 20-77 Ti all game, so a flat 40 makes a 3 Ti conveyor cost 43 — but it lost
# 9.5 points over the full suite (62.5% -> 53.0%, Heimdall v3 down to 39.4%) and
# did not fix antler either. The flat floor is load-bearing somewhere else.


def arm_reserve(threat_now: bool) -> None:
    """No-op kept so callers need not care whether the reserve is conditional."""


def ti_reserve() -> float:
    """Titanium a build action must leave unspent, so an alarm is always answerable."""
    return int(TI_RESERVE_CAP*_rc.get_scale_percent()/100)

_prev_pos: Position = None
_my_pos: Position = None           # cached rc.get_position(), updated on move
_my_team: Team = None
_my_team_idx: int = 0

# Per-tile arrays (scalar values that can't be bitmasks)
_building_id: list[int] = []
_building_et_idx: list[int] = []
_building_hp: list[int] = []
_building_hp_prev: list[int] = []   # snapshot of _building_hp at the end of last update
_building_dir: list[int] = []
_building_conv_target: list[int] = []

_conv_reverse: list[int] = []   # reverse[tn] = bitmask of conveyor-type buildings (either team) with any output to tile tn

# Hop distance from each of my conveyors to my core along the belt: 1 = points
# into the core, 2 = points into a conveyor that points into the core, etc.
# -1 for any tile that isn't one of my conveyors connected to the core (walls,
# empty tiles, enemy convs, and my convs whose chain never reaches the core).
# Rebuilt by `_compute_route_reaches_core()` (gated on `_struct_version`).
conv_dist_core: list[int] = []

# Bitmask lists indexed by _ET_INT / _TM_INT / _ENV_INT
_bm_et: list[int] = []      # one bitmask per EntityType
_bm_team: list[int] = []    # one bitmask per Team
_bm_env: list[int] = []     # one bitmask per Environment
_bm_seen: int = 0           # seen tiles (observed OR derived via symmetry)
_bm_seen_observed: int = 0  # seen tiles (directly observed only)
_bm_any_building: int = 0   # union of all tracked building bitmasks
_bm_dir: list[int] = []   # per facing

# Derived bitmasks
_bm_blocked: int = 0            # walls + every building except conveyors/splitters + both core areas (ownership is irrelevant — see _recompute_derived_structural)
_bm_conveyors: int = 0          # all conveyor-type buildings
_bm_my_core_area: int = 0       # my core 2x2 (update only in update)
_bm_their_core_area: int = 0    # enemy core 2x2
_bm_enemy_launch_adj: int = 0   # tiles adjacent to enemy launchers (update only in update)
_bm_route_targets: int = 0      # tiles route state can path toward (update only in update)
_bm_reaches_core: int = 0       # TEMP/debug: my conveyors whose chain reaches my core
_bm_loaded_run: int = 0         # TEMP/debug: the loaded 4+ run tiles only (no chain flood)
_bm_conv_ti: int = 0            # conveyors observed containing titanium
_bm_ti_carrying: int = 0       # conveyors believed to carry titanium (within 3 up/downstream of an observed ti conveyor)
# Per-conveyor "load" = 1 / (spacing of titanium along the belt), in [0, 1].
# For a conveyor that has ti: 1 / (hops DOWNSTREAM to the next ti conveyor).
# For a conveyor without ti: 1 / (hops up to nearest ti + hops down to nearest
# ti) -- the gap it sits in. 0.0 where undefined (no conveyor, or no downstream
# ti reference). Rebuilt by `_compute_conv_load()` off the conv_target chain.
conv_load: list[float] = []
# The same load split into four bitmask buckets by exact ti spacing, so callers can
# score it with bit ops instead of looping. Bucketed by `dist` (hops between ti):
# [3]=dist 1 (densest, back-to-back ti), [2]=dist 2, [1]=dist 3, [0]=dist >= 4 (any
# sparser but still real ti reference). Load 0 (no ti reference) is in no bucket.
conv_load_buckets: list[int] = [0, 0, 0, 0]
# Conveyors whose titanium is jammed (not physically moving). A visible conveyor
# is "base-stuck" when the resource-stack id sitting on it hasn't changed for long
# enough that its not-stuck grace expired; ANY id change (a stack advanced off it,
# arrived, or drained) refreshes the grace to _CONV_STUCK_GRACE turns, so a belt
# whose load moves at least once every 4 turns never reads as stuck. Base-stuck then
# floods UPSTREAM along the flow graph (a jam backs up the belts feeding it).
# Computed in _recompute_derived_loaded alongside _bm_ti_carrying, and PERSISTENT
# just like it: a conveyor's base-stuck bit is refreshed while the tile is visible
# and frozen at its last-observed value while out of vision, and the flood runs over
# every known conveyor -- so conv_stuck covers out-of-vision tiles too. AND
# `~conv_stuck` into any "loaded conveyor" test to target only belts whose titanium
# is actually flowing.
conv_stuck: int = 0
_conv_base_stuck: int = 0                # persistent: last-observed base-stuck bit per conveyor
_conv_res_id: dict[int, int] = {}       # conveyor tile -> last-observed resource-stack id (0 = empty)
_conv_stuck_grace: dict[int, int] = {}  # conveyor tile -> turns of forced-not-stuck left
_conv_stuck_round: int = -1             # round conv_stuck was last computed (compute once/turn)
_CONV_STUCK_GRACE = 4
_bm_dead_end: int = 0           # possible places to route from, defined by the targets of any conveyor types heading into nothing or a building that is not a (conveyor type, my core, my sentinel, or my gunner). also includes my conveyors pointing into an enemy building (update only in update)
_bm_my_turret_claims: int = 0     # tiles already covered by one of my turrets (gunner ray or sentinel line) (update only in update)
_bm_my_gunner_rays: int = 0       # just the GUNNER-ray tiles of the above -- blockable line of fire; placement avoids these so a new turret can't block my gunner (update only in update)
_bm_conv_by_dir: list[int] = [0] * 8  # per facing: CONVEYOR tiles with that direction
_bm_enemy_turret_threat: int = 0 # tiles any enemy turret (sentinel/gunner) can shoot = gunner | sentinel (update only in update)
_bm_enemy_gunner_threat: int = 0   # tiles an enemy GUNNER can shoot (GUNNER_DAMAGE each)
_bm_enemy_sentinel_threat: int = 0 # tiles an enemy SENTINEL can shoot (SENTINEL_DAMAGE each)
_bm_others_5x5: int = 0          # 5x5 around other friendly builder bots
_bm_others_3x3: int = 0          # 3x3 around other friendly builder bots

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
_bm_hp_changed: int = 0           # visible building tiles whose HP changed vs last turn (attacked/healed)

# Builder bot tracking
_bm_friendly_bots: int = 0       # bitmask of known friendly builder bot positions
_bm_rusher_bots: int = 0         # subset of friendly bots that broadcast the rush flag
_bm_enemy_bots: int = 0          # bitmask of known enemy builder bot positions
_bm_friendly_stationary: int = 0 # friendly bots seen on the SAME tile last turn and this turn (parked -- routed around, not followed, in the is_route=False avoid)
_bot_pos: dict[int, int] = {}    # uid -> tile index (both teams)
_bot_team: dict[int, int] = {}   # uid -> team_idx
_bot_at: dict[int, int] = {}    # tile index -> uid
_bot_last_seen: dict[int, int] = {}   # uid -> round it was last seen alive in vision
_bot_pos_history: dict[int, list[int]] = {}  # uid -> newest-first observed tile indices
_BOT_HISTORY_LIMIT = 12

_max_id_by_round: list[int] = []  # max_id_by_round[round] = max entity id seen up to that round
_max_id_seen: int = 0
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
    """Sentinel: a WIDTH-1 line of fire along the facing direction, out to
    range sqrt(32) (i.e. dist^2 <= 32). No perpendicular spread. Offsets are
    ordered near-to-far. This gives a cardinal line of 5 and a diagonal line
    of 4 (2*4^2 == 32)."""
    RANGE_SQ = 32
    result = [[] for _ in range(8)]
    for di in range(8):
        ddx, ddy = _DIRECTION_DELTAS_I[di]
        tiles = []
        step = 1
        while (ddx * step) ** 2 + (ddy * step) ** 2 <= RANGE_SQ:
            tiles.append((ddx * step, ddy * step))
            step += 1
        result[di] = tiles
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
    # Off-board coordinates read as unknown, not as a crash. `1 << negative`
    # raises ValueError, and callers build candidate tiles by adding offsets to a
    # position without bounds-checking -- so a core against the left or top edge
    # threw here on its first turn and every turn after, for the whole game
    # (permanently destroying the unit).
    if not (0 <= x < _width and 0 <= y < _height):
        return None
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
def in_bounds_coords(x, y) -> bool:
    return 0 <= x < _width and 0 <= y < _height


def positions_to_mask(positions) -> int:
    """Convert an iterable of Positions to a bitmask."""
    mask = 0
    w = _width
    for p in positions:
        mask |= 1 << (p.x + p.y * w)
    return mask

def iter_mask(mask):
    """Yield Positions from a bitmask."""
    w = _width
    while mask:
        lsb = mask & -mask
        n = lsb.bit_length() - 1
        yield Position(n % w, n // w)
        mask ^= lsb


def bot_position_history(uid: int) -> tuple[int, ...]:
    return tuple(_bot_pos_history.get(uid, ()))


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

def chebyshev(mask: int, times:int = 1) -> int:
    w = _width
    for i in range(times):
        h = ((mask & _not_right_col) << 1) | ((mask & _not_left_col) >> 1)
        mask = (h | (h << w) | (h >> w) | (mask << w) | (mask >> w)) & _board_mask
    return mask


def manhattan(mask: int, times:int = 1) -> int:
    w = _width
    for i in range(times):
        mask = (((mask & _not_right_col) << 1) | ((mask & _not_left_col) >> 1) | (mask << w) | (mask >> w)) & _board_mask
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
_turret_threat_cache: int = 0


def _compute_enemy_gunner_threat() -> int:
    """Tiles an enemy GUNNER can shoot (GUNNER_DAMAGE each).

    A gunner fires a single ray in its current facing and is absorbed by the
    first occupied tile, so it cannot shoot over obstacles. Walls and non-conveyor
    buildings stop the ray -- and a builder can't stand on them, so they aren't
    threatened. A conveyor/splitter IS walkable: a builder standing on one gets
    shot (not the conveyor), so that tile IS threatened and the ray stops there.
    Only empty tiles let the ray continue. Bots are ignored (the mask is
    struct-versioned and can't track their movement)."""
    enemy_idx = 1 - _my_team_idx
    gunners = _bm_et[_IDX_GUNNER] & _bm_team[enemy_idx]
    if not gunners:
        return 0
    w = _width
    not_walls = _board_mask & ~_bm_env[_IDX_ENV_WALL]
    blocked = _bm_blocked            # walls + non-conveyor buildings + core areas
    convs = _bm_conveyors            # walkable buildings (conveyors/splitters)
    threat = 0
    for di in range(8):
        dm = gunners & _bm_dir[di]
        if not dm:
            continue
        dx, dy = _DIRECTION_DELTAS_I[di]
        length = 3 - di % 2
        shift_mask = _turret_shift_masks.get((dx, dy))
        offset = dx + dy * w
        for _i in range(length):
            if offset > 0:
                dm = ((dm & shift_mask) << offset) & not_walls
            else:
                dm = ((dm & shift_mask) >> (-offset)) & not_walls
            # Empty and conveyor tiles are threatened (a builder can be there); a
            # non-conveyor building isn't a valid builder position.
            threat |= dm & ~blocked
            # The ray continues only through EMPTY tiles -- it is absorbed at any
            # conveyor (builder/conveyor takes the shot) or building.
            dm &= ~blocked & ~convs
    return threat


def _compute_enemy_sentinel_threat() -> int:
    """Tiles an enemy SENTINEL can shoot (SENTINEL_DAMAGE each): its line of fire
    (cardinal 5 / diagonal 4), no LOS blocking (matches the sentinel targeting
    model, which hits its full offset pattern regardless of intervening tiles)."""
    enemy_idx = 1 - _my_team_idx
    sentinels = _bm_et[_IDX_SENTINEL] & _bm_team[enemy_idx]
    if not sentinels:
        return 0
    w = _width
    threat = 0
    for di in range(8):
        dm = sentinels & _bm_dir[di]
        if not dm:
            continue
        for dx, dy in _SENTINEL_OFFSETS[di]:
            shift_mask = _turret_shift_masks.get((dx, dy))
            offset = dx + dy * w
            if offset > 0:
                threat |= (dm & shift_mask) << offset
            else:
                threat |= (dm & shift_mask) >> (-offset)
    return threat & _board_mask


_GUNNER_DAMAGE = GameConstants.GUNNER_DAMAGE       # 7
_SENTINEL_DAMAGE = GameConstants.SENTINEL_DAMAGE   # 18


def lethal_mask(hp: int) -> int:
    """Tiles where a builder with `hp` HP would DIE to one turn of enemy turret
    fire: GUNNER_DAMAGE (7) per covering gunner, SENTINEL_DAMAGE (18) per sentinel,
    so a tile covered by one of each is 25. Assumes at most one turret of each
    type covers a tile (the threat masks are boolean)."""
    gun = _bm_enemy_gunner_threat
    sen = _bm_enemy_sentinel_threat
    if hp <= _GUNNER_DAMAGE:
        return gun | sen                  # any single turret kills
    if hp <= _SENTINEL_DAMAGE:
        return sen                        # a sentinel kills; a gunner (7) doesn't
    if hp <= _GUNNER_DAMAGE + _SENTINEL_DAMAGE:
        return gun & sen                  # only a gunner+sentinel overlap kills
    return 0                              # survive any one gunner + one sentinel


_my_turret_claims_cache_version: int = -1
_my_turret_claims_cache: int = 0
# Just the GUNNER-ray portion of the claims (sentinels excluded). A gunner ray is
# absorbed by the first building in its path, so dropping a turret onto one of
# these tiles blocks the shot behind it -- placement must avoid them. Sentinel
# lines are NOT blockable (they shoot through everything), so they don't belong
# in this mask. Populated as a side effect of _compute_my_turret_claims().
_my_gunner_rays_cache: int = 0


def _compute_my_turret_claims() -> int:
    """Bitmask of tiles already covered by one of my turrets -- a gunner's
    current ray, or a sentinel's line of fire. Placement/rotation scoring
    subtracts this so neither a gunner nor a sentinel overshoots a target that
    another of my turrets already covers."""
    global _my_turret_claims_cache_version, _my_turret_claims_cache, _my_gunner_rays_cache
    if _struct_version == _my_turret_claims_cache_version:
        return _my_turret_claims_cache

    w = _width
    my = _bm_team[_my_team_idx]
    gunners = _bm_et[_IDX_GUNNER] & my
    sentinels = _bm_et[_IDX_SENTINEL] & my
    claimed = 0
    gunner_rays = 0
    if gunners or sentinels:
        not_walls = _board_mask & ~_bm_env[_IDX_ENV_WALL]
        # The real gunner ray is absorbed by the first occupied tile: it can hit a
        # unit on that tile but not anything behind it (see the sandbox model in
        # engine.py `_first_gunner_target`). `_bm_blocked` is every building except
        # conveyors/splitters, so OR those back in to get "walls + all buildings".
        # Bots would block too, but the claims cache is keyed on `_struct_version`
        # and cannot see bot movement, so we only stop at (struct-versioned)
        # buildings; over-claiming through a transient bot is rare and self-heals.
        blockers = _bm_blocked | _bm_conveyors
        for di in range(8):
            dx, dy = _DIRECTION_DELTAS_I[di]
            shift_mask = _turret_shift_masks.get((dx, dy))
            offset = dx + dy * w

            # Gunner ray: length 3 cardinal / 2 diagonal, stops at any building.
            dm = gunners & _bm_dir[di]
            if dm:
                for _i in range(3 - di % 2):
                    dm = ((dm & shift_mask) << offset) & not_walls if offset > 0 \
                        else ((dm & shift_mask) >> (-offset)) & not_walls
                    claimed |= dm
                    gunner_rays |= dm
                    dm &= ~blockers          # a building here is a target but stops the ray

            # Sentinel line of fire: length 5 cardinal / 4 diagonal, NO LOS
            # blocking at all -- not even walls. This matches the sentinel scorer,
            # whose shift plans are static geometry with no wall/building mask
            # (unlike the gunner scorer, which blocks dynamically). If the claim
            # stopped at walls but the scorer shoots through them, a second
            # sentinel would keep getting placed onto a target the first already
            # "covers" in the scorer's eyes. Clip to the board only.
            dm = sentinels & _bm_dir[di]
            if dm:
                for _i in range(len(_SENTINEL_OFFSETS[di])):
                    dm = ((dm & shift_mask) << offset) & _board_mask if offset > 0 \
                        else ((dm & shift_mask) >> (-offset)) & _board_mask
                    claimed |= dm
    _my_turret_claims_cache_version = _struct_version
    _my_turret_claims_cache = claimed
    _my_gunner_rays_cache = gunner_rays
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
    tiles = _width * _height
    outputs = (1 << target_n) if 0 <= target_n < tiles else 0
    # A splitter whose primary output faces off the board (target_n < 0) still
    # feeds its two on-board side belts, so fall through to compute them rather
    # than early-returning on the primary target alone.
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


def _takers_of(tiles: int, convs: int) -> int:
    """Conveyor-likes in `convs` that draw from any tile in `tiles`.

    A conveyor accepts input from every adjacent tile except the one it points
    into. This is the same relation `route.not_blocked` encodes in the opposite
    direction (there: which tiles already have something drawing from them), so
    keep the two in step.
    """
    if not tiles or not convs:
        return 0
    w = _width
    dir_mask = _bm_dir
    return (
        (((tiles & _not_right_col) << 1) & convs & ~dir_mask[_DIR_W])
        | (((tiles & _not_left_col) >> 1) & convs & ~dir_mask[_DIR_E])
        | (((tiles & _not_bottom_row) << w) & convs & ~dir_mask[_DIR_N])
        | (((tiles & _not_top_row) >> w) & convs & ~dir_mask[_DIR_S])
    ) & _board_mask


def _conveyor_target_tiles(source_mask: int) -> int:
    """Return the union of output target tiles for the given conveyor-like
    sources. Splitters include their primary output and both side outputs."""
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
    """Per facing (0..7): CONVEYOR tiles with that output direction. Cached on
    _struct_version — only rebuilt on structural changes
    (conveyor build/destroy/redirect)."""
    global _conv_by_dir_cache_version, _conv_by_dir_cache
    if _struct_version == _conv_by_dir_cache_version:
        return _conv_by_dir_cache

    convs = _bm_et[_IDX_CONVEYOR]
    result = [convs & _bm_dir[d] for d in range(8)]

    _conv_by_dir_cache_version = _struct_version
    _conv_by_dir_cache = result
    return result


_carrying_cache_key: tuple | None = None
_carrying_cache: int = 0


def _carrying_expand(
    seed, bm_conveyors, reverse, harvesters, w, not_left_col, not_right_col,
):
    expanded = seed
    # Upstream (reverse chain). A carrying conveyor Y's feeder is also carrying
    # ONLY when Y's titanium source is unambiguous: exactly one conveyor points
    # into Y and no harvester sits beside Y. If two conveyors feed Y (either
    # could be the carrier), or one conveyor plus a harvester adjacent to Y (the
    # ti could be the harvester's), we can't attribute the load, so that branch
    # stops. Observed-carrying conveyors are seeded directly, so this only gates
    # the INFERRED upstream neighbours.
    cur = seed
    for _ in range(3):
        nxt = 0
        m = cur
        while m:
            lsb = m & -m
            yn = lsb.bit_length() - 1
            feeders = reverse[yn] & bm_conveyors
            if feeders and (feeders & (feeders - 1)) == 0:      # exactly one feeder
                adj = (((lsb & not_left_col) >> 1) | ((lsb & not_right_col) << 1)
                       | (lsb >> w) | (lsb << w))
                if not (adj & harvesters):                      # no alternative source
                    nxt |= feeders & ~expanded
            m ^= lsb
        if not nxt:
            break
        expanded |= nxt
        cur = nxt
    # Downstream (conv_target chain): whatever a carrying belt points into
    # receives its titanium, so it carries too -- always unambiguous.
    cur = seed
    for _ in range(3):
        nxt = _conveyor_target_tiles(cur) & bm_conveyors & ~expanded
        if not nxt:
            break
        expanded |= nxt
        cur = nxt
    return expanded


def _compute_carrying() -> int:
    """Bitmask of conveyors believed to carry titanium.

    A conveyor Y is believed to carry titanium if EITHER:
      - any conveyor within 3 upstream or 3 downstream hops of Y (inclusive) is
        observed carrying it, OR
      - Y is within 3 hops DOWNSTREAM (inclusive) of a conveyor a harvester
        actually feeds -- harvesters are a continuous source, so their belt is
        believed loaded even with no observed stack. Forward-only, belief-only.
    """
    global _carrying_cache_key, _carrying_cache
    key = (_struct_version, _bm_conv_ti)
    if key == _carrying_cache_key:
        return _carrying_cache
    bm_conveyors = _bm_conveyors
    if not bm_conveyors:
        _carrying_cache_key = key
        _carrying_cache = 0
        return 0
    reverse = _conv_reverse
    harvesters = _bm_et[_IDX_HARVESTER]
    w = _width
    nlc = _not_left_col
    nrc = _not_right_col

    ti_seed = _bm_conv_ti & bm_conveyors
    ti = (
        _carrying_expand(ti_seed, bm_conveyors, reverse, harvesters, w, nlc, nrc)
        if ti_seed else 0
    )

    # A harvester is a continuous titanium source, so a conveyor it actually feeds
    # is believed to carry -- and so is everything up to 3 hops DOWNSTREAM of it --
    # even when we have never observed a stack on it. This seed is forward-only: it
    # NEVER expands upstream, and it only feeds the *believed* set (`_bm_ti_carrying`);
    # `_bm_conv_ti` (actual/observed carrying) is untouched, so jam detection and any
    # logic that reads real titanium is unaffected. "Fed" means the harvester sits on
    # one of the conveyor's three accepting sides -- i.e. NOT the side it outputs to
    # (a harvester on the output side never delivers into the belt).
    convs = _bm_et[_IDX_CONVEYOR]
    if harvesters and convs:
        ntr, nbr = _not_top_row, _not_bottom_row
        dm = _bm_dir
        # Conveyors that have a harvester on a given side.
        h_on_N = (harvesters & nbr) << w          # harvester north of the conveyor
        h_on_S = (harvesters & ntr) >> w          # harvester south of the conveyor
        h_on_E = (harvesters & nlc) >> 1          # harvester east of the conveyor
        h_on_W = (harvesters & nrc) << 1          # harvester west of the conveyor
        # Fed = harvester on any accepting side (all sides except the output side).
        fed = (
            (convs & dm[_DIR_E] & (h_on_N | h_on_S | h_on_W))
            | (convs & dm[_DIR_W] & (h_on_N | h_on_S | h_on_E))
            | (convs & dm[_DIR_N] & (h_on_S | h_on_E | h_on_W))
            | (convs & dm[_DIR_S] & (h_on_N | h_on_E | h_on_W))
        )
        if fed:
            ti |= fed
            cur = fed
            for _ in range(3):                    # seed + 3 hops downstream
                nxt = _conveyor_target_tiles(cur) & bm_conveyors & ~ti
                if not nxt:
                    break
                ti |= nxt
                cur = nxt

    _carrying_cache_key = key
    _carrying_cache = ti
    return ti


def end_cost_exempt_conveyors() -> int:
    """Conveyors that attach to the network for FREE -- exempt from the conveyor
    end-cost penalty in bfs_route: any of MY conveyors whose titanium load is NOT
    in the top quartile (the last `conv_load_buckets` bucket).

    Only a belt that is already near-full should cost extra to pile onto; a belt
    with spare capacity (or an empty one, which is in no bucket) is fine to attach
    to. Load is observed-ti only (`_bm_conv_ti`), recomputed each turn by
    `_compute_conv_load`, so this is just a couple of bit ops -- no caching."""
    return _bm_conveyors & _bm_team[_my_team_idx] & ~conv_load_buckets[3]


_strike_zone_key = None
_strike_zone_cache = 0

def enemy_core_strike_zone() -> int:
    """Bitmask of every tile within Chebyshev-4 of an enemy-core tile -- the seen core
    if we have it, else the symmetry-predicted core. 0 when we have no idea where
    their core is. Cached on the core mask it was built from."""
    global _strike_zone_key, _strike_zone_cache
    core = _bm_their_core_area
    if core == 0:
        p = _compute_predicted_enemy_core()
        if p is None:
            return 0
        for dx in (0, 1):
            for dy in (0, 1):
                x, y = p.x + dx, p.y + dy
                if 0 <= x < _width and 0 <= y < _height:
                    core |= 1 << (x + y * _width)
        if core == 0:
            return 0
    if core != _strike_zone_key:
        _strike_zone_key = core
        _strike_zone_cache = expand_chebyshev(core, 4)
    return _strike_zone_cache


def enemy_undeveloped() -> bool:
    """True while the enemy has almost nothing: at most ONE known enemy builder bot
    and FEWER THAN TWO enemy harvesters. This is the gate that turns the aggressive
    sentinel-vs-enemy-core rush ON (attack) -- and, while it's on, keeps the
    harassment states (block/disrupt/chip) OFF, so builders stay on the rush and our
    own economy instead of poking a bot that has no economy worth disrupting."""
    if _bm_enemy_bots.bit_count() > 1:
        return False
    enemy = _bm_team[1 - _my_team_idx]
    return (_bm_et[_IDX_HARVESTER] & enemy).bit_count() < 2


def update_at(pos: Position) -> None:
    """Re-scan a single tile from the controller and update all per-tile state.

    Maintains env/seen/symmetry tracking, raw building state, core detection,
    and conveyor titanium observation. Does NOT touch derived bitmasks rebuilt
    by `recompute_derived()` (e.g. `_bm_blocked`, `_bm_conveyors`); callers are
    expected to call `recompute_derived()` after iterating.
    """
    global _bm_seen, _bm_seen_observed, _bm_any_building
    global _bm_conv_ti
    global _bm_damaged, _bm_very_damaged
    global _hor_sym, _ver_sym, _rot_sym
    global _max_id_seen, _my_core, _their_core, _core_id, _predicted_enemy_core
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
    _bm_seen_observed |= bit
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
        # and _compute_my_turret_claims, so invalidate those caches.
        if env_idx == _IDX_ENV_WALL:
            _struct_version += 1

    # Walls can never hold buildings and never change. Skip the controller
    # building lookup and all building-state work — saves get_tile_building_id
    # calls (one of the heaviest controller methods in the profile).
    if bm_env[_IDX_ENV_WALL] & bit:
        return

    # --- Building state ---
    entity_id = get_tile_building_id(pos)
    if entity_id is not None and entity_id > _max_id_seen:
        _max_id_seen = entity_id

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
            if res is _RT_TITANIUM:
                _bm_conv_ti |= bit
            else:
                _bm_conv_ti &= nbit
        return

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
        tx = x + dx
        ty = y + dy
        if 0 <= tx < width and 0 <= ty < height:
            target = Position(tx, ty)

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

    # Call for every conveyor-like (symmetric with the removal path above): a
    # splitter still has on-board side outputs when its primary faces off-board,
    # and `_conv_output_mask` returns 0 for a plain conveyor with new_tn < 0.
    if is_conveyor[et_idx]:
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
        if res is _RT_TITANIUM:
            _bm_conv_ti |= bit
        else:
            _bm_conv_ti &= nbit

    # First-sight core detection
    if et is _ET_CORE:
        if _my_core is None and team_val == _my_team:
            _my_core = core_top_left(entity_id, pos)
            _core_id = entity_id
            build_core_areas()
            _predicted_enemy_core = _compute_predicted_enemy_core()
        elif _their_core is None and team_val != _my_team:
            _their_core = core_top_left(entity_id, pos)
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

    recompute_derived()


def init(c: Controller):
    global _rc, _width, _height
    global _my_team, _my_team_idx
    global _prev_pos, _my_pos
    global _my_team, _my_team_idx
    global _building_id, _building_et_idx, _building_hp, _building_dir, _building_conv_target, _conv_reverse
    global conv_dist_core, conv_load, conv_load_buckets
    global conv_stuck, _conv_base_stuck, _conv_res_id, _conv_stuck_grace, _conv_stuck_round
    global _bm_et, _bm_team, _bm_env
    global _left_col, _right_col, _bottom_row, _top_row, _not_left_col, _not_right_col, _not_bottom_row, _not_top_row
    global _board_mask, _bm_dir
    global _struct_version
    global _turret_threat_cache_version, _turret_threat_cache
    global _my_turret_claims_cache_version, _my_turret_claims_cache
    global _conv_by_dir_cache_version, _conv_by_dir_cache
    global _route_targets_cache_key, _route_targets_cache
    global _route_reaches_core_cache_version, _route_reaches_core_cache
    global _recompute_structural_cache_version, _recompute_loaded_cache_key, _recompute_visible_cache_key
    global _bot_pos, _bot_team, _bot_at, _bot_last_seen, _bot_pos_history
    _avoid_cache.clear()
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
    conv_dist_core        = [-1] * tiles
    conv_load             = [0.0] * tiles
    conv_load_buckets     = [0, 0, 0, 0]
    conv_stuck            = 0
    _conv_base_stuck      = 0
    _conv_res_id          = {}
    _conv_stuck_grace     = {}
    _conv_stuck_round     = -1

    _bm_et   = [0] * _NUM_ET
    _bm_team = [0] * _NUM_TEAM
    _bm_env  = [0] * _NUM_ENV
    _bm_dir  = [0] * len(Direction)

    _struct_version = 0
    _turret_threat_cache_version = -1
    _turret_threat_cache = 0
    _my_turret_claims_cache_version = -1
    _my_turret_claims_cache = 0
    _conv_by_dir_cache_version = -1
    _conv_by_dir_cache = [0] * 8
    _route_targets_cache_key = None
    _route_targets_cache = (0, 0)
    _route_reaches_core_cache_version = -1
    _route_reaches_core_cache = (0, [])
    _recompute_structural_cache_version = -1
    _recompute_loaded_cache_key = None
    _recompute_visible_cache_key = None
    _bot_pos = {}
    _bot_team = {}
    _bot_at = {}
    _bot_last_seen = {}
    _bot_pos_history = {}

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


# --- new 2-slot comms integration (bit0=rot, bit1=ver, bit2=hor everywhere) ---
_comm_enemy_ids: dict = {}       # tile index -> enemy id mod 128 (this turn only)
_seen_enemy_ids: set = set()     # enemy ids (mod 128) this unit sees itself THIS turn
_enemy_ids_ever: set = set()     # every distinct enemy id (mod 128) observed all game
_seen_uids: set = set()          # builder-bot uids this unit sees itself THIS turn
# Claim tiebreak partition of _bm_friendly_bots (rebuilt each turn in set_comm_bots):
#   _bm_friendly_tie_lose -- other bots I WIN equal-distance ties against (I claim)
#   _bm_friendly_tie_win  -- other bots that WIN ties against me (they claim)
# I win iff I saw the bot THIS turn OR its id > mine; else (global/stale, lower id) it wins.
_bm_friendly_tie_lose: int = 0
_bm_friendly_tie_win: int = 0
_comm_core_sym3: int = 0         # last symmetry word the core broadcast
_sym3_cache_key = None
_sym3_cache_val = 0


def note_comm_sym_possible(mask3: int) -> None:
    """Fold a peer's still-possible-symmetry mask (bit0 rot, bit1 ver, bit2 hor):
    a symmetry someone has ruled out is truly ruled out for us too."""
    global _hor_sym, _ver_sym, _rot_sym
    if not (mask3 & 1):
        _rot_sym = False
    if not (mask3 & 2):
        _ver_sym = False
    if not (mask3 & 4):
        _hor_sym = False


def set_comm_core_sym(sym3: int) -> None:
    """Apply the core's symmetry verdict [solved:bit0 | type:bits1-2] (0=rot, 1=ver,
    2=hor). When solved, lock symmetry to that type."""
    global _hor_sym, _ver_sym, _rot_sym, _comm_core_sym3
    _comm_core_sym3 = sym3
    if sym3 & 1:                          # solved
        typ = (sym3 >> 1) & 3
        _rot_sym = (typ == 0)
        _ver_sym = (typ == 1)
        _hor_sym = (typ == 2)


def _sym_evidence(flip_fn) -> int:
    """# of seen tiles whose flip is also seen -- evidence weight for a symmetry
    that has not been ruled out (all such pairs are consistent by definition)."""
    w = _width
    seen = _bm_seen
    cnt = 0
    m = seen
    while m:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        fp = flip_fn(Position(n % w, n // w))
        if (seen >> (fp.x + fp.y * w)) & 1:
            cnt += 1
    return cnt


def comm_core_sym3() -> int:
    """The core's 3-bit symmetry word [solved | type]. solved iff exactly one type
    is still possible; type = the most-evidenced possible type, tiebreak rot>ver>hor.
    Cached on (seen set, possibility flags)."""
    global _sym3_cache_key, _sym3_cache_val
    key = (_bm_seen, _rot_sym, _ver_sym, _hor_sym)
    if key == _sym3_cache_key:
        return _sym3_cache_val
    alive = []
    if _rot_sym:
        alive.append((0, rot_flip))
    if _ver_sym:
        alive.append((1, ver_flip))
    if _hor_sym:
        alive.append((2, hor_flip))
    if not alive:
        val = 0
    else:
        solved = 1 if len(alive) == 1 else 0
        best_t, best_e = None, -1
        for t, f in alive:
            e = _sym_evidence(f)
            if e > best_e or (e == best_e and (best_t is None or t < best_t)):
                best_e, best_t = e, t
        val = (solved & 1) | ((best_t & 3) << 1)
    _sym3_cache_key = key
    _sym3_cache_val = val
    return val


def claim_bots() -> int:
    """`_bm_friendly_bots` with the dedicated rusher removed -- the competitor set for
    the economy claim contests (route/harvest/disrupt) the rusher will never service.
    The rusher is pair 0 (the first builder); its tile is masked out in set_comm_bots.
    Without this, a real builder yields a tied tile to the (closer) rusher, which then
    bails at its rush guard and never builds it, so the tile is stranded."""
    return _bm_friendly_bots & ~_bm_rusher_bots


def set_comm_bots(friendly_claims, enemy_pos_ids, rusher_id: int = 0) -> None:
    """Fold globally-shared builder positions from comms into the bot masks (which
    update() already built from local vision). Call after update() + comms.read(),
    before recompute_derived(). friendly_claims: list[(Position, owner id mod 128)];
    enemy_pos_ids: list[(Position, id mod 128)]."""
    global _bm_friendly_bots, _bm_enemy_bots, _comm_enemy_ids
    global _bm_friendly_tie_lose, _bm_friendly_tie_win, _bm_rusher_bots
    w = _width
    my_n = _my_pos.x + _my_pos.y * w
    my_r = _rc.get_id() & 127
    # Collapse each friendly bot to ONE entry at its FRESHEST known tile, keyed by id
    # (mod 128). Freshness rank: 2 = seen by me THIS turn, 1 = relayed claim (a bot's
    # own position from last turn), 0 = remembered (my own, possibly very stale). A bot
    # that moved must light one tile, not both its observed and its claimed tile.
    fmerge = {}   # id -> (tile, rank)
    for uid, tile in _bot_pos.items():
        if _bot_team.get(uid) != _my_team_idx:
            continue
        fid = uid & 127
        rank = 2 if uid in _seen_uids else 0
        cur = fmerge.get(fid)
        if cur is None or rank > cur[1]:
            fmerge[fid] = (tile, rank)
    unknown_tiles = []   # relayed claims whose owner id we don't know yet (fid == 0)
    for cp, fid in friendly_claims:
        tile = cp.x + cp.y * w
        if not fid:
            unknown_tiles.append(tile)
            continue
        cur = fmerge.get(fid)
        if cur is None or cur[1] < 1:      # relay (rank 1) beats remembered, yields to seen-now
            fmerge[fid] = (tile, 1)
    _bm_friendly_bots = 0
    tie_lose = 0
    tie_win = 0
    for fid, (tile, rank) in fmerge.items():
        if tile == my_n:
            continue
        bit = 1 << tile
        _bm_friendly_bots |= bit
        # I win the tie if I OBSERVED them this turn (rank 2) or their id is higher;
        # otherwise (global/stale info AND lower id) they win it.
        if rank == 2 or fid > my_r:
            tie_lose |= bit
        else:
            tie_win |= bit
    for tile in unknown_tiles:             # unknown owner -> take ties (don't yield blindly)
        if tile != my_n:
            bit = 1 << tile
            _bm_friendly_bots |= bit
            tie_lose |= bit
    tie_win &= ~tie_lose                    # lose (I win the tile) wins any tile overlap
    _bm_friendly_tie_lose = tie_lose
    _bm_friendly_tie_win = tie_win
    # Dedicated rusher (pair 0, the first builder) -> its freshest tile, so claim_bots()
    # can drop it from the economy claim contests. rusher_id 0 means not-yet-known.
    rusher = fmerge.get(rusher_id) if rusher_id else None
    _bm_rusher_bots = (1 << rusher[0]) if rusher is not None else 0

    # Dedupe enemies by id before building the mask. Two builders that each relayed
    # the SAME enemy at different tiles (it moved between their sightings) would
    # otherwise light two bits and look like two enemies. Build one id -> tile map,
    # latest observation winning, then rebuild the mask from it:
    #   base   -- what this unit knows locally (current + remembered), keyed mod 128
    #   comm   -- relayed sightings (last turn), applied only for enemies this unit
    #             does NOT see itself right now (a live local sighting is fresher)
    emap = {}
    for uid, tile in _bot_pos.items():
        if _bot_team.get(uid) != _my_team_idx:
            emap[uid & 127] = tile
    for cp, eid in enemy_pos_ids:
        if eid not in _seen_enemy_ids:
            emap[eid] = cp.x + cp.y * w
    _bm_enemy_bots = 0
    ids = {}
    for eid, n in emap.items():
        if n == my_n:
            continue
        _bm_enemy_bots |= 1 << n
        ids[n] = eid
    _comm_enemy_ids = ids
    global _enemy_ids_ever
    _enemy_ids_ever |= set(emap.keys())   # cumulative: every enemy id ever observed


def note_symmetry_conflict(n: int, env_idx: int) -> None:
    """Eliminate any symmetry under which tile `n`'s mirror is already seen with
    a different env. Mirrors the observation-based check in `update_at`, but for
    tiles learned through comms (which `update_at` never sees). This is what lets
    the core re-derive symmetry from the tiles builders relay, now that builders
    no longer broadcast their own symmetry bits. No-op once symmetry is solved.

    Call right after recording env `env_idx` at tile `n` in `_bm_env`/`_bm_seen`."""
    global _hor_sym, _ver_sym, _rot_sym
    if _solved_sym:
        return
    x = n % _width
    y = n // _width
    rx = _width - 1 - x
    ry = _height - 1 - y
    if _hor_sym:
        fbit = 1 << (rx + y * _width)
        if (_bm_seen & fbit) and not (_bm_env[env_idx] & fbit):
            _hor_sym = False
    if _ver_sym:
        fbit = 1 << (x + ry * _width)
        if (_bm_seen & fbit) and not (_bm_env[env_idx] & fbit):
            _ver_sym = False
    if _rot_sym:
        fbit = 1 << (rx + ry * _width)
        if (_bm_seen & fbit) and not (_bm_env[env_idx] & fbit):
            _rot_sym = False

def record_relayed_tile(n: int, env_idx: int) -> bool:
    """Record env `env_idx` at tile `n` (and its mirror) from a comms relay.

    The counterpart of `update_at`'s env/seen block for tiles this unit never
    saw itself, and the ONLY supported way for comms to write pooled terrain.
    It owns the same invariants `update_at` does -- the core-area skip, the
    symmetry-conflict note, the solved-symmetry mirror, and the `_struct_version`
    bump that invalidates every wall-derived cache -- so comms does not have to
    re-derive them by hand and drift when this file changes.

    Returns True if anything new was written (i.e. the caller should
    `recompute_derived()`)."""
    global _bm_seen, _struct_version
    bit = 1 << n
    # Core-area tiles are owned by build_core_areas(): update_at returns before
    # touching their env/seen state, so relayed hearsay must not fill it in
    # either. This is load-bearing, not just tidiness -- because those tiles are
    # never in _bm_seen, the relaying unit's own env lookup reports them as
    # EMPTY (the wire has no "unknown" code), so accepting them here would brand
    # a core footprint as permanently-seen empty floor.
    if (_bm_my_core_area | _bm_their_core_area) & bit:
        return False
    if _bm_seen & bit:
        return False
    _bm_env[env_idx] |= bit
    _bm_seen |= bit
    # Relayed tiles never pass through update_at, so derive symmetry here -- this
    # is how the core (and anyone else) infers symmetry from pooled vision.
    note_symmetry_conflict(n, env_idx)
    if _solved_sym:
        x = n % _width
        y = n // _width
        if _hor_sym:
            fx, fy = _width - 1 - x, y
        elif _ver_sym:
            fx, fy = x, _height - 1 - y
        elif _rot_sym:
            fx, fy = _width - 1 - x, _height - 1 - y
        else:
            fx = fy = -1
        if fx >= 0:
            fbit = 1 << (fx + fy * _width)
            # Only fill an UNSEEN mirror. update_at writes its mirror blind,
            # which is safe there because it has just observed the tile itself;
            # here the source is hearsay, and overwriting a mirror we already
            # know would leave two env bits set for one tile -- ground_at would
            # then answer WALL for something recorded as ore.
            if (not (_bm_seen & fbit)
                    and not ((_bm_my_core_area | _bm_their_core_area) & fbit)):
                _bm_env[env_idx] |= fbit
                _bm_seen |= fbit
    # Newly-recorded walls block gunner rays in _compute_enemy_turret_threat and
    # _compute_my_turret_claims and feed _bm_blocked, all of which are memoised
    # on _struct_version. Without this bump the caller's recompute_derived() is
    # a no-op and the wall never reaches _bm_blocked. Same reason as update_at.
    if env_idx == _IDX_ENV_WALL:
        _struct_version += 1
    return True


def hor_flip(pos: Position) -> Position:
    return Position(_width - 1 - pos.x, pos.y)
def ver_flip(pos: Position) -> Position:
    return Position(pos.x, _height - 1 - pos.y)
def rot_flip(pos: Position) -> Position:
    return Position(_width - 1 - pos.x, _height - 1 - pos.y)

def flip(pos: Position) -> Position | None:
    if not _solved_sym:
        return None
    if _hor_sym:
        return hor_flip(pos)
    if _ver_sym:
        return ver_flip(pos)
    if _rot_sym:
        return rot_flip(pos)
    return None


# Core-corner flips. `_my_core`/`_their_core` store the TOP-LEFT corner of the
# 2x2 core. A single-tile flip maps that corner to the mirrored region's FAR
# corner, so step back by one on each flipped axis to land on the mirrored 2x2's
# top-left corner.
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

def core_top_left(core_id: int, tile: Position) -> Position | None:
    """Given one visible tile of a 2x2 core, return the TOP-LEFT corner of that
    2x2 (the reference position stored in _my_core / _their_core), or None if the
    core's extent isn't yet determinable from what's visible.

    Each of the four 2x2 tiles has exactly one empty vertical side and one empty
    horizontal side (its two outward edges); the corner that pair points to tells
    us which of the four tiles this is, and hence the top-left corner."""
    def empty(pos: Position) -> bool:
        return not in_bounds(pos) or (_rc.is_in_vision(pos) and _rc.get_tile_building_id(pos) != core_id)
    up    = empty(Position(tile.x,     tile.y - 1))
    down  = empty(Position(tile.x,     tile.y + 1))
    left  = empty(Position(tile.x - 1, tile.y))
    right = empty(Position(tile.x + 1, tile.y))
    if up and left:    return Position(tile.x,     tile.y)      # this tile is top-left
    if up and right:   return Position(tile.x - 1, tile.y)      # this tile is top-right
    if down and left:  return Position(tile.x,     tile.y - 1)  # this tile is bottom-left
    if down and right: return Position(tile.x - 1, tile.y - 1)  # this tile is bottom-right
    return None

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
    global _route_reaches_core_cache_version, _route_reaches_core_cache, conv_dist_core
    if _struct_version == _route_reaches_core_cache_version:
        return _route_reaches_core_cache

    my_convs = _bm_conveyors & _bm_team[_my_team_idx]
    reverse = _conv_reverse
    reaches_core = 0
    order: list[int] = []
    # Per-tile hop distance to core, filled in as the BFS peels off each layer.
    dist = [-1] * (_width * _height)

    layer = 0
    c_mask = _bm_my_core_area
    while c_mask:
        lsb = c_mask & -c_mask
        n = lsb.bit_length() - 1
        layer |= reverse[n] & my_convs
        c_mask ^= lsb

    # Single walk per layer: append to `order` and accumulate `next_layer`
    # in the same LSB-extraction loop, instead of two separate passes. Each
    # layer is one more hop from the core, so `hop` is the distance stored in
    # `dist`; the `& ~reaches_core` below keeps the first (shortest) hop a tile
    # is reached at.
    order_append = order.append
    hop = 1
    while layer:
        reaches_core |= layer
        next_layer = 0
        m = layer
        while m:
            lsb = m & -m
            n = lsb.bit_length() - 1
            order_append(n)
            dist[n] = hop
            next_layer |= reverse[n]
            m ^= lsb
        layer = next_layer & my_convs & ~reaches_core
        hop += 1

    conv_dist_core = dist
    # Cache as list — callers only iterate, no need to pay for tuple().
    result = (reaches_core, order)
    _route_reaches_core_cache_version = _struct_version
    _route_reaches_core_cache = result
    return result


# How many hops downstream of a *visible* titanium source still count as a
# dead-end source. 1 is enough for the per-hop stall the walk exists to fix (the
# tip route just laid is exactly one hop below the conveyor feeding it); larger
# values additionally keep a stretch of conveyors that never received titanium
# extendable, which is otherwise unroutable forever at any distance from the
# core. Read it like PAYG_HORIZON in _config.py -- a knob to sweep, where too
# large means committing builders to chains whose source may already be dead.
DEAD_END_LOOKAHEAD = 3


def _compute_route_targets() -> int:
    """Bitmask of tiles the route state can path toward.

    Route targets = my conveyors whose downstream chain reaches my core area,
    minus any that are part of a connected run of 4+ believed-loaded conveyors.
    My core area is always routable.

    Side effect: sets `_bm_dead_end` to the targets of any *source* conveyor
    whose output is nothing or a building not in (conveyor-type, my core,
    my sentinel, my gunner). Also includes my conveyors pointing into an enemy
    building. Sources are the loaded conveyors plus everything within
    DEAD_END_LOOKAHEAD hops downstream of a visible titanium source.

    Note `_bm_dead_end` mixes two tile kinds, and route repairs them
    differently: an empty output tile (`tbit`) is where a new conveyor gets
    built, while the conveyor's own tile (`lsb`, the `hard_block` branch) is a
    conveyor whose output can neither accept titanium nor be built on, so route
    destroys and re-lays it facing a new direction.
    """
    my_team_idx = _my_team_idx
    bm_my = _bm_team[my_team_idx]
    my_convs = _bm_conveyors & bm_my
    # Believed-loaded (observed titanium expanded up/downstream), matching the
    # docstring above. Cache-safe: `_bm_ti_carrying` is a pure function of
    # (`_struct_version`, `_bm_conv_ti`) -- both in this function's cache key --
    # and `_recompute_derived_loaded` refreshes it before route runs.
    loaded_union = _bm_ti_carrying
    visible_loaded_mine = my_convs & loaded_union & _bm_visible
    global _bm_dead_end, _bm_reaches_core, _bm_loaded_run
    global _route_targets_cache_key, _route_targets_cache
    # Seeds for the DEAD_END_LOOKAHEAD walk below. Both are visibility-gated, so
    # they belong in the cache key: `visible_loaded_mine` does not move when only
    # a harvester enters or leaves vision, and serving a stale `_bm_dead_end`
    # from the early return is exactly the blindness this walk exists to remove.
    my_harvesters_seen = _bm_et[_IDX_HARVESTER] & bm_my & _bm_visible
    key = (
        _struct_version,
        _bm_conv_ti,
        visible_loaded_mine,
        my_harvesters_seen,
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
    enemy_bm = _bm_team[1 - my_team_idx]
    enemy_turret = (
        _bm_et[_IDX_GUNNER] | _bm_et[_IDX_SENTINEL]
    ) & enemy_bm
    hard_block = (
        enemy_turret
        | ((_bm_et[_IDX_LAUNCHER] | _bm_et[_IDX_BARRIER] | _bm_et[_IDX_CORE]) & enemy_bm)
        | _bm_et[_IDX_HARVESTER]
    )

    loaded_sources = all_convs & loaded_union

    # --- Extend the source set a bounded distance downstream of visible ------
    # titanium sources. A conveyor laid this turn is empty, so on a loaded-only
    # source set the chain it extends goes invisible to route for at least a
    # turn: the new tip is not loaded, and the conveyor now feeding it points
    # into an accepting building so it produces no dead end either. Route then
    # scores 0 and the builder is free to be claimed by a higher state, which is
    # how chains get abandoned a few hops short of the core.
    #
    # Two seed kinds. Visible loaded conveyors cover the general case; conveyors
    # drawing from one of my visible harvesters cover hop 2, where the harvester
    # has already stopped being an orphan candidate (route.not_blocked drops it
    # as soon as anything draws from it) but hop 1 has not received titanium yet.
    #
    # Seeds are visibility-gated on purpose. `loaded_union` (believed-loaded) is
    # built from `_bm_conv_ti`, which is sticky for tiles out of vision --
    # update_at only writes tiles we actually read -- and then propagated further,
    # so seeding on that remembered state would invent frontiers on chains last
    # looked at hundreds of rounds ago. The `& _bm_visible` on visible_loaded_mine
    # is what keeps the believed set from leaking into the seed walk.
    seeds = visible_loaded_mine | _takers_of(my_harvesters_seen, my_convs)
    extra = 0
    visited = seeds
    frontier = seeds
    for _ in range(DEAD_END_LOOKAHEAD):
        frontier = _conveyor_target_tiles(frontier) & my_convs & ~visited
        if not frontier:
            break
        visited |= frontier
        extra |= frontier

    # --- Dead-ends: targets of any source conveyor whose output isn't
    # accepting (or, for my conveyors, points into an enemy building).
    dead_ends = 0
    mask = loaded_sources | seeds | extra
    while mask:
        lsb = mask & -mask
        mask ^= lsb
        n = lsb.bit_length() - 1
        tn = conv_target[n]
        # Conveyors may legally face off the board (probed:
        # can_build_conveyor(Position(2, 0), NORTH) succeeds), in which case
        # update_at stores -1. Kept as a guard so a negative index can never
        # reach the shift below -- that raises ValueError, which main.Player.run
        # catches at the cost of the unit's ENTIRE turn.
        if tn < 0 or tn >= tiles:
            continue
        tbit = 1 << tn
        if hard_block & tbit:
            dead_ends |= lsb
        elif not (accepting & tbit):
            dead_ends |= tbit
        # A source of mine pointing into an ENEMY conveyor/splitter is not flagged
        # here on purpose: those targets are `accepting`, and the strip at the end
        # of this function (`_bm_dead_end &= ~(_bm_conveyors & ~bm_my)`) would drop
        # any such tile anyway, so a dedicated branch would be a pure no-op.
    _bm_dead_end = dead_ends

    # --- Overlay loaded/visible state on top of the structural conveyor graph.
    reaches_core, reaches_core_order = _compute_route_reaches_core()
    _bm_reaches_core = reaches_core          # TEMP/debug export
    # Saturation ("loaded run of 4+") counts OBSERVED titanium on consecutive
    # conveyors, not the ±3 believed-carry set: a real jam holds ti on adjacent
    # conveyors, whereas believed-carry bridges spaced-out stacks into a phantom
    # continuous run (the glacierkeep false positive). conv_load is not used here
    # either -- being downstream-only, it zeroes a jam's last tile and would make
    # the run length off by one. The dead-end/seed logic above keeps the smoothed
    # believed set on purpose.
    loaded_mine = my_convs & _bm_conv_ti
    run_visible_mine = loaded_mine & _bm_visible
    unroutable = 0
    _bm_loaded_run = 0                        # TEMP/debug
    if loaded_mine:
        run_loaded_arr = [0] * tiles
        ext_roots = 0
        run_visible_arr = [0] * tiles if run_visible_mine else None

        for n in reaches_core_order:
            lsb = 1 << n
            p = conv_target[n]
            if 0 <= p < tiles and (reaches_core & (1 << p)):
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
            if run_visible_arr is not None and (run_visible_mine & lsb):
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
        _bm_loaded_run = unroutable           # TEMP: loaded 4+ run only, before chain flood

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
_recompute_loaded_cache_key: tuple | None = None
_recompute_visible_cache_key: tuple | None = None


def _recompute_derived_structural() -> None:
    global _bm_blocked, _bm_conveyors
    global _bm_enemy_launch_adj
    global _bm_enemy_turret_threat, _bm_enemy_gunner_threat, _bm_enemy_sentinel_threat
    global _bm_my_turret_claims, _bm_my_gunner_rays, _bm_conv_by_dir
    global _recompute_structural_cache_version

    if _struct_version == _recompute_structural_cache_version:
        return
    _recompute_structural_cache_version = _struct_version

    # The get_avoid cache keys on `_struct_version`, but its inputs include
    # derived masks rebuilt right here (`_bm_conveyors`, `_bm_enemy_launch_adj`,
    # ...). Between a struct bump in `update_at` and this recompute those derived
    # masks lag the live ones (e.g. a fresh conveyor is already in
    # `_bm_any_building` but not yet in `_bm_conveyors`), so a get_avoid call in
    # that window caches a mask that (wrongly) treats the new conveyor as a solid
    # building. Same `_struct_version`, so that stale entry would never expire.
    # Drop the cache whenever the structural masks are rebuilt.
    _avoid_cache.clear()

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

    # Ownership does not make a building walkable. Probing the live controller
    # from a builder standing beside our own structures: adjacent to a FRIENDLY
    # barrier, `can_move` said no on 345 of 345 samples, and beside our own core
    # 15 of 15 — while the old mask here called both of those tiles free. Only
    # conveyors and splitters are genuinely walk-through, and they are already
    # excluded below by never being added.
    #
    # This matters beyond `_bm_blocked` itself: `passable()` (the graph under
    # every `nav.closest`, `nav.closest_within` and `pathing.claim_subset`) is
    # now `_board_mask & ~get_avoid(False)`, the SAME mask `bfs_move` moves on,
    # so the two can no longer disagree. Previously they did — the closest-query
    # graph called our own barriers free while `bfs_move` refused to step onto
    # them — which let cut claim seal tiles it could never reach: `closest`
    # reported the target four steps away straight through a barrier we had laid
    # ourselves, `bfs_move` refused the step, and the state re-claimed the same
    # tile every round (cut_walk_failed_reach4_seal=153 in a single instrumented
    # game; on saga our own barriers sat in the closest-query graph on 819 of 981
    # builder-turns, up to 13 at once — the map where we finish on fifteen
    # barriers and lose). Sourcing both from get_avoid closes that gap.
    #
    # Note the core has to come from `_bm_my_core_area` rather than
    # `bm_et[_IDX_CORE] & bm_team[...]`: the 2x2 footprint is synthesised by
    # `build_core_areas()`, and using the entity mask would work only after that
    # has run for a core we have actually seen.
    _bm_blocked = bm_env[_IDX_ENV_WALL]
    _bm_blocked |= bm_et[_IDX_HARVESTER]
    _bm_blocked |= bm_et[_IDX_GUNNER] | bm_et[_IDX_SENTINEL]
    _bm_blocked |= bm_et[_IDX_LAUNCHER]
    _bm_blocked |= bm_et[_IDX_BARRIER]
    _bm_blocked |= _bm_their_core_area
    _bm_blocked |= _bm_my_core_area

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

    _bm_enemy_gunner_threat = _compute_enemy_gunner_threat()
    _bm_enemy_sentinel_threat = _compute_enemy_sentinel_threat()
    _bm_enemy_turret_threat = _bm_enemy_gunner_threat | _bm_enemy_sentinel_threat
    _bm_my_turret_claims = _compute_my_turret_claims()   # also populates _my_gunner_rays_cache
    _bm_my_gunner_rays = _my_gunner_rays_cache
    _bm_conv_by_dir = _compute_conv_by_dir()


def _compute_conv_load() -> None:
    """Fill `conv_load[n]` = 1 / titanium spacing along the belt at conveyor n.

    Two reference points define a conveyor's spacing:
      * a conveyor that HAS ti -> distance DOWNSTREAM to the next ti conveyor
        (look downstream only, per design -- one point is itself);
      * a conveyor WITHOUT ti  -> (hops up to nearest ti) + (hops down to nearest
        ti), the gap it sits between.

    Computed entirely off the single-valued `conv_target` (downstream) chain: the
    nearest-upstream-ti distance comes from one forward propagation out of the ti
    seeds, the downstream distance from a short forward walk -- no branching
    reverse walk. 0.0 where undefined (not a conveyor, or no ti reference found
    within the chain)."""
    global conv_load, conv_load_buckets
    tiles = _width * _height
    conv_load = [0.0] * tiles
    conv_load_buckets = [0, 0, 0, 0]
    convs = _bm_conveyors
    if not convs:
        return
    ti = _bm_conv_ti & convs
    conv_target = _building_conv_target
    INF = 1 << 30
    cap = _width + _height          # cycle / range guard

    # A[n] = hops to the nearest ti conveyor at-or-upstream of n (0 if n has ti),
    # via forward relaxation from the ti seeds along conv_target.
    A = [INF] * tiles
    frontier = []
    m = ti
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        A[n] = 0
        frontier.append(n)
    steps = 0
    while frontier and steps < cap:
        nxt = []
        for n in frontier:
            t = conv_target[n]
            if 0 <= t < tiles and (convs >> t) & 1 and A[n] + 1 < A[t]:
                A[t] = A[n] + 1
                nxt.append(t)
        frontier = nxt
        steps += 1

    # For each conveyor: B = strict downstream hops to the next ti conveyor.
    m = convs
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        has_ti = (ti >> n) & 1
        a = 0 if has_ti else A[n]        # upstream reference (0 for a ti tile)
        b = INF
        cur = conv_target[n]
        d = 1
        while 0 <= cur < tiles and (convs >> cur) & 1 and d <= cap:
            if (ti >> cur) & 1:
                b = d
                break
            cur = conv_target[cur]
            d += 1
        if has_ti:
            dist = b                     # ti tile: downstream spacing only
        else:
            dist = a + b                 # empty tile: full gap it sits in
        if 0 < dist < INF:
            conv_load[n] = 1.0 / dist
            # Bucket by exact spacing so all four tiers are reachable: dist 1 is the
            # densest (bucket 4), 2 -> 3, 3 -> 2, and any greater spacing (still a
            # real ti reference) falls in bucket 1.
            if dist == 1:
                bucket = 4
            elif dist == 2:
                bucket = 3
            elif dist == 3:
                bucket = 2
            else:
                bucket = 1
            conv_load_buckets[bucket - 1] |= 1 << n


def _recompute_conv_stuck() -> None:
    """Refresh `conv_stuck` -- the conveyors with titanium physically jammed on them.

    Persistent, exactly like `_bm_conv_ti`/`_bm_ti_carrying`: a conveyor's base-stuck
    bit is re-observed while its tile is visible and FROZEN at its last-observed value
    while out of vision, then the jam floods UPSTREAM over EVERY known conveyor -- so
    conv_stuck covers out-of-vision tiles, not just what we can currently see.

    Observation (visible conveyors): read the id of the resource stack sitting on the
    belt. Any change since last observation (a stack advanced off it, arrived, or
    drained) refreshes a `_CONV_STUCK_GRACE`-turn not-stuck grace; otherwise the grace
    ticks down. A belt is base-stuck once it still holds a stack (id != 0) whose grace
    has run out -- the same stack has sat there past the window -- so a belt whose load
    moves at least once every 4 turns never reads as stuck.

    Runs at most once per turn (the grace must tick exactly once even though
    `recompute_derived` can fire twice on a comms-dirty turn)."""
    global conv_stuck, _conv_base_stuck, _conv_stuck_round
    rnd = _rc.get_current_round()
    if rnd == _conv_stuck_round:
        return
    _conv_stuck_round = rnd

    get_res_id = _rc.get_stored_resource_id
    building_id = _building_id
    all_convs = _bm_conveyors
    _conv_base_stuck &= all_convs                    # forget tiles no longer conveyors
    m = all_convs & _bm_visible
    while m:
        lsb = m & -m
        n = lsb.bit_length() - 1
        m ^= lsb
        eid = building_id[n]
        rid = get_res_id(eid) if eid is not None and eid >= 0 else None
        rid = rid or 0                               # empty conveyor -> 0
        old = _conv_res_id.get(n)
        if old is None or rid != old:
            g = _CONV_STUCK_GRACE                    # id changed -> forced not-stuck
        else:
            g = _conv_stuck_grace.get(n, _CONV_STUCK_GRACE) - 1
            if g < 0:
                g = 0
        _conv_res_id[n] = rid
        _conv_stuck_grace[n] = g
        if rid != 0 and g == 0:                      # loaded, grace expired -> stuck
            _conv_base_stuck |= lsb
        else:
            _conv_base_stuck &= ~lsb

    # A jam backs up its feeders: flood upstream over ALL known conveyors (visible or
    # not) so the belief reaches tiles we cannot currently see.
    conv_reverse = _conv_reverse
    stuck = _conv_base_stuck
    frontier = _conv_base_stuck
    while frontier:
        nxt = 0
        f = frontier
        while f:
            lsb = f & -f
            a = lsb.bit_length() - 1
            f ^= lsb
            nxt |= conv_reverse[a]
        nxt &= all_convs & ~stuck
        stuck |= nxt
        frontier = nxt
    conv_stuck = stuck


def _recompute_derived_loaded() -> None:
    global _bm_ti_carrying
    global _recompute_loaded_cache_key

    # conv_stuck tracks physically-jammed belts and must tick its not-stuck grace
    # every turn -- a stack can sit or advance without changing _bm_conv_ti -- so it
    # runs unconditionally, NOT under the (struct, observed-ti) cache below.
    _recompute_conv_stuck()

    key = (_struct_version, _bm_conv_ti)
    if key == _recompute_loaded_cache_key:
        return
    _recompute_loaded_cache_key = key

    _bm_ti_carrying = _compute_carrying()
    _compute_conv_load()


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
    _recompute_derived_loaded()
    _recompute_derived_visible()


def update(recompute: bool = True) -> None:
    global _my_core, _their_core, _core_id, _solved_sym
    global _hor_sym, _ver_sym, _rot_sym
    global _rush_tiebroken, _predicted_enemy_core
    global _bm_any_building, _bm_hp_changed, _building_hp_prev
    global _bm_seen, _bm_visible, _prev_pos, _nearby_tiles, _nearby_tiles_pos, _my_pos
    global _bm_friendly_bots, _bm_enemy_bots, _bm_friendly_stationary
    global _bm_others_5x5, _bm_others_3x3
    global _max_id_seen, _seen_enemy_ids, _seen_uids
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

    # Which visible building tiles had their HP move since last turn (attacked or
    # healed). Compare this turn's readings to last turn's end-of-update snapshot:
    # a tile that was a building last turn (prev >= 0) and reads a different HP now
    # counts. First turn (no snapshot) yields nothing.
    prev = _building_hp_prev
    changed = 0
    if prev:
        for tile in visible_tiles:
            n = tile.x + tile.y * width
            if building_et_idx[n] >= 0 and prev[n] >= 0 and building_hp[n] != prev[n]:
                changed |= 1 << n
    _bm_hp_changed = changed
    _building_hp_prev = building_hp[:]   # snapshot for next turn's comparison

    # Invalidate a symmetry the moment we can SEE the tile where it says the enemy
    # core must sit and there is no enemy core there. Cores never move or appear,
    # so a predicted core footprint that we've seen and that isn't an enemy core
    # (or that falls off the board) is a definitive disproof. This catches what
    # mirrored-terrain checks cannot -- e.g. a horizontally-centred core whose
    # horizontal flip lands on our OWN base, which otherwise "solves" to the enemy
    # core sitting on ours and makes cut wall in our own base.
    if _my_core is not None and not _solved_sym:
        _enemy_core_bm = _bm_et[_IDX_CORE] & _bm_team[1 - my_team_idx]

        def _core_spot_ruled_out(corner) -> bool:
            for dx in (0, 1):
                for dy in (0, 1):
                    tx, ty = corner.x + dx, corner.y + dy
                    if not (0 <= tx < width and 0 <= ty < height):
                        return True                       # would fall off the board
                    b = 1 << (tx + ty * width)
                    if (_bm_seen & b) and not (_enemy_core_bm & b):
                        return True                       # seen, and not an enemy core
            return False

        if _hor_sym and _core_spot_ruled_out(hor_flip_core(_my_core)):
            _hor_sym = False
        if _ver_sym and _core_spot_ruled_out(ver_flip_core(_my_core)):
            _ver_sym = False
        if _rot_sym and _core_spot_ruled_out(rot_flip_core(_my_core)):
            _rot_sym = False

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
        else:
            if _rot_sym:
                _predicted_enemy_core = rot_flip_core(_my_core)
            else:
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
                elif _hor_sym:
                    _predicted_enemy_core = hsym_core
                else:
                    # Every symmetry has been ruled out (asymmetric map). Leave
                    # the enemy core unknown rather than defaulting to the
                    # horizontal flip, which on a centred core is our own base.
                    _predicted_enemy_core = None

    # --- Update builder bot tracking ---
    _bm_friendly_bots = 0
    _bm_enemy_bots = 0
    _bm_friendly_stationary = 0
    prev_pos = dict(_bot_pos)            # each real bot's BELIEVED tile at end of last turn
    seen_uids = set()
    cur_round = rc.get_current_round()
    self_id = rc.get_id()
    for uid in rc.get_nearby_units():
        if uid > _max_id_seen:
            _max_id_seen = uid
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
        history = _bot_pos_history.setdefault(uid, [])
        if not history or history[0] != n:
            history.insert(0, n)
            del history[_BOT_HISTORY_LIMIT:]
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
            # Parked teammate: its BELIEVED tile is unchanged since last turn. Because
            # _bot_pos holds a bot's tile put while it is out of vision, this stays set
            # through a bot leaving vision only because *I* moved -- it did not move, we
            # just can't see it. A bot we actually watch step away (its tile is still
            # visible but it's no longer there) fails the `seen-or-not-visible` guard
            # and correctly drops out.
            if prev_pos.get(uid) == n and (uid in seen_uids or not (bm_visible & bit)):
                _bm_friendly_stationary |= bit
        else:
            _bm_enemy_bots |= bit
    for uid in to_remove:
        n = _bot_pos[uid]
        if _bot_at.get(n) == uid:
            del _bot_at[n]
        del _bot_pos[uid]
        del _bot_team[uid]
        _bot_last_seen.pop(uid, None)
        _bot_pos_history.pop(uid, None)
    # Enemy ids (mod 128) this unit sees for itself THIS turn -- these are the
    # freshest observations, so set_comm_bots trusts them over any (last-turn)
    # relayed sighting of the same enemy when deduping by id.
    _seen_enemy_ids = {uid & 127 for uid in seen_uids if _bot_team.get(uid) != my_team_idx}
    _seen_uids = seen_uids   # who I actually saw this turn (for the claim tiebreak)

    # Precompute other-bots zone masks for cant_claim().
    # expand_chebyshev distributes over OR, so one call per layer suffices.
    my_bit = 1 << (my_pos.x + my_pos.y * width)
    friendly_others = _bm_friendly_bots & ~my_bit
    if friendly_others:
        _bm_others_3x3 = expand_chebyshev(friendly_others)
        _bm_others_5x5 = expand_chebyshev(_bm_others_3x3)
    else:
        _bm_others_3x3 = 0
        _bm_others_5x5 = 0

    while len(_max_id_by_round) <= cur_round:
        _max_id_by_round.append(0)
    _max_id_by_round[cur_round] = _max_id_seen

    if recompute:
        recompute_derived()


def is_tile_empty(pos: Position):
    return in_bounds(pos) and _rc.is_tile_empty(pos)


def has_builder_bot(pos: Position, include_self: bool = False) -> bool:
    if not in_bounds(pos):
        return False
    if include_self and pos == _my_pos:
        return True
    n = pos.x + pos.y * _width
    bit = 1 << n
    return bool((_bm_friendly_bots | _bm_enemy_bots) & bit)

def is_passable(pos: Position):
    """True if a builder bot could stand on `pos`.

    Conveyors and splitters are the only buildings a bot may occupy, and they
    are walkable regardless of who owns them. A friendly barrier or our own core
    is exactly as solid as an enemy's: the controller refused `can_move` onto an
    adjacent friendly barrier on all 345 probes and onto our own core on all 15,
    even though it answered `is_passable`-style queries in the affirmative every
    time. Callers here all mean "can a bot be on this tile" — screen and flank
    tile picking, the adjacency sets for building a harvester, the blocker's
    exit enumeration — so treating our own structures as free produced targets
    that could be selected but never occupied. Keep this in step with
    `_bm_blocked`; the two are the scalar and bitmask forms of one answer.
    """
    if not in_bounds(pos): return False
    n = pos.x + pos.y * _width
    bit = 1 << n
    if _bm_env[_IDX_ENV_WALL] & bit: return False
    if _building_id[n] == 0: return True
    return bool((_bm_et[_IDX_CONVEYOR] | _bm_et[_IDX_SPLITTER]) & bit)

_avoid_cache: dict = {}     # (is_route, enemy_pov) -> (key, mask)


def get_avoid(is_route: bool, enemy_pov: bool = False) -> int:
    """Return a bitmask of tiles to avoid during pathfinding.

    Both modes avoid walls, both cores, and every building except conveyors.
      - is_route=False (builder movement): also avoids tiles adjacent to enemy
        launchers, enemy bots, and PARKED friendly bots (_bm_friendly_stationary --
        a teammate that held its tile from last turn is routed around, not queued
        behind; a moving teammate is still flooded through so we can follow it).
      - is_route=True (conveyor routing): also avoids all conveyors, all
        threatened tiles, the output targets of our own conveyors, and every
        non-landlocked ore.
    enemy_pov models the enemy's own pathing, so it drops the avoidances that are
    ours alone (enemy-turret threat and enemy-launcher/bot/parked-teammate avoidance).

    Cached per (is_route, enemy_pov) on the exact state each variant reads:
    everything is struct-versioned except the routing landlocked/ore terms (which
    track _bm_seen) and our own movement's enemy-bot + parked-teammate avoidance
    (which track _bm_enemy_bots / _bm_friendly_stationary). All are constant within a
    unit's turn, so repeated pathing calls hit the cache."""
    ck = (is_route, enemy_pov)
    if is_route:
        key = (_struct_version, _bm_seen)
    elif not enemy_pov:
        key = (_struct_version, _bm_enemy_bots, _bm_friendly_stationary)
    else:
        key = _struct_version
    hit = _avoid_cache.get(ck)
    if hit is not None and hit[0] == key:
        return hit[1]
    mask = _compute_avoid(is_route, enemy_pov)
    _avoid_cache[ck] = (key, mask)
    return mask


def _compute_avoid(is_route: bool, enemy_pov: bool) -> int:
    mask = _bm_env[_IDX_ENV_WALL]
    mask |= _bm_my_core_area | _bm_their_core_area
    mask |= _bm_any_building & ~_bm_conveyors

    if is_route:
        mask |= _bm_conveyors
        mask |= _conveyor_target_tiles(_bm_conveyors & _bm_team[_my_team_idx])
        ore = _bm_env[_IDX_ENV_ORE_TI]
        w = _width
        landlocking = ore | (~_bm_seen & _board_mask)
        landlocked = (
            landlocking
            & (landlocking >> 1 & _not_right_col)
            & (landlocking << 1 & _not_left_col)
            & (landlocking >> w)
            & (landlocking << w)
        )
        mask |= ore & ~landlocked
        if not enemy_pov:
            mask |= _bm_enemy_turret_threat
        # Enemy barriers are NOT impassable to conveyor routing: they are routable
        # at a high cost (bfs_route weights them, see BARRIER_ROUTE_COST). A route
        # that chooses to run through one attacks it down first (route.run). Cleared
        # last so any avoid term above (buildings, threat) can't re-block them.
        mask &= ~(_bm_et[_IDX_BARRIER] & _bm_team[1 - _my_team_idx])
    elif not enemy_pov:
        mask |= _bm_enemy_launch_adj
        mask |= _bm_enemy_bots
        mask |= _bm_friendly_stationary
    return mask


def passable() -> int:
    """Tiles a builder can move onto: _board_mask & ~get_avoid(False). Replaces
    the old cached _bm_passable_FFF, but is now sourced from get_avoid so it
    stays consistent with the pathfinder (incl. enemy-bot avoidance and buildings
    that only _bm_any_building tracks)."""
    return _board_mask & ~get_avoid(False)
