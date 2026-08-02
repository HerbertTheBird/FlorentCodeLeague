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

from math import log2

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
_SYM_BITS = 3
_BOARD_BITS = _BOARD_SLOTS * 32 - _SYM_BITS
_MAX_TILES = int(_BOARD_BITS / log2(3))

# canonical-half tile list, cached per (w, h, symmetry)
_primary_cache_key = None
_primary_tiles: list[int] = []

_last_read_v: int | None = None       # last store value we folded into map_info
_last_written_v: int | None = None    # last value we wrote
_last_write_map_key = None            # symmetry/wall/ore snapshot at last write


def init(c: Controller) -> None:
    global rc, _primary_cache_key, _last_read_v, _last_written_v, _last_write_map_key
    rc = c
    _primary_cache_key = None
    _last_read_v = None
    _last_written_v = None
    _last_write_map_key = None


def _mirror(n: int, w: int, h: int, sym: int) -> int:
    x = n % w
    y = n // w
    if sym & 1:      # horizontal — reflect x
        return (w - 1 - x) + y * w
    if sym & 2:      # vertical — reflect y
        return x + (h - 1 - y) * w
    return (w - 1 - x) + (h - 1 - y) * w   # rotational


def _get_primary(w: int, h: int, sym: int) -> list[int]:
    """Ordered canonical half: tiles n with n <= mirror(n), capped to what fits."""
    global _primary_cache_key, _primary_tiles
    key = (w, h, sym)
    if key == _primary_cache_key:
        return _primary_tiles
    tiles = []
    for n in range(w * h):
        if n <= _mirror(n, w, h, sym):
            tiles.append(n)
            if len(tiles) >= _MAX_TILES:
                break
    _primary_cache_key = key
    _primary_tiles = tiles
    return tiles


def _read_value() -> int:
    v = 0
    for i in range(_BOARD_SLOTS):
        v |= rc.read_store(i) << (32 * i)
    return v


def _write_value(v: int) -> None:
    for i in range(_BOARD_SLOTS):
        rc.write_store(i, (v >> (32 * i)) & 0xFFFFFFFF)


def gunner_count() -> int:
    """Legacy ammo heuristic; slot 15 now carries emergency defense claims."""
    return 0


def note_gunner_built() -> None:
    return


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


def _apply_tile(n: int, env_idx: int) -> None:
    bit = 1 << n
    for e in range(len(map_info._bm_env)):
        map_info._bm_env[e] &= ~bit
    map_info._bm_env[env_idx] |= bit
    map_info._bm_seen |= bit


def update() -> None:
    """Called once per builder/launcher round. Fold shared symmetry/map data
    into map_info, then contribute this entity's observations back."""
    if rc is None:
        return
    _read()
    _write()


def _read() -> None:
    global _last_read_v
    v = _read_value()
    if v == _last_read_v:
        return
    _last_read_v = v
    sym = v & 7
    board = v >> _SYM_BITS
    if sym:
        # Possible-symmetry flags are useful before the team has converged on a
        # single answer. Every observer intersects them with its local flags.
        map_info.update_symmetry_from_comms(sym)
    if sym not in (1, 2, 4) or board == 0:
        return  # no symmetry-specific board payload usable yet

    w = map_info._width
    h = map_info._height
    primary = _get_primary(w, h, sym)
    wall_idx = map_info._IDX_ENV_WALL
    ore_idx = map_info._IDX_ENV_ORE_TI
    seen = map_info._bm_seen
    changed = False
    num = board
    for n in primary:
        digit = num % 3
        num //= 3
        if digit == 0:
            continue  # empty is ambiguous with "unseen" — leave to local vision
        env_idx = wall_idx if digit == 1 else ore_idx
        m = _mirror(n, w, h, sym)
        for t in (n, m):
            if not (seen >> t) & 1:      # don't clobber our own direct observations
                _apply_tile(t, env_idx)
                changed = True
    if changed:
        map_info._struct_version += 1
        # refresh local `seen` used above is stale after edits, fine for this pass


def _write() -> None:
    global _last_written_v, _last_write_map_key
    sym = (1 if map_info._hor_sym else 0) | (2 if map_info._ver_sym else 0) | (4 if map_info._rot_sym else 0)

    if not map_info._solved_sym:
        # Share eliminations immediately, before a single symmetry is known.
        # Zero in the store means uninitialized; otherwise intersect with the
        # team's prior possibilities. Preserve the rest of slot 0 verbatim.
        old_word = rc.read_store(0)
        shared_sym = old_word & 7
        combined_sym = sym if shared_sym == 0 else sym & shared_sym
        if combined_sym != shared_sym:
            rc.write_store(0, (old_word & ~7) | combined_sym)
        return
    if sym not in (1, 2, 4):
        return

    wall = map_info._bm_env[map_info._IDX_ENV_WALL]
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
    map_key = (sym, wall, ore)
    # Unlike _struct_version, this catches newly observed ore as well as walls.
    if map_key == _last_write_map_key:
        return
    _last_write_map_key = map_key

    w = map_info._width
    h = map_info._height
    primary = _get_primary(w, h, sym)
    num = 0
    for n in reversed(primary):        # primary[0] = least-significant digit
        bit = 1 << n
        digit = 1 if (wall & bit) else (2 if (ore & bit) else 0)
        num = num * 3 + digit
    v = sym | (num << _SYM_BITS)
    if v == _last_written_v:
        return
    _last_written_v = v
    _write_value(v)
