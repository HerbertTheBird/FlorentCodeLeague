import map_info
import map_identifier
from fcode import Controller
from _config import NUM_ATTACK, INITIAL_SPAWN_COUNT

rc: Controller = None

_BOARD_SLOTS = 8                                     # slots 0..7 hold the board
OPENING_ROLE_SLOT = 8
_ROLE_ID_MASK = 0x7FFF                               # opening ids are tiny

# Two compact launcher-defense claim lanes use slots 9..12. Each fixed economy
# builder owns the lane matching its economy index. One word stores enemy id +
# active flag; the next stores the last reported position and round.
_DEFENSE_CLAIM_BASE = 9
_DEFENSE_CLAIM_STRIDE = 2
_DEFENSE_ACTIVE_BIT = 1 << 16
_DEFENSE_REPORT_MAX_AGE = 4
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

# Slot 8 bit 14: set once the opening launcher has flung the whole roster.
# Builders then stop treating adjacency as opening-transport wait; completed
# economy defenders may deliberately return to that launcher for defense duty.
_LAUNCH_DONE_BIT = 1 << 14
_SENTINEL_COUNT_SHIFT = 15
_SENTINEL_COUNT_MASK = 0xF << _SENTINEL_COUNT_SHIFT


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


def sentinel_count() -> int:
    """Team-wide number of sentinels placed by the attack builder."""
    return (rc.read_store(OPENING_ROLE_SLOT) & _SENTINEL_COUNT_MASK) >> _SENTINEL_COUNT_SHIFT


def note_sentinel_built() -> None:
    v = rc.read_store(OPENING_ROLE_SLOT)
    count = sentinel_count() + 1
    field = (count << _SENTINEL_COUNT_SHIFT) & _SENTINEL_COUNT_MASK
    rc.write_store(OPENING_ROLE_SLOT, (v & ~_SENTINEL_COUNT_MASK) | field)


# --- opening roles ------------------------------------------------------------
# The opening builders' ids are broadcast one per store slot in spawn order,
# starting at _OPENING_ID_BASE (the old compressed-board slots 1..7 are free in
# Hermod). A builder's spawn INDEX determines its role: index < NUM_ATTACK -> that
# attacker slot, otherwise -> economy. Change NUM_ATTACK / NUM_ECON in spawn_plan
# and the whole split follows — here, in the core's spawn recording, and in the
# launcher's targeting.
_OPENING_ID_BASE = 1
_OPENING_ID_LAST = _BOARD_SLOTS - 1   # slots 1..7 available for opening ids
SIEGE_INSERT_SLOT = 4                 # opening uses only slots 1..3 in Hermod_v1
SIEGE_RELAUNCH_SLOT = 5
ATTACK_LAUNCH_COUNT_SLOT = 6
DEFENDER_READY_SLOT = 7


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


def economy_builder_id(index: int) -> int:
    """Opening economy/defense builder id for ``index`` (0 or 1)."""
    opening_index = NUM_ATTACK + index
    slot = _OPENING_ID_BASE + opening_index
    if not (0 <= index and slot <= _OPENING_ID_LAST):
        return 0
    return rc.read_store(slot)


def set_siege_insert(builder_id: int, site, facing) -> None:
    """Hand the launched attacker its exact cardinally-adjacent sentinel site."""
    try:
        direction_index = map_info._DIRECTIONS.index(facing)
    except ValueError:
        return
    value = (
        ((site.x + 1) & 0x3F)
        | (((site.y + 1) & 0x3F) << 6)
        | ((direction_index & 0x7) << 12)
        | ((builder_id & _ROLE_ID_MASK) << 15)
    )
    rc.write_store(SIEGE_INSERT_SLOT, value)


def siege_insert(builder_id: int):
    value = rc.read_store(SIEGE_INSERT_SLOT)
    if ((value >> 15) & _ROLE_ID_MASK) != builder_id:
        return None
    x = (value & 0x3F) - 1
    y = ((value >> 6) & 0x3F) - 1
    direction_index = (value >> 12) & 0x7
    if x < 0 or y < 0 or direction_index >= len(map_info._DIRECTIONS):
        return None
    from fcode import Position
    return Position(x, y), map_info._DIRECTIONS[direction_index]


