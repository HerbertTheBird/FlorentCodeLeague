"""Comms — global-store board sharing for Heimdall v0.

Titan replaced Cambridge's tile markers with a 16-slot per-team integer store.
Heimdall keeps Loki's compressed map sharing, but reserves six slots for the
two defensive builder/launcher handoff mailboxes. The board value uses slots
0..7; slot 8 holds the completed-ring and permanent-PvP flags. Stable rush and
economy IDs occupy reserved high bits in the two defender builder mailboxes so
phase-flag writes cannot erase them:

    bits 0..2   symmetry flags — bit0 horizontal, bit1 vertical, bit2 rotational
                (1 = still possible, 0 = invalidated). We only start writing once
                symmetry is confirmed (exactly one flag set).
    bits 3..    a base-3 number, one digit per tile of the *canonical half* of the
                board (tiles n with n <= mirror(n)): 0 = empty, 1 = wall,
                2 = titanium ore. The other half is reconstructed by mirroring
                under the confirmed symmetry, so the whole map is encoded.

Capacity: 253 bits hold 159 base-3 digits, so on maps whose half-board is larger
we share the lowest-indexed tiles that fit (walls + ore are what matter). Reading
folds shared walls/ore into map_info; writing pushes map_info's own knowledge
back, so the store accumulates the union of everyone's knowledge over time.
"""

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

# --- Board layout (slots 0..7 = 256 bits) --------------------------------------
# bits 0..2 : symmetry flags (hor/ver/rot still possible)
# bits 3..  : one 2-bit digit per canonical-half tile, 0=unknown 1=empty 2=wall
#             3=titanium-ore.
#
# A single 4-state plane (not a turn%2 double-buffer): the store uses BUFFERED
# writes, so within a round no unit can see another's write and the board is
# last-writer-wins. The only thing that keeps a shared board from being clobbered
# is fold-on-read -> write-back-your-superset: reading folds the board into your
# own _bm_seen, so when you re-encode your knowledge you preserve everyone
# else's. That invariant needs the SAME plane every round; a turn%2 scheme whose
# two rounds carry different planes breaks it (the off-plane write destroys what
# you'd merge against, so a tile only one unit has seen never propagates).
#
# Encoding "unknown" as digit 0 carries the known-vs-unknown distinction the
# alternating known/unknown plane was meant to provide — sharing a *known-empty*
# tile (digit 1) is exactly what lets pooled observations eliminate a symmetry —
# while staying single-plane and merge-safe. apply_shared_tile mirrors folded
# tiles across the axis once symmetry is solved.
_DATA_SHIFT = 3
_TILES = (_BOARD_SLOTS * 32 - _DATA_SHIFT) // 2   # tiles the board can hold

# canonical-half tile list (tiles n with n <= mirror(n)), cached per (w,h,sym)
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
    """Board tile order. Once symmetry is solved we transmit the canonical half
    (each folded tile is mirrored back on read, covering the whole map); before
    that we transmit the lowest-indexed tiles so pooled observations can start
    eliminating symmetries."""
    global _primary_cache_key, _primary_tiles
    key = (w, h, sym)
    if key == _primary_cache_key:
        return _primary_tiles
    if sym in (1, 2, 4):
        tiles = [n for n in range(w * h) if n <= _mirror(n, w, h, sym)][:_TILES]
    else:
        tiles = list(range(min(_TILES, w * h)))
    _primary_cache_key = key
    _primary_tiles = tiles
    return tiles


def _read_board() -> int:
    v = 0
    for i in range(_BOARD_SLOTS):
        v |= rc.read_store(i) << (32 * i)
    return v


def _write_board(v: int) -> None:
    for i in range(_BOARD_SLOTS):
        rc.write_store(i, (v >> (32 * i)) & 0xFFFFFFFF)


def _digit_to_env(digit: int) -> int:
    if digit == 1:
        return map_info._IDX_ENV_EMPTY
    if digit == 2:
        return map_info._IDX_ENV_WALL
    return map_info._IDX_ENV_ORE_TI


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


def assign_rusher(builder_id: int) -> None:
    """Publish the third builder in lane 0's reserved mailbox high bits."""
    slot = _handoff_slot(0, 0)
    value = rc.read_store(slot)
    mask = _ROLE_ID_MASK << _MAILBOX_ROLE_SHIFT
    rc.write_store(slot, (value & ~mask) | ((builder_id & _ROLE_ID_MASK) << _MAILBOX_ROLE_SHIFT))


def is_rusher(builder_id: int) -> bool:
    value = rc.read_store(_handoff_slot(0, 0))
    return ((value >> _MAILBOX_ROLE_SHIFT) & _ROLE_ID_MASK) == builder_id


def assign_economy(builder_id: int) -> None:
    """Publish the fourth builder in lane 1's reserved mailbox high bits."""
    slot = _handoff_slot(1, 0)
    value = rc.read_store(slot)
    mask = _ROLE_ID_MASK << _MAILBOX_ROLE_SHIFT
    rc.write_store(slot, (value & ~mask) | ((builder_id & _ROLE_ID_MASK) << _MAILBOX_ROLE_SHIFT))


def is_economy(builder_id: int) -> bool:
    value = rc.read_store(_handoff_slot(1, 0))
    return ((value >> _MAILBOX_ROLE_SHIFT) & _ROLE_ID_MASK) == builder_id


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
    v = _read_board()
    sym = v & 7
    if sym:
        # Possible-symmetry flags: every observer intersects them locally.
        map_info.update_symmetry_from_comms(sym)
    data = v >> _DATA_SHIFT
    order = _tile_order(map_info._width, map_info._height, sym)
    for i, n in enumerate(order):
        digit = (data >> (2 * i)) & 3
        if digit:      # 0 = unknown -> nothing to fold
            map_info.apply_shared_tile(n, _digit_to_env(digit))


def _write() -> None:
    my_sym = (
        (1 if map_info._hor_sym else 0)
        | (2 if map_info._ver_sym else 0)
        | (4 if map_info._rot_sym else 0)
    )
    v = _read_board()
    stored_sym = v & 7
    combined_sym = my_sym if stored_sym == 0 else (my_sym & stored_sym)
    # Start from the board already there and only OVERWRITE tiles I know — this
    # read-merge-write is what keeps other units' contributions from being
    # clobbered under the store's last-writer-wins buffered semantics.
    data = v >> _DATA_SHIFT

    seen = map_info._bm_seen
    wall = map_info._bm_env[map_info._IDX_ENV_WALL]
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    order = _tile_order(map_info._width, map_info._height, my_sym)
    for i, n in enumerate(order):
        bit = 1 << n
        if not (seen & bit):
            continue      # unknown to me — leave whatever the team already shared
        digit = 2 if (wall & bit) else (3 if (ore & bit) else 1)
        data = (data & ~(3 << (2 * i))) | (digit << (2 * i))

    _write_board(combined_sym | (data << _DATA_SHIFT))
