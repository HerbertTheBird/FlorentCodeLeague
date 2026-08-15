"""Global-store map sharing.

Markers are gone, so units pool map knowledge through the 16-slot per-team
communication store (`read_store`/`write_store`; each slot is a u32 = 32 bits).

Ownership: one writer per slot, so there is never a clobber.
    slot 0        -> the core
    slots 1..14   -> builder bots, each claiming the lowest free slot
    slot 15       -> the sentry launcher's defence alarm (see `_DEFENSE_SLOT`)

Per-slot payload (<= 32 bits). Two layouts, told apart by slot index:

  Core (slot 0):
    [ position (POS_BITS) | heartbeat (1) | valid=1 (1) | sym-possible (3)
      | route-tally (through bit 27) | repair-target (bits 28..31) ]
  - sym-possible : bit0 hor / bit1 ver / bit2 rot still possible (map_info's
                 convention). ONLY the core broadcasts symmetry now. It
                 re-derives it from the tiles every unit relays (see
                 `_inject` -> `map_info.note_symmetry_conflict`), so builders no
                 longer spend 3 bits on it. Readers apply symmetry from slot 0.
  - route-tally  : cumulative count of "route fully connected" reports the core
                 has tallied from builders over the whole game.
  - repair-target: desired near-core repair crew, capped at eight. Reserving the
                 high nibble still leaves at least 13 bits for route-tally on
                 the largest supported map.

  Builder (slots 1..15):
    [ position (POS_BITS) | heartbeat (1) | valid=1 (1) | path-done (1) | tiles ]
  - path-done  : 1 on the turn this builder connects its routed source to the
                 network (route.py builds the final segment), else 0. The core
                 sums these into route-tally.

  Common to both:
  - position   : the writer's tile index (x + y*w) this turn.
  - heartbeat  : flips every turn a live unit writes. Frozen across turns => the
                 writer is dead/idle, so the slot may be reclaimed.
  - valid      : always 1 for a live writer, which is what makes a zero slot
                 value reliably mean "free".
  Sentry launcher (slot 15):
    [ launcher pos (POS_BITS) | enemy pos (POS_BITS) | heartbeat (1)
      | alarm (1) | live=1 (1) ]
  - launcher pos : where the sentry stands, so the core knows which spawn-ring
                 tile to drop the defender on (it must land inside the sentry's
                 pickup radius).
  - enemy pos    : the unblocked enemy bot that tripped the alarm, 0 when
                 `alarm` is clear.
  - heartbeat    : flips on every write, exactly as for the other layouts. The
                 store never clears itself, so a dead sentry would otherwise
                 leave a stale alarm latched forever; readers age the slot out
                 after `_DEAD_AFTER` unchanged reads.
  - live         : always 1, so "slot value 0" still means "no sentry yet".

  - tiles      : env of the tiles newly seen this turn, sorted by tile index,
                 each as a prefix code (0 = empty, 10 = wall, 11 = ore) so the
                 common empty tile costs one bit. The tile *positions* are not
                 sent: a reader recomputes them from vision(pos_now) minus
                 vision(pos_prev) and decodes exactly that many codes -- the
                 known count self-terminates the stream, no length field. Worst
                 case is still 2 bits/tile, so the count budget is unchanged: a
                 cardinal step reveals <= 9 tiles = the budget on 32x32 maps, so
                 a normal move never overflows.

Launch / first sight / owner change all show up as the position jumping by
Chebyshev > 1 (or there being no prior position). Both writer and reader then
send/expect 0 tiles and just re-baseline -- no explicit flag needed. Symmetry no
longer propagates peer-to-peer: builders relay only tiles, the core turns those
into symmetry eliminations locally and broadcasts the result in slot 0.

Call `read()`/`write()` once per turn, after `map_info.update()`.
"""

from fcode import Controller, Position, EntityType, GameConstants

import map_info