def request_siege_relaunch(builder_id: int) -> None:
    """Fresh attack-builder request for a spent launcher to insert it again."""
    value = (
        (builder_id & _ROLE_ID_MASK)
        | (((rc.get_current_round() + 1) & 0xFFF) << 15)
    )
    rc.write_store(SIEGE_RELAUNCH_SLOT, value)


def siege_relaunch_requested(builder_id: int, max_age: int = 2) -> bool:
    value = rc.read_store(SIEGE_RELAUNCH_SLOT)
    if (value & _ROLE_ID_MASK) != builder_id:
        return False
    report_round = ((value >> 15) & 0xFFF) - 1
    age = rc.get_current_round() - report_round
    return 0 <= age <= max_age


def attack_launch_count() -> int:
    return rc.read_store(ATTACK_LAUNCH_COUNT_SLOT) & 0x3


def note_attack_launch() -> None:
    count = min(3, attack_launch_count() + 1)
    rc.write_store(ATTACK_LAUNCH_COUNT_SLOT, count)


def mark_defender_ready(lane: int) -> None:
    if lane not in (0, 1):
        return
    value = rc.read_store(DEFENDER_READY_SLOT)
    rc.write_store(DEFENDER_READY_SLOT, value | (1 << lane))


def defender_ready(lane: int) -> bool:
    return lane in (0, 1) and bool(rc.read_store(DEFENDER_READY_SLOT) & (1 << lane))


# --- persistent launcher-defense claims --------------------------------------
def _claim_slot(lane: int) -> int:
    return _DEFENSE_CLAIM_BASE + lane * _DEFENSE_CLAIM_STRIDE


def _report_slot(lane: int) -> int:
    return _claim_slot(lane) + 1


def _pack_defense_report(pos, round_num: int) -> int:
    return (
        ((pos.x + 1) & 0x3F)
        | (((pos.y + 1) & 0x3F) << 6)
        | (((round_num + 1) & 0x3FF) << 12)
    )


def _unpack_defense_report(value: int):
    x = (value & 0x3F) - 1
    y = ((value >> 6) & 0x3F) - 1
    round_num = ((value >> 12) & 0x3FF) - 1
    if x < 0 or y < 0 or round_num < 0:
        return None, -1
    from fcode import Position
    return Position(x, y), round_num


def set_defense_claim(lane: int, enemy_id: int, enemy_pos, active: bool) -> None:
    if lane not in (0, 1) or not enemy_id:
        return
    rc.write_store(
        _claim_slot(lane),
        (enemy_id & 0xFFFF) | (_DEFENSE_ACTIVE_BIT if active else 0),
    )
    rc.write_store(
        _report_slot(lane),
        _pack_defense_report(enemy_pos, rc.get_current_round()),
    )


def defense_claim(lane: int, allow_stale: bool = False):
    """Return ``(enemy_id, reported_position, active)`` for a live lane."""
    if lane not in (0, 1):
        return None
    word = rc.read_store(_claim_slot(lane))
    enemy_id = word & 0xFFFF
    if not enemy_id:
        return None
    pos, seen_round = _unpack_defense_report(rc.read_store(_report_slot(lane)))
    if pos is None:
        return None
    if not allow_stale and rc.get_current_round() - seen_round > _DEFENSE_REPORT_MAX_AGE:
        return None
    return enemy_id, pos, bool(word & _DEFENSE_ACTIVE_BIT)


def refresh_defense_claim(lane: int, enemy_id: int, enemy_pos) -> None:
    claim = defense_claim(lane, allow_stale=True)
    active = bool(claim is not None and claim[0] == enemy_id and claim[2])
    set_defense_claim(lane, enemy_id, enemy_pos, active)


def release_defense_claim(lane: int) -> None:
    if lane not in (0, 1):
        return
    rc.write_store(_claim_slot(lane), 0)
    rc.write_store(_report_slot(lane), 0)


def clear_stale_defense_claims() -> None:
    for lane in (0, 1):
        if (
            rc.read_store(_claim_slot(lane)) & 0xFFFF
            and defense_claim(lane) is None
        ):
            release_defense_claim(lane)


def claimed_defense_enemy_ids() -> set[int]:
    result = set()
    for lane in (0, 1):
        claim = defense_claim(lane)
        if claim is not None:
            result.add(claim[0])
    return result


def update() -> None:
    """Called once per builder/launcher/gunner round. Load the whole board from
    the map id the core published (see the slot-0 layout note) the first time it
    is available; a no-op every round after."""
    if rc is None:
        return
    sync_map()
