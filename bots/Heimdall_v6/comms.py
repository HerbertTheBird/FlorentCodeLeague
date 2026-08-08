"""Global-store map sharing.

Units pool map knowledge through the 16-slot per-team communication store 
(`read_store`/`write_store`; each slot is a u32 = 32 bits).

Ownership: one writer per slot at a time.
    slot 0        -> the core
    slots 1..14   -> builder bots, each claiming a free or timed-out slot
    slot 15       -> the sentry launcher's defence alarm (see `_DEFENSE_SLOT`)

Claims are optimistic -- a writer checks next turn that the slot still holds
what it wrote, and drops it if not -- so two units CAN briefly collide. The scan
order is seeded from the unit's entity id and stepped on every detected clobber.
A plain lowest-free scan does eventually settle, because one winner peels off
per round, but every unit sees the same buffered snapshot and scans it in the
same order, so K simultaneous claimants take K rounds to sort themselves out and
relay nothing until they do. Seeding the order makes that one round.

Per-slot payload (<= 32 bits). Two layouts, told apart by slot index:

  Core (slot 0):
    [ position (POS_BITS) | heartbeat (1) | valid=1 (1) | sym-possible (3)
      | route-tally (remaining bits) ]
  - sym-possible : bit0 hor / bit1 ver / bit2 rot still possible (map_info's
                 convention). ONLY the core broadcasts symmetry now. It
                 re-derives it from the tiles every unit relays (see
                 `map_info.record_relayed_tile`), so builders no longer spend
                 3 bits on it. Readers apply symmetry from slot 0.
  - route-tally  : cumulative count of "route fully connected" reports the core
                 has tallied from builders over the whole game. The core is
                 stationary, so it never sends tiles -- all remaining bits are
                 free for this counter.

  Builder (slots 1..14):
    [ position (POS_BITS) | heartbeat (1) | valid=1 (1) | path-done (1)
      | rebase (1) | tiles ]
  - path-done  : 1 on the turn this builder connects its routed source to the
                 network (route.py builds the final segment), else 0. The core
                 sums these into route-tally.
                 KNOWN LIMIT (unfixed, deliberately): this is a one-turn pulse
                 and is never re-sent, so a round the core loses to the CPU cap
                 drops the report for good, and attack.py gates the siege on the
                 tally. One bit cannot carry an exact count to an observer that
                 misses rounds: a held level merges completions that are close
                 together, a parity toggle cancels two completions inside one
                 blackout, and both let a slot hand-off manufacture a count that
                 never happened. Over-counting opens the siege on routes that do
                 not exist, which is worse than the current under-count, and
                 attack.py already has two other escapes (SIEGE_MIN_HARVESTERS,
                 SIEGE_OPEN_ROUND). Fixing this properly needs a wider field,
                 which the tile budget has no room for -- see git history for
                 three measured attempts that were each worse than this.
  - rebase     : 1 when this writer deliberately sent NO tiles because it had no
                 usable previous position -- a fresh slot claim, a launch, or a
                 reveal too big to fit. See "Re-baselining" below. It also tells
                 the core to re-baseline route-tog, since the word may be the
                 first from a new owner whose toggle is unrelated to the last.

  Common to both:
  - position   : the writer's tile index (x + y*w) this turn.
  - heartbeat  : flips on every write, which is what guarantees a live writer's
                 word always CHANGES. Readers use "raw slot value unchanged
                 since last read" as the liveness test, so a frozen word means
                 the writer is dead/idle and the slot may be reclaimed.
  - valid      : always 1 for a live writer. Never read directly; it exists to
                 keep a live word non-zero, which is what makes a zero slot
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
                 after `_DEAD_AFTER` rounds without a change.
  - live         : always 1, so "slot value 0" still means "no sentry yet".

  - tiles      : env of the tiles newly seen this turn, sorted by tile index,
                 each as a prefix code (0 = empty, 10 = wall, 11 = ore) so the
                 common empty tile costs one bit. The tile *positions* are not
                 sent: a reader recomputes them from vision(pos_now) minus
                 vision(pos_prev) and decodes exactly that many codes -- the
                 known count self-terminates the stream, no length field. Worst
                 case is 2 bits/tile; a cardinal step reveals at most 9 tiles,
                 which is exactly `_MAX_TILES` on the biggest (30x30) map, so a
                 normal move never overflows.

Re-baselining. The tile stream carries no length, so writer and reader MUST
agree exactly on the previous position the delta was measured from. Three rules
keep them in lockstep, and all three are needed:
  1. `rebase` is explicit. A reader never guesses that a writer sent nothing --
     it is told. Inferring it from "the position jumped by Chebyshev > 1" is not
     safe, because after a slot changes hands the reader's baseline still points
     at the PREVIOUS owner, and two builders standing within one tile of each
     other (routine near the core) would let the reader decode the all-zero tile
     region as a run of real "empty" tiles and record them permanently.
  2. Every write changes the raw word, so a reader can never miss one. The
     heartbeat covers the same writer; a claimant additionally seeds its
     heartbeat to differ from whatever is latched in the slot it takes over
     (`_ensure_slot`), so the first write after a hand-off is detectable too.
  3. A reader that missed a round (CPU cutoff) cannot know what it skipped, so
     it drops every baseline and waits to be re-based. Dropping a baseline only
     ever costs one turn of tiles; keeping a wrong one corrupts the map forever,
     since `map_info` marks relayed tiles permanently seen.

Call `read()`/`write()` once per turn, after `map_info.update()`.
"""