_STORE_SIZE = GameConstants.STORE_SIZE      # 16
_CORE_SLOT = 0
_FIRST_BUILDER_SLOT = 1
_DEFENSE_SLOT = _STORE_SIZE - 1             # sentry launcher's alarm; not a builder slot
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
_VALID_SHIFT = 0
_PATHDONE_SHIFT = 0      # builder layout
_TILE_SHIFT = 0          # builder layout (tile stream start)
_MAX_TILES = 0
_SYM_SHIFT = 0           # core layout
_TALLY_SHIFT = 0         # core layout
_TALLY_MASK = 0
_REPAIR_SHIFT = 28       # core layout; high nibble is desired near-core crew
_REPAIR_MASK = 0xF
_AL_ENEMY_SHIFT = 0      # sentry layout (enemy pos)
_AL_HB_SHIFT = 0         # sentry layout
_AL_ALARM_SHIFT = 0      # sentry layout
_AL_LIVE_SHIFT = 0       # sentry layout

# Prefix (variable-length) code for a tile's env, packed LSB-first into the tile
# region:  0 -> empty (1 bit),  10 -> wall,  11 -> ore (2 bits each). Empty is the
# common case, so it costs a single bit. A reader knows exactly how many tiles to
# expect (from the geometric delta), so the stream self-terminates with no length
# field. Worst case is 2 bits/tile, so _MAX_TILES (the count budget) is unchanged.
_TILE_PREFIX = {0: (0,), 1: (1, 0), 2: (1, 1)}

# --- per-unit state (each bot runs its own module instance) ---
rc: Controller = None
_width = 0
_height = 0
_am_core = False

_my_slot = None          # slot this unit owns (None = unclaimed)
_my_prev_pos = None      # my last broadcast position (delta baseline)
_my_hb = 0
_last_written = 0        # value I last wrote (for claim verification)

_pending_path_done = False   # builder: set by route.py the turn a route completes
_route_total = 0             # core: cumulative connected-route reports I've tallied
_core_route_total = 0        # non-core: latest route-tally read from the core slot
_repair_target_to_send = 1   # core: desired builders in slots 2..target+1
_core_repair_target = 1      # non-core: latest repair target read from slot 0

_my_alarm_hb = 0             # sentry launcher: heartbeat for slot 15
_alarm_last_raw = None       # reader: last raw slot-15 value, for staleness ageing
_alarm_dead = 0              # reader: consecutive unchanged reads of slot 15

# --- per-slot reader tracking (indexed by slot) ---
_slot_prev_pos: list = []   # Position | None
_slot_last_hb: list = []    # int | None
_slot_dead: list = []       # consecutive unchanged-heartbeat count


def init(c: Controller):
    global rc, _width, _height, _am_core
    global _POS_BITS, _POS_MASK, _HB_SHIFT, _VALID_SHIFT, _PATHDONE_SHIFT
    global _TILE_SHIFT, _MAX_TILES, _SYM_SHIFT, _TALLY_SHIFT, _TALLY_MASK
    global _my_slot, _my_prev_pos, _my_hb, _last_written
    global _pending_path_done, _route_total, _core_route_total
    global _repair_target_to_send, _core_repair_target
    global _slot_prev_pos, _slot_last_hb, _slot_dead
    global _AL_ENEMY_SHIFT, _AL_HB_SHIFT, _AL_ALARM_SHIFT, _AL_LIVE_SHIFT
    global _my_alarm_hb, _alarm_last_raw, _alarm_dead
    rc = c
    _width = map_info._width
    _height = map_info._height
    _am_core = (c.get_entity_type() == EntityType.CORE)

    _POS_BITS = max(1, (_width * _height - 1).bit_length())
    _POS_MASK = (1 << _POS_BITS) - 1
    _HB_SHIFT = _POS_BITS
    _VALID_SHIFT = _POS_BITS + 1
    # builder layout
    _PATHDONE_SHIFT = _POS_BITS + 2
    _TILE_SHIFT = _POS_BITS + 3
    _MAX_TILES = (32 - _TILE_SHIFT) // 2        # worst case 2 bits/tile
    # core layout (the high nibble is reserved for the repair-crew target)
    _SYM_SHIFT = _POS_BITS + 2
    _TALLY_SHIFT = _POS_BITS + 5
    _TALLY_MASK = (1 << (_REPAIR_SHIFT - _TALLY_SHIFT)) - 1
    # sentry layout (two positions + 3 flags; <= 23 bits on the biggest 30x30 map)
    _AL_ENEMY_SHIFT = _POS_BITS
    _AL_HB_SHIFT = _POS_BITS * 2
    _AL_ALARM_SHIFT = _POS_BITS * 2 + 1
    _AL_LIVE_SHIFT = _POS_BITS * 2 + 2

    _my_slot = _CORE_SLOT if _am_core else None
    _my_prev_pos = None
    _my_hb = 0
    _last_written = 0

    _pending_path_done = False
    _route_total = 0
    _core_route_total = 0
    _repair_target_to_send = 1
    _core_repair_target = 1

    _slot_prev_pos = [None] * _STORE_SIZE
    _slot_last_hb = [None] * _STORE_SIZE
    _slot_dead = [0] * _STORE_SIZE

    _my_alarm_hb = 0
    _alarm_last_raw = None
    _alarm_dead = 0


