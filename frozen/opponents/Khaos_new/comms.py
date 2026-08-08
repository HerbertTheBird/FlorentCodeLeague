"""Global-store map sharing.

Markers are gone, so units pool map knowledge through the 16-slot per-team
communication store (`read_store`/`write_store`; each slot is a u32 = 32 bits).

Ownership: one writer per slot, so there is never a clobber.
    slot 0        -> the core
    slots 1..15   -> builder bots, each claiming the lowest free slot

Per-slot payload (14 fixed bits + 2 bits per new tile, <= 32):
    [ position (POS_BITS) | heartbeat (1) | sym-possible (3) | tiles (2 each) ]
  - position   : the writer's tile index (x + y*w) this turn.
  - heartbeat  : flips every turn a live unit writes. Frozen across turns => the
                 writer is dead/idle, so the slot may be reclaimed.
  - sym-possible: bit0 hor / bit1 ver / bit2 rot still possible (map_info's
                 convention). Always >= 1 for a live unit, which is what makes a
                 zero slot value reliably mean "free".
  - tiles      : env of the tiles newly seen this turn (2 bits: 0 empty /
                 1 wall / 2 ore), sorted by tile index. The tile *positions* are
                 not sent: a reader recomputes them from vision(pos_now) minus
                 vision(pos_prev). A cardinal step reveals <= 9 tiles = the tile
                 budget on maps up to 32x32, so a normal move never overflows.

Launch / first sight / owner change all show up as the position jumping by
Chebyshev > 1 (or there being no prior position). Both writer and reader then
send/expect 0 tiles and just re-baseline -- no explicit flag needed. Symmetry
propagates peer-to-peer through every slot's 3 sym-possible bits (each reader
ORs the eliminations via map_info.update_symmetry_from_comms), so no separate
core "resolved symmetry" broadcast is required.

Call `update()` once per turn, after `map_info.update()`.
"""

from main import has_op
from fcode import Controller, Position, EntityType, GameConstants

import map_info

_STORE_SIZE = GameConstants.STORE_SIZE      # 16
_CORE_SLOT = 0
_FIRST_BUILDER_SLOT = 1
_DEAD_AFTER = 3                             # unchanged heartbeats before a slot is reclaimable

# Vision disk offsets (builder radius^2), precomputed once. A reader reconstructs
# any unit's vision analytically from its position, so this must be deterministic
# and identical everywhere.
_VISION_R2 = GameConstants.BUILDER_BOT_VISION_RADIUS_SQ  # 20
_RAD = int(_VISION_R2 ** 0.5)
_VISION_OFFSETS = [
    (dx, dy)
    for dy in range(-_RAD, _RAD + 1)
    for dx in range(-_RAD, _RAD + 1)
    if dx * dx + dy * dy <= _VISION_R2
]

# --- bit layout (filled in by init once the map size is known) ---
_POS_BITS = 0
_POS_MASK = 0
_HB_SHIFT = 0
_SYM_SHIFT = 0
_TILE_SHIFT = 0
_MAX_TILES = 0

# --- per-unit state (each bot runs its own module instance) ---
rc: Controller = None
_width = 0
_height = 0
_am_core = False

_my_slot = None          # slot this unit owns (None = unclaimed)
_my_prev_pos = None      # my last broadcast position (delta baseline)
_my_hb = 0
_last_written = 0        # value I last wrote (for claim verification)

# --- per-slot reader tracking (indexed by slot) ---
_slot_prev_pos: list = []   # Position | None
_slot_last_hb: list = []    # int | None
_slot_dead: list = []       # consecutive unchanged-heartbeat count


def init(c: Controller):
    global rc, _width, _height, _am_core
    global _POS_BITS, _POS_MASK, _HB_SHIFT, _SYM_SHIFT, _TILE_SHIFT, _MAX_TILES
    global _my_slot, _my_prev_pos, _my_hb, _last_written
    global _slot_prev_pos, _slot_last_hb, _slot_dead
    rc = c
    _width = map_info._width
    _height = map_info._height
    _am_core = (c.get_entity_type() == EntityType.CORE)

    _POS_BITS = max(1, (_width * _height - 1).bit_length())
    _POS_MASK = (1 << _POS_BITS) - 1
    _HB_SHIFT = _POS_BITS
    _SYM_SHIFT = _POS_BITS + 1
    _TILE_SHIFT = _POS_BITS + 4
    _MAX_TILES = (32 - _TILE_SHIFT) // 2

    _my_slot = _CORE_SLOT if _am_core else None
    _my_prev_pos = None
    _my_hb = 0
    _last_written = 0

    _slot_prev_pos = [None] * _STORE_SIZE
    _slot_last_hb = [None] * _STORE_SIZE
    _slot_dead = [0] * _STORE_SIZE


# --------------------------------------------------------------------------- #
# Vision / delta helpers
# --------------------------------------------------------------------------- #
def _vision_set(pos: Position) -> set:
    """Tile indices within vision radius of `pos` (in-bounds)."""
    px, py = pos.x, pos.y
    w, h = _width, _height
    s = set()
    for dx, dy in _VISION_OFFSETS:
        x = px + dx
        y = py + dy
        if 0 <= x < w and 0 <= y < h:
            s.add(x + y * w)
    return s