from fcode import Controller, Position, EntityType, GameConstants

import map_info

_STORE_SIZE = GameConstants.STORE_SIZE      # 16
_CORE_SLOT = 0
_FIRST_BUILDER_SLOT = 1
_DEFENSE_SLOT = _STORE_SIZE - 1             # sentry launcher's alarm; not a builder slot
_NUM_BUILDER_SLOTS = _DEFENSE_SLOT - _FIRST_BUILDER_SLOT   # 14
_DEAD_AFTER = 3                             # rounds without a change before a slot is reclaimable

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

# Newly-revealed offsets per single step, keyed by (dx, dy) of the step. A tile
# at offset `o` from the new position sat at offset `o + step` from the old one,
# so it is new iff `o + step` was outside the disk. This replaces building two
# ~69-element sets and diffing them on every slot every turn (~14x that per
# absorb) with one 9-element list walk -- roughly a 10x saving on the hottest
# path in this module. Sorted by (dy, dx) so the resulting tile indices come out
# ascending for free: index = (px+dx) + (py+dy)*w.
_VIS_SET = set(_VISION_OFFSETS)
_STEP_DELTA = {
    (sx, sy): sorted(
        (o for o in _VIS_SET if (o[0] + sx, o[1] + sy) not in _VIS_SET),
        key=lambda o: (o[1], o[0]),
    )
    for sx in (-1, 0, 1)
    for sy in (-1, 0, 1)
}

# Prefix (variable-length) code for a tile's env, packed LSB-first into the tile
# region:  0 -> empty (1 bit),  10 -> wall,  11 -> ore (2 bits each). Empty is the
# common case, so it costs a single bit. A reader knows exactly how many tiles to
# expect (from the geometric delta), so the stream self-terminates with no length
# field. Worst case is 2 bits/tile, which is what _MAX_TILES budgets for.
_WIRE_EMPTY = 0
_WIRE_WALL = 1
_WIRE_ORE = 2
_TILE_PREFIX = {_WIRE_EMPTY: (0,), _WIRE_WALL: (1, 0), _WIRE_ORE: (1, 1)}

# --- bit layout (filled in by init once the map size is known) ---
_POS_BITS = 0
_POS_MASK = 0
_HB_SHIFT = 0
_VALID_SHIFT = 0
_PATHDONE_SHIFT = 0      # builder layout
_REBASE_SHIFT = 0        # builder layout
_TILE_SHIFT = 0          # builder layout (tile stream start)
_MAX_TILES = 0
_SYM_SHIFT = 0           # core layout
_TALLY_SHIFT = 0         # core layout
_TALLY_MASK = 0
_AL_ENEMY_SHIFT = 0      # sentry layout (enemy pos)
_AL_HB_SHIFT = 0         # sentry layout
_AL_ALARM_SHIFT = 0      # sentry layout
_AL_LIVE_SHIFT = 0       # sentry layout

