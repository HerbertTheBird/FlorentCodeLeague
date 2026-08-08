"""Comms — global-store board sharing for Loki.

Titan replaced Cambridge's tile markers with a 16-slot per-team integer store
(16 x u32 = 512 bits). Loki uses it to share the whole map. The 512-bit value:

    bits 0..2   symmetry flags — bit0 horizontal, bit1 vertical, bit2 rotational
                (1 = still possible, 0 = invalidated). We only start writing once
                symmetry is confirmed (exactly one flag set).
    bits 3..    a base-3 number, one digit per tile of the *canonical half* of the
                board (tiles n with n <= mirror(n)): 0 = empty, 1 = wall,
                2 = titanium ore. The other half is reconstructed by mirroring
                under the confirmed symmetry, so the whole map is encoded.

Capacity: 509 bits hold ~321 base-3 digits, so on maps whose half-board is larger
we share the lowest-indexed tiles that fit (walls + ore are what matter). Reading
folds shared walls/ore into map_info; writing pushes map_info's own knowledge
back, so the store accumulates the union of everyone's knowledge over time.
"""

from math import log2

import map_info
from fcode import Controller

rc: Controller = None

_BOARD_SLOTS = 15                                    # slots 0..14 hold the board
GUNNER_COUNT_SLOT = 15                               # slot 15: gunners placed by our builders
_SYM_BITS = 3
_BOARD_BITS = _BOARD_SLOTS * 32 - _SYM_BITS          # 477
_MAX_TILES = int(_BOARD_BITS / log2(3))              # 300

# canonical-half tile list, cached per (w, h, symmetry)
_primary_cache_key = None
_primary_tiles: list[int] = []

_last_read_v: int | None = None       # last store value we folded into map_info
_last_written_v: int | None = None    # last value we wrote
_last_write_struct = -1               # map_info._struct_version at last write


def init(c: Controller) -> None:
    global rc, _primary_cache_key, _last_read_v, _last_written_v, _last_write_struct
    rc = c
    _primary_cache_key = None
    _last_read_v = None
    _last_written_v = None
    _last_write_struct = -1


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
    """Gunners placed by our builder bots (as of the start of this round)."""
    return rc.read_store(GUNNER_COUNT_SLOT)


def note_gunner_built() -> None:
    """Increment the shared gunner counter. Store writes are buffered, so two
    builders placing gunners in the same round lose one increment; acceptable
    for an ammo-target heuristic."""
    rc.write_store(GUNNER_COUNT_SLOT, rc.read_store(GUNNER_COUNT_SLOT) + 1)


def _apply_tile(n: int, env_idx: int) -> None:
    bit = 1 << n
    for e in range(len(map_info._bm_env)):
        map_info._bm_env[e] &= ~bit
    map_info._bm_env[env_idx] |= bit
    map_info._bm_seen |= bit


def update() -> None:
    """Called once per unit per round (from builder.handle_comms). Folds the
    store's shared board into map_info, then writes our knowledge back."""
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
    if sym not in (1, 2, 4) or board == 0:
        return  # nothing usable shared yet

    # adopt the shared symmetry if we haven't solved ours
    if not map_info._solved_sym:
        map_info.update_symmetry_from_comms(sym)

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
    global _last_written_v, _last_write_struct
    if not map_info._solved_sym:
        return
    sym = (1 if map_info._hor_sym else 0) | (2 if map_info._ver_sym else 0) | (4 if map_info._rot_sym else 0)
    if sym not in (1, 2, 4):
        return
    # only re-encode when our map knowledge changed
    if map_info._struct_version == _last_write_struct:
        return
    _last_write_struct = map_info._struct_version

    w = map_info._width
    h = map_info._height
    primary = _get_primary(w, h, sym)
    wall = map_info._bm_env[map_info._IDX_ENV_WALL]
    ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI]
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
