import map_info
import map_identifier
from fcode import Controller
from _config import NUM_ATTACK, INITIAL_SPAWN_COUNT

rc: Controller = None

_BOARD_SLOTS = 8                                     # slots 0..7 hold the board
OPENING_ROLE_SLOT = 8
_ROLE_ID_MASK = 0x7FFF                               # opening ids are tiny

# Opening-role mailboxes. Only the low word (field 0) of each of the two lane
# slots is used now (the launcher-ring handoff fields are gone): the high bits
# hold attacker 0/1 and the low 16 hold attacker 2 / economy 1. Slots 10, 11,
# 13, 14 and 15 are unused.
DEFENSE_HANDOFF_BASE = 9                             # role words live in slots 9 and 12
DEFENSE_HANDOFF_STRIDE = 3
_MAILBOX_ROLE_SHIFT = 16

# --- Map identity (slot 0) -----------------------------------------------------
# The whole map is a known member of the fixed pool (see map_identifier), so
# rather than pool per-tile observations we transmit only which map this is. The
# core identifies it from its own core origin + the map size (a unique key across
# the pool) and writes a small field into slot 0:
#   bits 0..ID_BITS-1 : map index + 1  (0 = not yet identified)
#   bit  ID_BITS      : side — which of the map's two stored cores is ours, so a
#                       unit that never sees its own core still knows friend from
#                       foe.
# Every unit reads this once and loads the entire board (walls, ore, both cores,
# symmetry) via map_identifier.load(). Slots 1..7 (the old compressed-board
# region) are now unused.
MAP_ID_SLOT = 0
_ID_BITS = map_identifier.ID_BITS
_MAP_INDEX_MASK = (1 << _ID_BITS) - 1
_SIDE_SHIFT = _ID_BITS
_MAP_FIELD_MASK = (1 << (_ID_BITS + 1)) - 1

# Once True this unit has loaded the full board into map_info; sync_map() is a
# no-op thereafter.
_map_loaded = False


def init(c: Controller) -> None:
    global rc, _map_loaded
    rc = c
    _map_loaded = False


def _load_from_field(v: int) -> bool:
    """Load the map named by a slot-0 value, if it names one. Returns True on a
    successful (or already-done) load."""
    global _map_loaded
    index1 = v & _MAP_INDEX_MASK
    if not index1:
        return False
    idx = index1 - 1
    side = (v >> _SIDE_SHIFT) & 1
    if map_identifier.load(idx, side):
        _map_loaded = True
        return True
    return False


def _publish_field(idx: int, side: int) -> None:
    field = ((idx + 1) & _MAP_INDEX_MASK) | ((side & 1) << _SIDE_SHIFT)
    cur = rc.read_store(MAP_ID_SLOT)
    if (cur & _MAP_INDEX_MASK):
        return                       # a teammate already published it
    rc.write_store(MAP_ID_SLOT, (cur & ~_MAP_FIELD_MASK) | field)


def _identify_publish_load(core) -> bool:
    """Identify the map from `core` (our own core origin), publish its id for
    teammates, and load the full board locally. Returns False if we can't yet
    (no core / unknown map)."""
    global _map_loaded
    if core is None:
        return False
    res = map_identifier.identify(map_info._width, map_info._height, core)
    if res is None:
        return False
    idx, side = res
    _publish_field(idx, side)
    if map_identifier.load(idx, side):
        _map_loaded = True
        return True
    return False


def publish_identified_map() -> bool:
    """Core: identify the map from our own core origin and publish it, loading
    the full board locally too. Idempotent — safe to call every round."""
    if _map_loaded:
        return True
    if _load_from_field(rc.read_store(MAP_ID_SLOT)):
        return True
    return _identify_publish_load(map_info._my_core or map_info._my_pos)