# Wire env code -> map_info's `_bm_env` index. The two enumerations are chosen
# independently (the wire order is picked so "empty" gets the 1-bit prefix), so
# translate rather than assuming they coincide.
_WIRE_TO_ENV = ()

# --- per-unit state (each bot runs its own module instance) ---
rc: Controller = None
_width = 0
_height = 0
_am_core = False
_am_builder = False

_my_slot = None          # slot this unit owns (None = unclaimed)
_my_prev_pos = None      # my last broadcast position (delta baseline)
_my_hb = 0
_last_written = 0        # value I last wrote (for claim verification)
_probe_off = 0           # where my slot scan starts (id-seeded, stepped on clobber)
_probe_step = 1

_pending_path_done = False   # builder: set by route.py the turn a route completes
_route_total = 0             # core: cumulative connected-route reports I've tallied
_core_route_total = 0        # non-core: latest route-tally read from the core slot

_my_alarm_hb = 0             # sentry launcher: heartbeat for slot 15
_alarm_last_write = 0        # sentry launcher: value I last wrote into slot 15
_alarm_last_raw = None       # last raw slot-15 value seen
_alarm_last_change = None    # round that value last changed (None = never seen one)
_alarm_round = -1            # round the above were last aged (keeps ageing idempotent)

# --- per-slot reader tracking (indexed by slot) ---
_slot_prev_pos: list = []   # Position | None -- baseline the next delta is measured from
_slot_last_raw: list = []   # int | None -- last raw value seen (liveness test)
_slot_dead: list = []       # consecutive reads with an unchanged raw value
_slot_raw: list = []        # raw value read this turn (so _ensure_slot need not re-read)
_raw_round = -1             # round `_slot_raw` was filled
_read_round = -2            # round `_absorb` last ran


def init(c: Controller):
    global rc, _width, _height, _am_core, _am_builder
    global _POS_BITS, _POS_MASK, _HB_SHIFT, _VALID_SHIFT, _PATHDONE_SHIFT
    global _REBASE_SHIFT, _TILE_SHIFT, _MAX_TILES, _SYM_SHIFT, _TALLY_SHIFT
    global _TALLY_MASK, _WIRE_TO_ENV
    global _my_slot, _my_prev_pos, _my_hb, _last_written, _probe_off, _probe_step
    global _pending_path_done, _route_total, _core_route_total
    global _slot_prev_pos, _slot_last_raw, _slot_dead, _slot_raw
    global _raw_round, _read_round
    global _AL_ENEMY_SHIFT, _AL_HB_SHIFT, _AL_ALARM_SHIFT, _AL_LIVE_SHIFT
    global _my_alarm_hb, _alarm_last_write
    global _alarm_last_raw, _alarm_last_change, _alarm_round
    rc = c
    _width = map_info._width
    _height = map_info._height
    _am_core = (c.get_entity_type() == EntityType.CORE)
    _am_builder = (c.get_entity_type() == EntityType.BUILDER_BOT)

    _POS_BITS = max(1, (_width * _height - 1).bit_length())
    _POS_MASK = (1 << _POS_BITS) - 1
    _HB_SHIFT = _POS_BITS
    _VALID_SHIFT = _POS_BITS + 1
    # builder layout
    _PATHDONE_SHIFT = _POS_BITS + 2
    _REBASE_SHIFT = _POS_BITS + 3
    _TILE_SHIFT = _POS_BITS + 4
    _MAX_TILES = (32 - _TILE_SHIFT) // 2        # worst case 2 bits/tile
    # core layout (core never sends tiles, so the tally owns the rest of the word)
    _SYM_SHIFT = _POS_BITS + 2
    _TALLY_SHIFT = _POS_BITS + 5
    _TALLY_MASK = (1 << (32 - _TALLY_SHIFT)) - 1
    # sentry layout (two positions + 3 flags; 23 bits on the biggest 30x30 map)
    _AL_ENEMY_SHIFT = _POS_BITS
    _AL_HB_SHIFT = _POS_BITS * 2
    _AL_ALARM_SHIFT = _POS_BITS * 2 + 1
    _AL_LIVE_SHIFT = _POS_BITS * 2 + 2

    _WIRE_TO_ENV = (map_info._IDX_ENV_EMPTY,
                    map_info._IDX_ENV_WALL,
                    map_info._IDX_ENV_ORE_TI)

    _my_slot = _CORE_SLOT if _am_core else None
    _my_prev_pos = None
    _my_hb = 0
    _last_written = 0
    # Seed the scan from our own id so colliding claimants diverge immediately;
    # the step is likewise id-derived, so even two units whose ids agree modulo
    # the slot count separate on the next attempt.
    _probe_off = c.get_id() % _NUM_BUILDER_SLOTS
    _probe_step = 1 + (c.get_id() % (_NUM_BUILDER_SLOTS - 1))

    _pending_path_done = False
    _route_total = 0
    _core_route_total = 0

    _slot_prev_pos = [None] * _STORE_SIZE
    _slot_last_raw = [None] * _STORE_SIZE
    _slot_dead = [0] * _STORE_SIZE
    _slot_raw = [0] * _STORE_SIZE
    _raw_round = -1
    _read_round = -2

    _my_alarm_hb = 0
    _alarm_last_write = 0
    _alarm_last_raw = None
    _alarm_last_change = None
    _alarm_round = -1