# --------------------------------------------------------------------------- #
# Public helpers used by other modules
# --------------------------------------------------------------------------- #
def note_route_complete():
    """Called by route.py the turn a builder connects its routed source to the
    network. Latched until the next broadcast, which sends it as the path-done
    bit and then clears it."""
    global _pending_path_done
    _pending_path_done = True


def write_alarm(launcher: Position, enemy: Position | None) -> None:
    """Sentry launcher: publish this round's defence alarm into slot 15.

    Called every round the sentry is alive, alarm or not — the flipping
    heartbeat is what lets readers tell "no threat right now" from "the sentry
    is dead and this value is stale"."""
    global _my_alarm_hb
    val = (launcher.x + launcher.y * _width) & _POS_MASK
    if enemy is not None:
        val |= ((enemy.x + enemy.y * _width) & _POS_MASK) << _AL_ENEMY_SHIFT
        val |= 1 << _AL_ALARM_SHIFT
    val |= (_my_alarm_hb & 1) << _AL_HB_SHIFT
    val |= 1 << _AL_LIVE_SHIFT
    rc.write_store(_DEFENSE_SLOT, val)
    _my_alarm_hb ^= 1


def read_alarm() -> tuple[Position, Position | None] | None:
    """Latest (sentry position, alarmed enemy position) from slot 15.

    Returns None when there is no live sentry — either it never wrote, or its
    heartbeat has been frozen for `_DEAD_AFTER` rounds, which means it died and
    the latched value must not be trusted. The enemy element is None when the
    sentry is alive but sees nothing worth answering.

    Call once per turn (it ages the staleness counter)."""
    global _alarm_last_raw, _alarm_dead
    val = rc.read_store(_DEFENSE_SLOT)
    if val == _alarm_last_raw:
        _alarm_dead += 1
    else:
        _alarm_dead = 0
        _alarm_last_raw = val
    if val == 0 or not ((val >> _AL_LIVE_SHIFT) & 1):
        return None
    if _alarm_dead >= _DEAD_AFTER:
        return None
    n = val & _POS_MASK
    launcher = Position(n % _width, n // _width)
    if not ((val >> _AL_ALARM_SHIFT) & 1):
        return launcher, None
    en = (val >> _AL_ENEMY_SHIFT) & _POS_MASK
    return launcher, Position(en % _width, en // _width)


def route_total() -> int:
    """Cumulative "route fully connected" count the core exposes in its comms.
    On the core this is its own running tally; elsewhere it's the last value
    read from the core slot."""
    return _route_total if _am_core else _core_route_total


def set_repair_target(count: int) -> None:
    """Core: publish how many non-rusher builders should remain near home."""
    global _repair_target_to_send
    _repair_target_to_send = max(1, min(8, int(count)))


def repair_target() -> int:
    """Desired near-core repair/economy crew from the core's latest broadcast."""
    return _repair_target_to_send if _am_core else _core_repair_target


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
def _encode_core(pos: Position, hb: int, sym_bits: int, tally: int) -> int:
    val = (pos.x + pos.y * _width) & _POS_MASK
    val |= (hb & 1) << _HB_SHIFT
    val |= 1 << _VALID_SHIFT
    val |= (sym_bits & 0x7) << _SYM_SHIFT
    val |= (tally & _TALLY_MASK) << _TALLY_SHIFT
    val |= (_repair_target_to_send & _REPAIR_MASK) << _REPAIR_SHIFT
    return val


def _encode_builder(pos: Position, hb: int, path_done: int, tile_codes) -> int:
    val = (pos.x + pos.y * _width) & _POS_MASK
    val |= (hb & 1) << _HB_SHIFT
    val |= 1 << _VALID_SHIFT
    val |= (path_done & 1) << _PATHDONE_SHIFT
    shift = _TILE_SHIFT
    for code in tile_codes:
        for b in _TILE_PREFIX[code]:
            val |= b << shift
            shift += 1
    return val


def _broadcast():
    global _my_prev_pos, _my_hb, _last_written, _pending_path_done
    my_pos = map_info._my_pos
    if _am_core:
        val = _encode_core(my_pos, _my_hb, _my_sym_bits(), _route_total)
    else:
        d = _delta_tiles(my_pos, _my_prev_pos)
        codes = [] if d is None else [_env_code(n) for n in d]
        val = _encode_builder(my_pos, _my_hb, 1 if _pending_path_done else 0, codes)
    rc.write_store(_my_slot, val)
    _last_written = val
    _my_prev_pos = my_pos
    _my_hb ^= 1
    _pending_path_done = False


# --------------------------------------------------------------------------- #
# Absorb (read every slot, learn symmetry + tiles + route tally)
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
    # Relayed tiles never pass through update_at, so derive symmetry here -- this
    # is how the core (and anyone else) infers symmetry from pooled vision.
    map_info.note_symmetry_conflict(n, code)
    fp = map_info.flip(Position(n % _width, n // _width))
    if fp is not None:
        fb = 1 << (fp.x + fp.y * _width)
        if not (map_info._bm_seen & fb):
            map_info._bm_env[code] |= fb
            map_info._bm_seen |= fb
    return True


def _absorb():
    global _route_total, _core_route_total, _core_repair_target
    dirty = False
    for s in range(_STORE_SIZE):
        if s == _my_slot or s == _DEFENSE_SLOT:
            continue                     # slot 15 uses the sentry layout, not this one
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

        if s == _CORE_SLOT:
            sym_bits = (val >> _SYM_SHIFT) & 0x7
            map_info.update_symmetry_from_comms(sym_bits)
            _core_route_total = (val >> _TALLY_SHIFT) & _TALLY_MASK
            _core_repair_target = max(1, (val >> _REPAIR_SHIFT) & _REPAIR_MASK)
            _slot_prev_pos[s] = pos      # core sends no tiles
            continue

        # Builder slot: tally connected-route reports (core only), then tiles.
        if _am_core and ((val >> _PATHDONE_SHIFT) & 1):
            _route_total = (_route_total + 1) & _TALLY_MASK

        d = _delta_tiles(pos, _slot_prev_pos[s])
        if d is not None:
            shift = _TILE_SHIFT
            for tn in d:
                if (val >> shift) & 1:           # 1x -> wall (10) / ore (11)
                    shift += 1
                    code = 1 + ((val >> shift) & 1)
                    shift += 1
                else:                            # 0  -> empty
                    shift += 1
                    code = 0
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
    for s in range(_FIRST_BUILDER_SLOT, _DEFENSE_SLOT):
        # Free (never written) or held by a unit that has gone silent.
        if rc.read_store(s) == 0 or _slot_dead[s] >= _DEAD_AFTER:
            _my_slot = s
            _my_prev_pos = None
            return


# --------------------------------------------------------------------------- #
# Public entry point -- call once per turn after map_info.update()
# --------------------------------------------------------------------------- #
def read():
    _absorb()


def write():
    if _am_core:
        _broadcast()
    elif rc.get_entity_type() == EntityType.BUILDER_BOT:
        _ensure_slot()
        if _my_slot is not None:
            _broadcast()
