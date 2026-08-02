"""Comms — global-store board sharing for Heimdall.

Titan replaced Cambridge's tile markers with a 16-slot per-team integer store.
Heimdall uses slots 0..7 (32 bytes) for a zlib-compressed 4-state map board and
slots 8..15 for the two defensive builder/launcher handoff mailboxes, the
opening-role assignments, the gunner counter, and the reinforcement claim.

The map is very low entropy, so a whole 4-state half-board (0=unknown, 1=empty,
2=wall, 3=titanium-ore) compresses into the 8 board slots on nearly every
competition map. Each unit reads the board, decompresses it, merges in the tiles
it knows (only ever filling an unknown, never clearing a known one), recompresses
and writes back — so the store monotonically accumulates the union of everyone's
observations and multi-writer stays clobber-free under the store's buffered
last-writer-wins semantics. See the board-layout comment below for the byte
format and the pre/post-symmetry tile order.
"""

import zlib

import map_info
from fcode import Controller

rc: Controller = None

_BOARD_SLOTS = 8                                     # slots 0..7 hold the board
OPENING_ROLE_SLOT = 8
_ROLE_ID_MASK = 0x7FFF                               # opening ids are tiny
_RING_COMPLETE_BIT = 1 << 30
_GUNNER_PVP_BIT = 1 << 31

# Two three-word defensive handoff mailboxes. The low 16 bits of each builder
# slot hold that lane's defender ID; the high bits hold the stable rusher or
# economy ID. Launcher publications preserve those reserved role bits.
DEFENSE_HANDOFF_BASE = 9                             # lanes use slots 9..14
DEFENSE_HANDOFF_STRIDE = 3
_MAILBOX_ROLE_SHIFT = 16
_INTERCEPT_ACTIVE_BIT = 1 << 31
_DEFENDER_CLAIM_BIT = 1 << 30
_LAUNCHER_ID_MASK = _DEFENDER_CLAIM_BIT - 1
GUNNER_COUNT_SLOT = 15                               # slot 15: emergency reinforcement claim

# --- Board layout (slots 0..7 = 32 bytes) --------------------------------------
# byte 0 : symmetry flags (bits 0..2: hor/ver/rot still possible)
# byte 1 : L = length in bytes of the compressed payload
# 2..2+L : raw-DEFLATE of a 2-bit-per-tile 4-state stream (0=unknown 1=empty
#          2=wall 3=titanium-ore) over the current tile order.
#
# The map is very low entropy (mostly empty, clustered walls/ore, and — until
# fully explored — long runs of unknown), so zlib packs a whole 4-state
# half-board into well under 32 bytes on nearly every competition map (measured
# 11..35 B fully explored). Each unit does decode -> merge-its-knowledge ->
# re-encode -> write, which keeps the union monotonic (you only ever fill an
# unknown tile, never clear a known one), so multi-writer stays clobber-free
# under the store's buffered last-writer-wins semantics. If a dense map's stream
# won't fit, the low-index prefix that does is sent (deterministic, so every
# writer truncates identically).
_BOARD_BYTES = _BOARD_SLOTS * 4          # 32
_BLOB_BUDGET = _BOARD_BYTES - 2          # bytes available after the 2-byte header

# tile order, cached per (w, h, sym)
_primary_cache_key = None
_primary_tiles: list[int] = []


def init(c: Controller) -> None:
    global rc, _primary_cache_key
    rc = c
    _primary_cache_key = None


def _mirror(n: int, w: int, h: int, sym: int) -> int:
    x = n % w
    y = n // w
    if sym & 1:      # horizontal — reflect x
        return (w - 1 - x) + y * w
    if sym & 2:      # vertical — reflect y
        return x + (h - 1 - y) * w
    return (w - 1 - x) + (h - 1 - y) * w   # rotational


def _tile_order(w: int, h: int, sym: int) -> list[int]:
    """Tile transmission order. Once symmetry is solved we send the canonical
    half (each folded tile is mirrored back on read, covering the whole map);
    before that we send the whole board so pooled observations can eliminate
    symmetries. Compression + prefix truncation handle whatever fits."""
    global _primary_cache_key, _primary_tiles
    key = (w, h, sym)
    if key == _primary_cache_key:
        return _primary_tiles
    if sym in (1, 2, 4):
        tiles = [n for n in range(w * h) if n <= _mirror(n, w, h, sym)]
    else:
        tiles = list(range(w * h))
    _primary_cache_key = key
    _primary_tiles = tiles
    return tiles