# --------------------------------------------------------------------------- #
# Public helpers used by other modules
# --------------------------------------------------------------------------- #
def note_route_complete():
    """Called by route.py the turn a builder connects its routed source to the
    network. Latched until the next broadcast, which sends it as the path-done
    bit and then clears it."""
    global _pending_path_done
    _pending_path_done = True


def _alarm_observe() -> int:
    """Read slot 15 and age its staleness. Idempotent within a round.

    Ageing is keyed on the round number rather than counting calls, so it is
    safe to call from more than one place per turn and it does not under-count
    when a unit loses a turn to the CPU cap or a swallowed exception."""
    global _alarm_last_raw, _alarm_last_change, _alarm_round
    val = rc.read_store(_DEFENSE_SLOT)
    rnd = rc.get_current_round()
    if rnd != _alarm_round:
        _alarm_round = rnd
        if _alarm_last_raw is None:
            # First look. The store never clears itself, so whatever is latched
            # here may have been written by a sentry that died long ago. Take it
            # as a baseline ONLY -- a unit that spawns mid-game must not read a
            # stale word as a live alarm just because it is new to *us*.
            _alarm_last_raw = val
        elif val != _alarm_last_raw:
            _alarm_last_raw = val
            _alarm_last_change = rnd
    return val


def _alarm_stale(rnd: int) -> bool:
    return _alarm_last_change is None or rnd - _alarm_last_change >= _DEAD_AFTER


def write_alarm(launcher: Position, enemy: Position | None) -> None:
    """Sentry launcher: publish this round's defence alarm into slot 15.

    Called every round the sentry is alive, alarm or not -- the flipping
    heartbeat is what lets readers tell "no threat right now" from "the sentry
    is dead and this value is stale".

    Two launchers can each conclude locally that they are the sentry (the role
    is derived from per-unit remembered vision, and opposite arcs of the spawn
    ring are out of each other's sight). So this claims the slot the same way
    builders claim theirs: whoever loses the write race sees a word that is not
    its own and defers, and only takes over once the incumbent goes silent.
    Without that, the two interleave and readers get a launcher position from
    one sentry with an enemy position from the other."""
    global _my_alarm_hb, _alarm_last_write
    cur = _alarm_observe()
    rnd = rc.get_current_round()
    if _alarm_last_write != 0 and cur != _alarm_last_write:
        _alarm_last_write = 0                # clobbered: someone else owns the slot
    if _alarm_last_write == 0 and cur != 0 and not _alarm_stale(rnd):
        return                               # incumbent is live; stay quiet
    val = (launcher.x + launcher.y * _width) & _POS_MASK
    if enemy is not None:
        val |= ((enemy.x + enemy.y * _width) & _POS_MASK) << _AL_ENEMY_SHIFT
        val |= 1 << _AL_ALARM_SHIFT
    val |= (_my_alarm_hb & 1) << _AL_HB_SHIFT
    val |= 1 << _AL_LIVE_SHIFT
    rc.write_store(_DEFENSE_SLOT, val)
    _alarm_last_write = val
    _my_alarm_hb ^= 1