def sync_map() -> bool:
    """Any unit: load the full board once the map id is available. Reads the
    core's published index; failing that, self-identifies if we already know our
    own core (and publishes so teammates benefit). No-op once loaded."""
    if _map_loaded:
        return True
    if _load_from_field(rc.read_store(MAP_ID_SLOT)):
        return True
    return _identify_publish_load(map_info._my_core)


_GUNNER_COUNT_MASK = 0x3FFF   # slot 8 bits 0..13 (ring/pvp flags live in bits 30/31)

# Slot 8 bit 14: set once the opening launcher has flung the whole roster and
# self-destructed. Builders read it to stop waiting to be launched by a launcher
# that no longer exists (otherwise one lingering near the old launcher tile holds
# forever).
_LAUNCH_DONE_BIT = 1 << 14


def mark_launch_done() -> None:
    v = rc.read_store(OPENING_ROLE_SLOT)
    if not (v & _LAUNCH_DONE_BIT):
        rc.write_store(OPENING_ROLE_SLOT, v | _LAUNCH_DONE_BIT)


def launch_done() -> bool:
    return bool(rc.read_store(OPENING_ROLE_SLOT) & _LAUNCH_DONE_BIT)


def gunner_count() -> int:
    """Team-wide count of gunners built. No single unit sees every gunner (core
    vision is local), so builders bump this shared counter as they build and the
    core reads it to size ammo conversion."""
    return rc.read_store(OPENING_ROLE_SLOT) & _GUNNER_COUNT_MASK


def note_gunner_built() -> None:
    v = rc.read_store(OPENING_ROLE_SLOT)
    count = (v & _GUNNER_COUNT_MASK) + 1
    rc.write_store(OPENING_ROLE_SLOT, (v & ~_GUNNER_COUNT_MASK) | (count & _GUNNER_COUNT_MASK))


# --- opening roles ------------------------------------------------------------
# The opening builders' ids are broadcast one per store slot in spawn order,
# starting at _OPENING_ID_BASE (the old compressed-board slots 1..7 are free in
# Zeus). A builder's spawn INDEX determines its role: index < NUM_ATTACK -> that
# attacker slot, otherwise -> economy. Change NUM_ATTACK / NUM_ECON in spawn_plan
# and the whole split follows — here, in the core's spawn recording, and in the
# launcher's targeting.
_OPENING_ID_BASE = 1
_OPENING_ID_LAST = _BOARD_SLOTS - 1   # slots 1..7 available for opening ids


def rebroadcast_opening(opening_ids) -> None:
    """Broadcast opening builder ids by spawn index. Buffered writes are repeated
    every round until every builder has recognized its assignment."""
    for i, bid in enumerate(opening_ids):
        if bid and _OPENING_ID_BASE + i <= _OPENING_ID_LAST:
            rc.write_store(_OPENING_ID_BASE + i, bid)


def _opening_index(builder_id: int):
    """The spawn index of an opening builder, or None if it isn't one."""
    if not builder_id:
        return None
    for i in range(INITIAL_SPAWN_COUNT):
        if _OPENING_ID_BASE + i > _OPENING_ID_LAST:
            break
        if rc.read_store(_OPENING_ID_BASE + i) == builder_id:
            return i
    return None


def atk_index(builder_id: int):
    """This builder's attacker index (0..NUM_ATTACK-1), or None if not an attacker."""
    i = _opening_index(builder_id)
    return i if (i is not None and i < NUM_ATTACK) else None


def is_economy(builder_id: int) -> bool:
    """True if this builder is one of the opening economy builders."""
    i = _opening_index(builder_id)
    return i is not None and i >= NUM_ATTACK


def economy_index(builder_id: int):
    """This opening economy/defender's zero-based slot, or None."""
    i = _opening_index(builder_id)
    return i - NUM_ATTACK if i is not None and i >= NUM_ATTACK else None


def update() -> None:
    """Called once per builder/launcher/gunner round. Load the whole board from
    the map id the core published (see the slot-0 layout note) the first time it
    is available; a no-op every round after."""
    if rc is None:
        return
    sync_map()