def _digit_to_env(digit: int) -> int:
    if digit == 1:
        return map_info._IDX_ENV_EMPTY
    if digit == 2:
        return map_info._IDX_ENV_WALL
    return map_info._IDX_ENV_ORE_TI


def _read_board_bytes() -> bytes:
    out = bytearray(_BOARD_BYTES)
    for i in range(_BOARD_SLOTS):
        out[i * 4:i * 4 + 4] = rc.read_store(i).to_bytes(4, "little")
    return bytes(out)


def _write_board_bytes(b: bytes) -> None:
    b = b[:_BOARD_BYTES].ljust(_BOARD_BYTES, b"\x00")
    for i in range(_BOARD_SLOTS):
        rc.write_store(i, int.from_bytes(b[i * 4:i * 4 + 4], "little"))


def _deflate(data: bytes) -> bytes:
    co = zlib.compressobj(9, zlib.DEFLATED, -15)   # raw deflate, no header/checksum
    return co.compress(data) + co.flush()


def _pack_digits(order: list[int], digits: bytearray, k: int) -> bytes:
    ba = bytearray((k * 2 + 7) // 8)
    for idx in range(k):
        d = digits[order[idx]]
        if d:
            ba[idx >> 2] |= (d & 3) << ((idx & 3) * 2)
    return bytes(ba)


def _decode_board(board: bytes) -> tuple[int, bytearray]:
    """Return (sym, digits) where digits[n] in 0..3 for every tile index n."""
    sym = board[0] & 7
    length = board[1]
    digits = bytearray(map_info._width * map_info._height)
    if length == 0 or 2 + length > len(board):
        return sym, digits
    try:
        raw = zlib.decompressobj(-15).decompress(board[2:2 + length])
    except Exception:
        return sym, digits
    order = _tile_order(map_info._width, map_info._height, sym)
    for idx, n in enumerate(order):
        bi = idx >> 2
        if bi >= len(raw):
            break
        d = (raw[bi] >> ((idx & 3) * 2)) & 3
        if d:
            digits[n] = d
    return sym, digits


def _encode_board(sym: int, digits: bytearray) -> bytes:
    order = _tile_order(map_info._width, map_info._height, sym)
    k = len(order)
    blob = _deflate(_pack_digits(order, digits, k))
    if len(blob) > _BLOB_BUDGET:
        # Send the largest low-index prefix that fits (binary search; deflate
        # size grows monotonically with the tile count in practice).
        lo, hi = 0, k
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(_deflate(_pack_digits(order, digits, mid))) <= _BLOB_BUDGET:
                lo = mid
            else:
                hi = mid - 1
        blob = _deflate(_pack_digits(order, digits, lo))
    return bytes([sym & 7, len(blob)]) + blob


_GUNNER_COUNT_MASK = 0xFFFF   # slot 8 low 16 bits (ring/pvp flags live in bits 30/31)


def gunner_count() -> int:
    """Team-wide count of gunners built. No single unit sees every gunner (core
    vision is local), so builders bump this shared counter as they build and the
    core reads it to size ammo conversion."""
    return rc.read_store(OPENING_ROLE_SLOT) & _GUNNER_COUNT_MASK


def note_gunner_built() -> None:
    v = rc.read_store(OPENING_ROLE_SLOT)
    count = (v & _GUNNER_COUNT_MASK) + 1
    rc.write_store(OPENING_ROLE_SLOT, (v & ~_GUNNER_COUNT_MASK) | (count & _GUNNER_COUNT_MASK))


def _handoff_slot(lane: int, field: int) -> int:
    return DEFENSE_HANDOFF_BASE + lane * DEFENSE_HANDOFF_STRIDE + field


def assign_defender(lane: int, builder_id: int) -> None:
    """Assign initial-spawn builder ``builder_id`` to defense lane 0 or 1."""
    slot = _handoff_slot(lane, 0)
    value = rc.read_store(slot)
    rc.write_store(slot, (value & 0xFFFF0000) | (builder_id & 0xFFFF))


# The two attack builders live in the reserved high bits of the two defender
# mailboxes (slot 9 = atk 0, slot 12 = atk 1); launcher/defender writes preserve
# those high bits. The economy builder lives in slot 8 bits 16..29 (low 16 =
# gunner counter, bits 30/31 = ring/pvp flags).
_ECON_ID_SHIFT = 16
_ECON_ID_MASK = 0x3FFF


def assign_atk(index: int, builder_id: int) -> None:
    """Publish attack builder ``index`` (0 or 1) in lane ``index``'s mailbox
    high bits."""
    slot = _handoff_slot(index, 0)
    value = rc.read_store(slot)
    mask = _ROLE_ID_MASK << _MAILBOX_ROLE_SHIFT
    rc.write_store(slot, (value & ~mask) | ((builder_id & _ROLE_ID_MASK) << _MAILBOX_ROLE_SHIFT))


def atk_index(builder_id: int):
    """Return which attack slot (0 or 1) this builder was assigned, else None."""
    if not builder_id:
        return None
    for index in (0, 1):
        value = rc.read_store(_handoff_slot(index, 0))
        if ((value >> _MAILBOX_ROLE_SHIFT) & _ROLE_ID_MASK) == builder_id:
            return index
    return None


def assign_economy(builder_id: int) -> None:
    v = rc.read_store(OPENING_ROLE_SLOT)
    mask = _ECON_ID_MASK << _ECON_ID_SHIFT
    rc.write_store(OPENING_ROLE_SLOT, (v & ~mask) | ((builder_id & _ECON_ID_MASK) << _ECON_ID_SHIFT))


def is_economy(builder_id: int) -> bool:
    if not builder_id:
        return False
    v = rc.read_store(OPENING_ROLE_SLOT)
    return ((v >> _ECON_ID_SHIFT) & _ECON_ID_MASK) == (builder_id & _ECON_ID_MASK)


def mark_ring_complete() -> None:
    rc.write_store(OPENING_ROLE_SLOT, rc.read_store(OPENING_ROLE_SLOT) | _RING_COMPLETE_BIT)


def ring_complete() -> bool:
    return bool(rc.read_store(OPENING_ROLE_SLOT) & _RING_COMPLETE_BIT)


def mark_gunner_pvp() -> None:
    """Permanently tell both defenders to stop launcher construction."""
    rc.write_store(OPENING_ROLE_SLOT, rc.read_store(OPENING_ROLE_SLOT) | _GUNNER_PVP_BIT)


def gunner_pvp() -> bool:
    return bool(rc.read_store(OPENING_ROLE_SLOT) & _GUNNER_PVP_BIT)


def defender_lane(builder_id: int) -> int | None:
    """Return this builder's defense lane, or None for rush/economy bots."""
    for lane in (0, 1):
        if (rc.read_store(_handoff_slot(lane, 0)) & 0xFFFF) == builder_id:
            return lane
    return None


def defender_id(lane: int) -> int:
    """Return the builder assigned to a defensive lane."""
    return rc.read_store(_handoff_slot(lane, 0)) & 0xFFFF


def pack_position(pos) -> int:
    # Zero means no target; the +1 offsets preserve the real (0, 0) tile.
    return ((pos.x + 1) << 16) | (pos.y + 1)


def unpack_position(value: int):
    if value == 0:
        return None
    from fcode import Position
    return Position((value >> 16) - 1, (value & 0xFFFF) - 1)


def publish_launcher_handoff(lane: int, builder_id: int, launcher_id: int, next_pos) -> None:
    """Publish a freshly built launcher and where it should throw its builder."""
    builder_slot = _handoff_slot(lane, 0)
    value = rc.read_store(builder_slot)
    rc.write_store(builder_slot, (value & 0xFFFF0000) | (builder_id & 0xFFFF))
    # A new construction handoff ends any previous intercept assignment.
    rc.write_store(_handoff_slot(lane, 1), launcher_id & _LAUNCHER_ID_MASK)
    rc.write_store(_handoff_slot(lane, 2), pack_position(next_pos) if next_pos is not None else 0)


def publish_defender_home(lane: int, builder_id: int, launcher_id: int) -> None:
    """Assign exactly one completed-ring home launcher to this defender."""
    builder_slot = _handoff_slot(lane, 0)
    value = rc.read_store(builder_slot)
    rc.write_store(builder_slot, (value & 0xFFFF0000) | (builder_id & 0xFFFF))
    launcher_slot = _handoff_slot(lane, 1)
    previous_launcher = rc.read_store(launcher_slot) & _LAUNCHER_ID_MASK
    rc.write_store(launcher_slot, launcher_id & _LAUNCHER_ID_MASK)
    # Clear the construction-time next-site payload only when switching this
    # lane to its home launcher. Later rebroadcasts preserve intruder sightings.
    if previous_launcher != launcher_id:
        rc.write_store(_handoff_slot(lane, 2), 0)


def defender_home_for_launcher(launcher_id: int):
    """Return ``(lane, defender_id)`` only for a finalized home launcher."""
    for lane in (0, 1):
        if (
            rc.read_store(_handoff_slot(lane, 1)) & _LAUNCHER_ID_MASK
        ) == launcher_id:
            builder_id = rc.read_store(_handoff_slot(lane, 0)) & 0xFFFF
            if builder_id:
                return lane, builder_id
    return None


def claim_enemy_builder(lane: int, enemy_id: int) -> None:
    """Reserve one enemy builder for exactly one defender lane."""
    slot = _handoff_slot(lane, 1)
    rc.write_store(slot, _DEFENDER_CLAIM_BIT | (enemy_id & 0xFFFF))


def activate_defender_intercept(lane: int, enemy_id: int) -> None:
    """Mark this lane's claimed defender as launched and pure-mirroring."""
    slot = _handoff_slot(lane, 1)
    rc.write_store(
        slot,
        _INTERCEPT_ACTIVE_BIT | _DEFENDER_CLAIM_BIT | (enemy_id & 0xFFFF),
    )


def claimed_enemy_id(lane: int) -> int:
    value = rc.read_store(_handoff_slot(lane, 1))
    if not (value & _DEFENDER_CLAIM_BIT):
        return 0
    return value & 0xFFFF


def defender_claim_pending(lane: int) -> bool:
    value = rc.read_store(_handoff_slot(lane, 1))
    return bool(value & _DEFENDER_CLAIM_BIT) and not bool(value & _INTERCEPT_ACTIVE_BIT)


def defender_intercepting(lane: int) -> bool:
    return bool(rc.read_store(_handoff_slot(lane, 1)) & _INTERCEPT_ACTIVE_BIT)


# Slot 15 emergency reinforcement record:
# bits 0..8 enemy id, 9..17 defender id, 18..23 x, 24..29 y,
# bit30 launched, bit31 valid. Maps are <=64x64 and early entity ids are <512.
_REINFORCEMENT_VALID = 1 << 31
_REINFORCEMENT_LAUNCHED = 1 << 30


def _reinforcement_fields(value: int):
    return (
        value & 0x1FF,
        (value >> 9) & 0x1FF,
        (value >> 18) & 0x3F,
        (value >> 24) & 0x3F,
        bool(value & _REINFORCEMENT_LAUNCHED),
    )


def publish_reinforcement_enemy(enemy_id: int, pos) -> None:
    value = rc.read_store(GUNNER_COUNT_SLOT)
    if value & _REINFORCEMENT_VALID:
        current_enemy, defender_id, _x, _y, launched = _reinforcement_fields(value)
        if current_enemy != enemy_id:
            return
        if defender_id == 0:
            # The core may be assigning the spawned defender in this same
            # buffered-write round. Do not overwrite that assignment with the
            # previously committed zero defender id.
            return
    else:
        defender_id = 0
        launched = False
    packed = (
        _REINFORCEMENT_VALID
        | (_REINFORCEMENT_LAUNCHED if launched else 0)
        | (enemy_id & 0x1FF)
        | ((defender_id & 0x1FF) << 9)
        | ((pos.x & 0x3F) << 18)
        | ((pos.y & 0x3F) << 24)
    )
    rc.write_store(GUNNER_COUNT_SLOT, packed)


def reinforcement_claim():
    value = rc.read_store(GUNNER_COUNT_SLOT)
    if not (value & _REINFORCEMENT_VALID):
        return None
    enemy_id, defender_id, x, y, launched = _reinforcement_fields(value)
    from fcode import Position
    return enemy_id, defender_id, Position(x, y), launched


def assign_reinforcement(defender_id: int) -> None:
    value = rc.read_store(GUNNER_COUNT_SLOT)
    if not (value & _REINFORCEMENT_VALID):
        return
    enemy_id, _old_defender, x, y, launched = _reinforcement_fields(value)
    rc.write_store(
        GUNNER_COUNT_SLOT,
        _REINFORCEMENT_VALID
        | (_REINFORCEMENT_LAUNCHED if launched else 0)
        | enemy_id
        | ((defender_id & 0x1FF) << 9)
        | (x << 18)
        | (y << 24),
    )


def activate_reinforcement() -> None:
    value = rc.read_store(GUNNER_COUNT_SLOT)
    if value & _REINFORCEMENT_VALID:
        rc.write_store(GUNNER_COUNT_SLOT, value | _REINFORCEMENT_LAUNCHED)


def reinforcement_for_builder(builder_id: int):
    claim = reinforcement_claim()
    if claim is None:
        return None
    enemy_id, defender_id, pos, launched = claim
    if defender_id != builder_id:
        return None
    return enemy_id, pos, launched


def publish_lane_intruder(lane: int, pos, current_round: int) -> None:
    """Publish a fresh sighting in this lane's handoff payload word."""
    value = (
        ((current_round & 0xFFFF) << 16)
        | (((pos.y + 1) & 0xFF) << 8)
        | ((pos.x + 1) & 0xFF)
    )
    rc.write_store(_handoff_slot(lane, 2), value)


def lane_intruder(lane: int, current_round: int, max_age: int = 2):
    """Return a recently launcher-seen intruder position for ``lane``."""
    value = rc.read_store(_handoff_slot(lane, 2))
    if value == 0:
        return None
    seen_round = (value >> 16) & 0xFFFF
    age = (current_round - seen_round) & 0xFFFF
    if age > max_age:
        return None
    x = (value & 0xFF) - 1
    y = ((value >> 8) & 0xFF) - 1
    if x < 0 or y < 0:
        return None
    from fcode import Position
    return Position(x, y)


def launcher_handoff(launcher_id: int):
    """Return (builder_id, next_pos) when ``launcher_id`` owns a mailbox."""
    for lane in (0, 1):
        launcher_value = rc.read_store(_handoff_slot(lane, 1))
        if (
            not (launcher_value & _INTERCEPT_ACTIVE_BIT)
            and (launcher_value & _LAUNCHER_ID_MASK) == launcher_id
        ):
            builder_id = rc.read_store(_handoff_slot(lane, 0)) & 0xFFFF
            next_pos = unpack_position(rc.read_store(_handoff_slot(lane, 2)))
            if builder_id and next_pos is not None:
                return builder_id, next_pos
    return None


def update() -> None:
    """Called once per builder/launcher round. Fold shared symmetry/map data
    into map_info, then contribute this entity's observations back."""
    if rc is None:
        return
    _read()
    _write()


def _read() -> None:
    sym, digits = _decode_board(_read_board_bytes())
    if sym:
        # Possible-symmetry flags: every observer intersects them locally.
        map_info.update_symmetry_from_comms(sym)
    for n, d in enumerate(digits):
        if d:      # 0 = unknown -> nothing to fold
            map_info.apply_shared_tile(n, _digit_to_env(d))


def _write() -> None:
    my_sym = (
        (1 if map_info._hor_sym else 0)
        | (2 if map_info._ver_sym else 0)
        | (4 if map_info._rot_sym else 0)
    )
    # Decode what the team has shared, then fill in only tiles I know (never
    # clearing a known one). This monotonic read-merge-write keeps other units'
    # contributions from being clobbered under buffered last-writer-wins.
    stored_sym, digits = _decode_board(_read_board_bytes())
    combined_sym = my_sym if stored_sym == 0 else (my_sym & stored_sym)

    seen = map_info._bm_seen
    wall = map_info._bm_env[map_info._IDX_ENV_WALL]
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    order = _tile_order(map_info._width, map_info._height, combined_sym)
    for n in order:
        if digits[n]:
            continue      # already known to the team
        bit = 1 << n
        if not (seen & bit):
            continue      # unknown to me too
        digits[n] = 2 if (wall & bit) else (3 if (ore & bit) else 1)

    _write_board_bytes(_encode_board(combined_sym, digits))