def read_alarm() -> tuple[Position, Position | None] | None:
    """Latest (sentry position, alarmed enemy position) from slot 15.

    Returns None when there is no live sentry -- either it never wrote, or its
    value has not changed for `_DEAD_AFTER` rounds, which means it died and the
    latched word must not be trusted. The enemy element is None when the sentry
    is alive but sees nothing worth answering."""
    val = _alarm_observe()
    if val == 0 or not ((val >> _AL_LIVE_SHIFT) & 1):
        return None
    if _alarm_stale(rc.get_current_round()):
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


# --------------------------------------------------------------------------- #
# Vision / delta helpers
# --------------------------------------------------------------------------- #
def _delta_tiles(now: Position, prev):
    """Sorted tile indices newly visible at `now` vs `prev`, or None to signal a
    re-baseline (no prior position, a launch/owner-change jump, or a reveal too
    big to fit). None => 0 tiles this turn; [] => moved but nothing new."""
    if prev is None:
        return None
    sx = now.x - prev.x
    sy = now.y - prev.y
    if sx < -1 or sx > 1 or sy < -1 or sy > 1:
        return None
    offs = _STEP_DELTA[(sx, sy)]
    if not offs:
        return []
    w = _width
    h = _height
    px = now.x
    py = now.y
    d = []
    for ox, oy in offs:
        x = px + ox
        y = py + oy
        if 0 <= x < w and 0 <= y < h:
            d.append(x + y * w)
    if len(d) > _MAX_TILES:
        return None
    return d


def _env_code(n: int) -> int:
    """Wire env code for tile index `n`, from map_info's env masks.

    A tile the writer has not actually seen reports as empty -- the wire has no
    "unknown" code, and the tile count is fixed by geometry so it cannot simply
    be dropped. `map_info.record_relayed_tile` is what keeps that from becoming
    false knowledge: it refuses tiles (core areas) that `update_at` never marks
    seen, which is the one case where this can be reached."""
    bit = 1 << n
    if map_info._bm_env[map_info._IDX_ENV_WALL] & bit:
        return _WIRE_WALL
    if map_info._bm_env[map_info._IDX_ENV_ORE_TI] & bit:
        return _WIRE_ORE
    return _WIRE_EMPTY


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
    return val


def _encode_builder(pos: Position, hb: int, path_done: int, rebase: int, tile_codes) -> int:
    val = (pos.x + pos.y * _width) & _POS_MASK
    val |= (hb & 1) << _HB_SHIFT
    val |= 1 << _VALID_SHIFT
    val |= (path_done & 1) << _PATHDONE_SHIFT
    val |= (rebase & 1) << _REBASE_SHIFT
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
        codes = () if d is None else [_env_code(n) for n in d]
        val = _encode_builder(my_pos, _my_hb, 1 if _pending_path_done else 0,
                              1 if d is None else 0, codes)
    rc.write_store(_my_slot, val)
    _last_written = val
    _my_prev_pos = my_pos
    _my_hb ^= 1
    _pending_path_done = False