def _delta_tiles(now: Position, prev):
    """Sorted tile indices newly visible at `now` vs `prev`, or None to signal a
    re-baseline (no prior position, a launch/owner-change jump, or a reveal too
    big to fit). None => 0 tiles this turn; [] => moved but nothing new."""
    if prev is None:
        return None
    if max(abs(now.x - prev.x), abs(now.y - prev.y)) > 1:
        return None
    d = sorted(_vision_set(now) - _vision_set(prev))
    if len(d) > _MAX_TILES:
        return None
    return d


def _env_code(n: int) -> int:
    """0 empty / 1 wall / 2 ore for tile index `n`, from map_info's env masks."""
    bit = 1 << n
    if map_info._bm_env[map_info._IDX_ENV_WALL] & bit:
        return 1
    if map_info._bm_env[map_info._IDX_ENV_ORE_TI] & bit:
        return 2
    return 0


def _my_sym_bits() -> int:
    return (int(map_info._hor_sym)
            | (int(map_info._ver_sym) << 1)
            | (int(map_info._rot_sym) << 2))


# --------------------------------------------------------------------------- #
# Encode / broadcast
# --------------------------------------------------------------------------- #
def _encode(pos: Position, hb: int, sym_bits: int, tile_codes) -> int:
    val = (pos.x + pos.y * _width) & _POS_MASK
    val |= (hb & 1) << _HB_SHIFT
    val |= (sym_bits & 0x7) << _SYM_SHIFT
    shift = _TILE_SHIFT
    for code in tile_codes:
        val |= (code & 0x3) << shift
        shift += 2
    return val


def _broadcast():
    global _my_prev_pos, _my_hb, _last_written
    my_pos = map_info._my_pos
    d = _delta_tiles(my_pos, _my_prev_pos)
    codes = [] if d is None else [_env_code(n) for n in d]
    val = _encode(my_pos, _my_hb, _my_sym_bits(), codes)
    rc.write_store(_my_slot, val)
    _last_written = val
    _my_prev_pos = my_pos
    _my_hb ^= 1


# --------------------------------------------------------------------------- #
# Absorb (read every slot, learn symmetry + tiles)
# --------------------------------------------------------------------------- #
def _inject(n: int, code: int) -> bool:
    """Record env `code` at tile `n` (and its symmetric mirror). Returns True if
    anything new was written into map_info."""
    if code > 2:
        return False
    bit = 1 << n
    if map_info._bm_seen & bit:
        return False
    map_info._bm_env[code] |= bit
    map_info._bm_seen |= bit
    fp = map_info.flip(Position(n % _width, n // _width))
    if fp is not None:
        fb = 1 << (fp.x + fp.y * _width)
        if not (map_info._bm_seen & fb):
            map_info._bm_env[code] |= fb
            map_info._bm_seen |= fb
    return True


def _absorb():
    dirty = False
    for s in range(_STORE_SIZE):
        if s == _my_slot:
            continue
        val = rc.read_store(s)
        if val == 0:                     # never written -> free
            _slot_prev_pos[s] = None
            _slot_last_hb[s] = None
            _slot_dead[s] = 0
            continue

        hb = (val >> _HB_SHIFT) & 1
        last = _slot_last_hb[s]
        if last is not None and hb == last:
            _slot_dead[s] += 1           # no fresh write this turn
            continue
        _slot_dead[s] = 0
        _slot_last_hb[s] = hb

        n = val & _POS_MASK
        pos = Position(n % _width, n // _width)

        sym_bits = (val >> _SYM_SHIFT) & 0x7
        map_info.update_symmetry_from_comms(sym_bits)

        d = _delta_tiles(pos, _slot_prev_pos[s])
        if d is not None:
            shift = _TILE_SHIFT
            for tn in d:
                code = (val >> shift) & 0x3
                shift += 2
                if _inject(tn, code):
                    dirty = True
        _slot_prev_pos[s] = pos

    if dirty:
        map_info.recompute_derived()


# --------------------------------------------------------------------------- #
# Slot claiming (builders only)
# --------------------------------------------------------------------------- #
def _ensure_slot():
    global _my_slot, _my_prev_pos
    if _my_slot is not None:
        # My previous write is visible this turn iff nobody clobbered the slot.
        if _last_written != 0 and rc.read_store(_my_slot) == _last_written:
            return
        _my_slot = None
        _my_prev_pos = None
    for s in range(_FIRST_BUILDER_SLOT, _STORE_SIZE):
        # Free (never written) or held by a unit that has gone silent.
        if rc.read_store(s) == 0 or _slot_dead[s] >= _DEAD_AFTER:
            _my_slot = s
            _my_prev_pos = None
            return


# --------------------------------------------------------------------------- #
# Public entry point -- call once per turn after map_info.update()
# --------------------------------------------------------------------------- #
def update():
    _absorb()
    if _am_core:
        _broadcast()
    elif rc.get_entity_type() == EntityType.BUILDER_BOT:
        _ensure_slot()
        if _my_slot is not None:
            _broadcast()