# --------------------------------------------------------------------------- #
# Absorb (read every slot, learn symmetry + tiles + route tally)
# --------------------------------------------------------------------------- #
def _absorb():
    global _route_total, _core_route_total, _raw_round, _read_round
    rnd = rc.get_current_round()
    if rnd != _read_round + 1:
        # We did not read last round (CPU cutoff, or this is our first turn), so
        # we may have skipped a write. Every baseline is suspect and a same-parity
        # word can look unchanged, so drop them all: the next accepted word
        # decodes zero tiles and re-bases us. Costs one turn of tiles; keeping a
        # wrong baseline would corrupt the map permanently.
        for s in range(_STORE_SIZE):
            _slot_prev_pos[s] = None
    _read_round = rnd
    _raw_round = rnd

    dirty = False
    for s in range(_STORE_SIZE):
        if s == _DEFENSE_SLOT:
            continue                     # sentry layout; aged by _alarm_observe
        val = rc.read_store(s)
        _slot_raw[s] = val
        if val == 0:                     # never written -> free
            _slot_prev_pos[s] = None
            _slot_last_raw[s] = None
            _slot_dead[s] = 0
            continue

        # Liveness by raw value, not heartbeat parity: every write flips the
        # heartbeat so a live writer's word always differs from the last one we
        # saw, and a claimant seeds its heartbeat against the word it takes over
        # (see _ensure_slot). An unchanged word therefore means nobody wrote.
        if val == _slot_last_raw[s]:
            _slot_dead[s] += 1
            continue
        _slot_last_raw[s] = val
        _slot_dead[s] = 0

        # Track liveness for our own slot too -- but there is nothing to learn
        # from our own word. Skipping the bookkeeping (rather than just the
        # decode) used to freeze _slot_dead[_my_slot] forever, and _ensure_slot
        # reads that same array when deciding what is reclaimable.
        # Drop the baseline while we own the slot: we are not tracking this
        # writer, so if we later lose the slot and start decoding it again, the
        # position we last recorded belongs to an owner two hand-offs back.
        if s == _my_slot:
            _slot_prev_pos[s] = None
            continue

        n = val & _POS_MASK
        pos = Position(n % _width, n // _width)

        if s == _CORE_SLOT:
            sym_bits = (val >> _SYM_SHIFT) & 0x7
            map_info.update_symmetry_from_comms(sym_bits)
            _core_route_total = (val >> _TALLY_SHIFT) & _TALLY_MASK
            _slot_prev_pos[s] = pos      # core sends no tiles
            continue

        # Builder slot: tally connected-route reports (core only), then tiles.
        rebase = (val >> _REBASE_SHIFT) & 1
        if _am_core and ((val >> _PATHDONE_SHIFT) & 1):
            _route_total = (_route_total + 1) & _TALLY_MASK

        if not rebase:
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
                        code = _WIRE_EMPTY
                    if map_info.record_relayed_tile(tn, _WIRE_TO_ENV[code]):
                        dirty = True
        _slot_prev_pos[s] = pos

    if dirty:
        map_info.recompute_derived()


# --------------------------------------------------------------------------- #
# Slot claiming (builders only)
# --------------------------------------------------------------------------- #
def _ensure_slot():
    global _my_slot, _my_prev_pos, _my_hb, _probe_off
    # _absorb read every slot this round, and reads are stable within a round,
    # so reuse those values instead of asking the controller again.
    cached = (_raw_round == rc.get_current_round())
    if _my_slot is not None:
        # My previous write is visible this turn iff nobody clobbered the slot.
        cur = _slot_raw[_my_slot] if cached else rc.read_store(_my_slot)
        if _last_written != 0 and cur == _last_written:
            return
        _my_slot = None
        _my_prev_pos = None
        # Step the scan so we do not re-collide with the same rival next round.
        _probe_off = (_probe_off + _probe_step) % _NUM_BUILDER_SLOTS
    for k in range(_NUM_BUILDER_SLOTS):
        s = _FIRST_BUILDER_SLOT + (_probe_off + k) % _NUM_BUILDER_SLOTS
        cur = _slot_raw[s] if cached else rc.read_store(s)
        # Free (never written) or held by a unit that has gone silent.
        if cur == 0 or _slot_dead[s] >= _DEAD_AFTER:
            _my_slot = s
            _my_prev_pos = None
            _slot_dead[s] = 0
            _slot_last_raw[s] = cur
            # Make our first write differ from whatever is latched here, so
            # every reader sees it as a fresh write and re-bases off it.
            _my_hb = ((cur >> _HB_SHIFT) & 1) ^ 1
            return


# --------------------------------------------------------------------------- #
# Public entry point -- call once per turn after map_info.update()
# --------------------------------------------------------------------------- #
def read():
    _absorb()


def write():
    if _am_core:
        _broadcast()
    elif _am_builder:
        _ensure_slot()
        if _my_slot is not None:
            _broadcast()
