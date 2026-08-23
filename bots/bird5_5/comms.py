"""Global-store communication -- 2-slot protocol.

The 16-slot per-team store is carved into 64-bit words (two adjacent 32-bit
slots each, low slot = bits 0..31, high slot = bits 32..63):

    slots 0,1    -> the core          (one 64-bit word)
    slots 2+2i.. -> builder bot i      (i = 0..6; pairs 2/3, 4/5, ... 14/15)

So at most 7 builder bots ever get a slot. Ownership is PERMANENT: builder i
keeps its pair for the whole game; when it dies the core zeroes its two slots so
the pair reads 0 (== empty) for everyone. Nothing is ever reassigned.

Timing: read() at the START of the turn (so the unit acts on max info) and
write() at the END (so it deposits max info). Writes are buffered one round.

------------------------------------------------------------------- core word
By round (readers know the round, so no marker bit is needed):
  rounds 0-3 : the raw conveyor-plan DFS bitstream for the builder spawned that
               round (see conveyor_plan.py). That builder reads it next round.
  round 4    : [ sym:3 | id0:5 | id1:5 | id2:5 | id3:5 ]  -- the 4 original
               builders' ids mod 32, in spawn order. (Builders self-assign their
               slot from their spawn round, so this is only for map_info to learn
               which id owns which of pairs 0..3.)
  round >=5  : [ sym:3 | income:7 | just_spawned:1 ]  and, ONLY when
               just_spawned==1, [ owner_id:7 ] x 7 -- id mod 128 owning each of
               the 7 pairs (0 = unassigned). A newly-spawned (5th..7th) builder
               matches its own id here to learn its pair.

  sym (3 bits) = [ solved:1 | type:2 ] with type 0=rot, 1=ver, 2=hor. Solved iff
               exactly one symmetry type is still possible; otherwise type is the
               core's best guess. The prediction is the core's alone.
  income (7)  = raw predicted income (see core.py); route_total() returns it.

---------------------------------------------------------------- builder word
    [ pos:POS_BITS | move:3 | (tiles if cardinal) | sym_possible:3 | enemies | hb:1@63 ]

  pos          : absolute tile index (x + y*w), ALWAYS sent -- every word is
                 self-describing, so no reader ever needs a prior baseline and a
                 lost round costs nothing.
  move         : 0-3 = stepped N/S/E/W, 4 = stayed, 5 = launched / first write.
  tiles        : only for a cardinal move -- the env of the (up to 9) tiles this
                 step newly reveals, in ascending tile order, each a prefix code
                 0=empty (1 bit) / 10=wall / 11=ore (2 bits). The reader recomputes
                 exactly which tiles from (pos, move), so the stream self-terminates.
  sym_possible : 3-bit mask of symmetries this bot still believes possible
                 (bit0 rot, bit1 ver, bit2 hor); the core folds these in.
  enemies      : each [ relpos | id:7 ]; relpos is an index into the sorted
                 in-bounds tile set vision(prev_pos) U vision(now_pos) -- 7 bits
                 for a cardinal/stay word (union <= 78), 8 bits for a launched one
                 (disjoint disks, <= 138). id = enemy id mod 128 (ids start at 1,
                 so an entry with id==0 terminates the list; the list also ends on
                 reaching the heartbeat bit).
  hb           : heartbeat at bit 63, flipped every turn (at read() time, so a
                 turn lost to the CPU cap still counts as "alive"). A pair whose
                 raw value is unchanged since last read is dead -> ignored, and
                 the core clears it.
"""

from fcode import Controller, Position, EntityType, GameConstants

import map_info

_STORE_SIZE = GameConstants.STORE_SIZE      # 16
_CORE_LO = 0
_CORE_HI = 1
_FIRST_BUILDER_PAIR_SLOT = 2
_MAX_BUILDERS = 6                            # pairs 2/3 .. 12/13
# bird2 gives slot 14 to the siege word and leaves 15 spare. The cost is one
# builder comm pair (7 -> 6), which this bot never reaches: it opens on ONE
# builder and grows only through defensive/healer spawns.
_SIEGE_SLOT = 14
_NUM_ORIGINAL = 4

# --- vision geometry (builder radius^2), precomputed once ---
_VISION_R2 = GameConstants.BUILDER_BOT_VISION_RADIUS_SQ  # 20
_RAD = int(_VISION_R2 ** 0.5)
_VISION_OFFSETS = [
    (dx, dy)
    for dy in range(-_RAD, _RAD + 1)
    for dx in range(-_RAD, _RAD + 1)
    if dx * dx + dy * dy <= _VISION_R2
]
_VIS_SET = frozenset(_VISION_OFFSETS)

# Cardinal move deltas, indexed by the move code 0..3.
_MOVE_DELTA = ((0, -1), (0, 1), (1, 0), (-1, 0))   # N, S, E, W
_MOVE_N, _MOVE_S, _MOVE_E, _MOVE_W = 0, 1, 2, 3
_MOVE_STAY = 4
_MOVE_LAUNCH = 5

# Offsets a cardinal step newly reveals: o (from the new pos) is new iff o+step
# was outside the disk (i.e. not visible from the old pos). Sorted by (dy, dx) so
# the resulting tile indices come out ascending for free.
_STEP_NEW = {
    m: sorted(
        (o for o in _VISION_OFFSETS
         if (o[0] + _MOVE_DELTA[m][0], o[1] + _MOVE_DELTA[m][1]) not in _VIS_SET),
        key=lambda o: (o[1], o[0]),
    )
    for m in range(4)
}

# Prefix code for a tile's env, LSB-first: 0 -> empty, 10 -> wall, 11 -> ore.
_WIRE_EMPTY, _WIRE_WALL, _WIRE_ORE = 0, 1, 2
_TILE_PREFIX = {_WIRE_EMPTY: (0,), _WIRE_WALL: (1, 0), _WIRE_ORE: (1, 1)}

# symmetry type codes (must match the docstring / map_info convention below)
_SYM_ROT, _SYM_VER, _SYM_HOR = 0, 1, 2

_ID_MOD = 128                               # ids stored mod 2^7
_ORIG_ID_MOD = 32                           # original 4 ids sent in 5 bits

# --- bit layout, filled in by init once the map size is known ---
_POS_BITS = 0
_POS_MASK = 0
_width = 0
_height = 0

# --- per-unit state (each bot runs its own module instance) ---
rc: Controller = None
_am_core = False
_am_builder = False
_my_id = 0
_my_pair = None            # builder: my pair index 0..6 (None until assigned)
_my_slot_lo = None
_can_comm = False          # builder: True once we own a slot
_my_hb = 0
_my_prev_pos = None        # my last reported position (for the enemy union)
_spawn_round = -1
_enemies_pre = None        # enemies snapshotted at read() (pre-move), (pos,id) list

# core producer state
_pending_core_plan = None  # DFS payload to write rounds 0..3
_orig_ids = []             # core: ids of the 4 original builders, spawn order
_pair_owner = [0] * _MAX_BUILDERS   # core: id mod 128 owning each pair (0=none)
# Persistent on EVERY unit: pair -> owning builder id, at its broadcast modulus
# (pairs 0..3 are originals sent mod 32; pairs 4.. are late builders sent mod 128).
# The core fills it as it spawns; builders learn it from the round-4 orig-id word and
# the just_spawned owner word. Used to match a relayed friendly to a local sighting.
_pair_id = [0] * _MAX_BUILDERS
_just_spawned = False      # core: a non-original builder was spawned last round
_income_raw = 0            # core: this turn's raw predicted income
_core_alarm_raw = 0        # core: 1 while the core's HP is below the distress threshold

# reader state
_income = 0                # last income read from the core word
_core_alarm = 0            # reader: last core-alarm bit read from the core word
_slot_last_raw = None      # list[int|None] per PAIR: last raw 64-bit value seen
_comm_friendly = None      # decoded: {pair -> Position}
_comm_enemies = None       # decoded: list[(Position, id_mod128)]
_dead_pairs = ()           # core: pairs found dead in this read() (zeroed at write)
_read_round = -2


def init(c: Controller):
    global rc, _width, _height, _am_core, _am_builder, _my_id
    global _POS_BITS, _POS_MASK
    global _my_pair, _my_slot_lo, _can_comm, _my_hb, _my_prev_pos, _spawn_round
    global _enemies_pre, _pending_core_plan, _orig_ids, _pair_owner, _pair_id, _just_spawned
    global _income_raw, _income, _slot_last_raw, _comm_friendly, _comm_enemies, _read_round
    global _core_alarm_raw, _core_alarm
    rc = c
    _width = map_info._width
    _height = map_info._height
    _am_core = (c.get_entity_type() == EntityType.CORE)
    _am_builder = (c.get_entity_type() == EntityType.BUILDER_BOT)
    _my_id = c.get_id()
    _POS_BITS = max(1, (_width * _height - 1).bit_length())
    _POS_MASK = (1 << _POS_BITS) - 1

    _my_pair = None
    _my_slot_lo = None
    _can_comm = False
    _my_hb = 0
    _my_prev_pos = None
    _spawn_round = c.get_current_round()      # our first turn == the round we spawned
    _enemies_pre = []

    _pending_core_plan = None
    _orig_ids = []
    _pair_owner = [0] * _MAX_BUILDERS
    _pair_id = [0] * _MAX_BUILDERS
    _just_spawned = False
    _income_raw = 0
    _core_alarm_raw = 0
    _core_alarm = 0
    _income = 0
    _slot_last_raw = [None] * _MAX_BUILDERS
    _comm_friendly = {}
    _comm_enemies = []
    _read_round = -2

    # A builder self-assigns its slot from its spawn round. VERIFIED against the
    # engine: the core spawns the 4 originals on rounds 0..3, but a builder's FIRST
    # turn (its `get_current_round()`) is the round AFTER it was spawned -- so the
    # originals first-run on rounds 1..4, and pair = spawn_round - 1 (0..3). Late
    # builders wait for the core's just_spawned id-map (see _absorb_core).
    if _am_builder and 1 <= _spawn_round <= _NUM_ORIGINAL:
        _my_pair = _spawn_round - 1
        _my_slot_lo = _FIRST_BUILDER_PAIR_SLOT + 2 * _my_pair
        _can_comm = True


# =========================================================================== #
# 64-bit slot-pair I/O
# =========================================================================== #
def _read_pair(lo_slot: int) -> int:
    return rc.read_store(lo_slot) | (rc.read_store(lo_slot + 1) << 32)


def _write_pair(lo_slot: int, val: int) -> None:
    rc.write_store(lo_slot, val & 0xFFFFFFFF)
    rc.write_store(lo_slot + 1, (val >> 32) & 0xFFFFFFFF)


def _pair_lo(pair: int) -> int:
    return _FIRST_BUILDER_PAIR_SLOT + 2 * pair


# =========================================================================== #
# vision / enemy-position helpers
# =========================================================================== #
def _vision_tiles(px: int, py: int):
    """In-bounds tile indices visible from (px,py)."""
    w, h = _width, _height
    out = []
    for ox, oy in _VISION_OFFSETS:
        x, y = px + ox, py + oy
        if 0 <= x < w and 0 <= y < h:
            out.append(x + y * w)
    return out


def _enemy_candidate_list(prev, now):
    """Sorted in-bounds tiles of vision(prev) U vision(now) -- the index space an
    enemy relpos is drawn from. `prev`/`now` are Positions (prev may be None)."""
    s = set(_vision_tiles(now.x, now.y))
    if prev is not None:
        s.update(_vision_tiles(prev.x, prev.y))
    return sorted(s)


def _local_enemies():
    """(Position, id mod 128) for every enemy builder bot in MY vision right now."""
    out = []
    for uid in rc.get_nearby_units():
        if uid == _my_id:
            continue
        if rc.get_entity_type(uid) != EntityType.BUILDER_BOT:
            continue
        if rc.get_team(uid) == rc.get_team():
            continue
        out.append((rc.get_position(uid), uid % _ID_MOD))
    return out


def _env_code(n: int) -> int:
    bit = 1 << n
    if map_info._bm_env[map_info._IDX_ENV_WALL] & bit:
        return _WIRE_WALL
    if map_info._bm_env[map_info._IDX_ENV_ORE_TI] & bit:
        return _WIRE_ORE
    return _WIRE_EMPTY


def _sym_possible_mask() -> int:
    return ((1 if map_info._rot_sym else 0) << _SYM_ROT
            | (1 if map_info._ver_sym else 0) << _SYM_VER
            | (1 if map_info._hor_sym else 0) << _SYM_HOR)


# =========================================================================== #
# builder word encode / decode  (pure; the reader passes prev via `prev_pos`)
# =========================================================================== #
def encode_builder(pos: Position, move: int, tile_codes, sym_possible: int,
                   enemies, prev_pos) -> int:
    """enemies: list of (Position, id_mod128). Returns the 64-bit word."""
    val = (pos.x + pos.y * _width) & _POS_MASK
    shift = _POS_BITS
    val |= (move & 0x7) << shift
    shift += 3
    if move < 4:                               # cardinal -> tile stream
        for code in tile_codes:
            for b in _TILE_PREFIX[code]:
                val |= b << shift
                shift += 1
    val |= (sym_possible & 0x7) << shift
    shift += 3
    # enemies
    cand = _enemy_candidate_list(prev_pos, pos)
    idx = {n: i for i, n in enumerate(cand)}
    pos_bits = 8 if move == _MOVE_LAUNCH else 7
    for epos, eid in enemies:
        en = epos.x + epos.y * _width
        if en not in idx:
            continue                           # not in the shared index space
        rel = idx[en]
        entry_bits = pos_bits + 7
        if shift + entry_bits > 63:            # no room before the heartbeat
            break
        eid = eid % _ID_MOD
        if eid == 0:
            continue                           # id 0 is the terminator, skip
        val |= (rel & ((1 << pos_bits) - 1)) << shift
        val |= (eid & 0x7F) << (shift + pos_bits)
        shift += entry_bits
    return val


def decode_builder(val: int, prev_pos):
    """Returns (pos, move, tiles[(n,env_idx)], sym_possible, enemies[(Position,id)]).
    `prev_pos` is the position this slot reported last turn (None if unknown)."""
    n = val & _POS_MASK
    pos = Position(n % _width, n // _width)
    shift = _POS_BITS
    move = (val >> shift) & 0x7
    shift += 3
    tiles = []
    if move < 4:
        for ox, oy in _STEP_NEW[move]:
            x, y = pos.x + ox, pos.y + oy
            if not (0 <= x < _width and 0 <= y < _height):
                continue
            tn = x + y * _width
            if (val >> shift) & 1:             # 1x -> wall(10)/ore(11)
                shift += 1
                code = _WIRE_WALL if not ((val >> shift) & 1) else _WIRE_ORE
                shift += 1
            else:
                shift += 1
                code = _WIRE_EMPTY
            tiles.append((tn, code))
    sym_possible = (val >> shift) & 0x7
    shift += 3
    enemies = []
    cand = _enemy_candidate_list(prev_pos, pos)
    pos_bits = 8 if move == _MOVE_LAUNCH else 7
    entry_bits = pos_bits + 7
    while shift + entry_bits <= 63:
        eid = (val >> (shift + pos_bits)) & 0x7F
        if eid == 0:
            break                              # terminator
        rel = (val >> shift) & ((1 << pos_bits) - 1)
        if rel < len(cand):
            tn = cand[rel]
            enemies.append((Position(tn % _width, tn // _width), eid))
        shift += entry_bits
    return pos, move, tiles, sym_possible, enemies


# =========================================================================== #
# core word encode / decode
# =========================================================================== #
def _encode_sym(solved: int, sym_type: int) -> int:
    return (solved & 1) | ((sym_type & 0x3) << 1)


def encode_core(rnd: int, plan_payload, sym3: int, orig_ids, income: int,
                just_spawned: int, pair_owner, alarm: int = 0,
                heal_ok: int = 0) -> int:
    if 0 <= rnd < _NUM_ORIGINAL:
        return (plan_payload or 0) & 0xFFFFFFFFFFFFFFFF
    if rnd == _NUM_ORIGINAL:                    # round 4: sym + 4 x 5-bit ids
        val = sym3 & 0x7
        shift = 3
        for i in range(_NUM_ORIGINAL):
            oid = (orig_ids[i] % _ORIG_ID_MOD) if i < len(orig_ids) else 0
            val |= (oid & 0x1F) << shift
            shift += 5
        return val
    # round >= 5
    val = sym3 & 0x7
    val |= (income & 0x7F) << 3
    val |= (just_spawned & 1) << 10
    val |= (alarm & 1) << 11                    # core distress: HP below threshold
    val |= (heal_ok & 1) << 61                  # we are winning the healing race
    if just_spawned:
        shift = 12
        for k in range(_MAX_BUILDERS):
            val |= (pair_owner[k] & 0x7F) << shift
            shift += 7
    return val


def decode_core(rnd: int, val: int):
    """Returns a dict of whatever this round's core word carries."""
    if 0 <= rnd < _NUM_ORIGINAL:
        return {"plan": val}
    out = {"sym": val & 0x7}
    if rnd == _NUM_ORIGINAL:
        ids = []
        shift = 3
        for _ in range(_NUM_ORIGINAL):
            ids.append((val >> shift) & 0x1F)
            shift += 5
        out["orig_ids"] = ids
        return out
    out["income"] = (val >> 3) & 0x7F
    js = (val >> 10) & 1
    out["just_spawned"] = js
    out["alarm"] = (val >> 11) & 1
    out["heal_ok"] = (val >> 61) & 1
    if js:
        owners = []
        shift = 12
        for _ in range(_MAX_BUILDERS):
            owners.append((val >> shift) & 0x7F)
            shift += 7
        out["pair_owner"] = owners
    return out


# =========================================================================== #
# Public producer hooks used by core.py / builder.py
# =========================================================================== #
def queue_core_plan(dfs_bits) -> None:
    """Core: queue a conveyor-plan DFS bitstream to write this round (rounds 0-3)."""
    global _pending_core_plan
    payload = 0
    for i, b in enumerate(dfs_bits):
        if i >= 64:
            break
        if b:
            payload |= 1 << i
    _pending_core_plan = payload


def core_plan_dfs_budget() -> int:
    return 64


def read_core_plan():
    """A builder's opening plan (list[int]) if the core word this round is a plan
    word (rounds 0-3), else None."""
    if not (0 <= rc.get_current_round() - 1 < _NUM_ORIGINAL):
        # plan words are written rounds 0..3, read the round after
        pass
    val = _read_pair(_CORE_LO)
    rnd = rc.get_current_round()
    # We read at the START of `rnd`, seeing the core's write from `rnd-1`.
    if not (0 <= rnd - 1 < _NUM_ORIGINAL):
        return None
    if val == 0:
        return None          # no plan this round (fanout round, or none queued)
    return [(val >> i) & 1 for i in range(64)]


def register_original(builder_id: int) -> None:
    """Core: record an original builder's id (spawn order) for the round-4 word."""
    if len(_orig_ids) < _NUM_ORIGINAL:
        _pair_id[len(_orig_ids)] = builder_id % _ORIG_ID_MOD   # pair == spawn order
        _orig_ids.append(builder_id)


def register_spawn(builder_id: int) -> int:
    """Core: assign a newly-spawned late builder the next free pair, flag it for the
    next round's just_spawned broadcast. Returns the pair index (or -1 if full)."""
    global _just_spawned
    for k in range(_NUM_ORIGINAL, _MAX_BUILDERS):
        if _pair_owner[k] == 0:
            _pair_owner[k] = builder_id % _ID_MOD
            _pair_id[k] = builder_id % _ID_MOD
            _just_spawned = True
            return k
    return -1


def set_income(raw: int) -> None:
    global _income_raw
    _income_raw = raw & 0x7F


def set_core_alarm(on: bool) -> None:
    """Core: raise/clear the distress bit broadcast in the core word (its HP fell
    below the threshold). Builders reading it drop everything to heal the core."""
    global _core_alarm_raw
    _core_alarm_raw = 1 if on else 0


def core_alarm() -> bool:
    """Reader: True if the core broadcast its distress bit (HP below threshold)."""
    return bool(_core_alarm)


def route_total() -> int:
    """Predicted income raw -- the siege gate reads this (1 route ~= 1 income)."""
    return _income_raw if _am_core else _income


def core_income() -> int:
    """The core's last-broadcast raw income value, read STRAIGHT from the store so a
    turret (which never runs a full read()) can consult it. Returns 127 on a round
    whose core word carries no income field yet (the opening rounds), so it never
    reads as low before the economy word exists."""
    val = _read_pair(_CORE_LO)
    info = decode_core(rc.get_current_round() - 1, val)   # we read rnd's start = rnd-1's write
    return info.get("income", 127)


def note_route_complete():          # retained as a no-op; route/harvest still call it
    pass


def write_alarm(*a, **k):           # sentry alarm removed (no launchers)
    pass


def read_alarm():
    return None


def ally_positions() -> list:
    """Positions of live friendly builders decoded from the store this turn."""
    return list(_comm_friendly.values()) if _comm_friendly else []


def friendly_bots() -> list:
    """(Position, id) for each live friendly builder this turn. The core knows each
    pair's id from its own spawns (originals in spawn order, later ones by pair)."""
    out = []
    for pair, pos in (_comm_friendly or {}).items():
        bid = _orig_ids[pair] if pair < len(_orig_ids) else _pair_owner[pair]
        out.append((pos, bid))
    return out


# =========================================================================== #
# read()  -- START of turn:  heartbeat, absorb every slot
# =========================================================================== #
def read():
    global _my_hb, _enemies_pre, _read_round
    _my_hb ^= 1                       # flip at turn start (survives a CPU cap)
    if _am_builder:
        _enemies_pre = _local_enemies()   # snapshot pre-move sightings
    _absorb()
    _read_round = rc.get_current_round()


def _absorb():
    global _income, _comm_friendly, _comm_enemies, _my_pair, _my_slot_lo, _can_comm
    global _dead_pairs
    rnd = rc.get_current_round()
    # --- core word ---
    _absorb_core(rnd)
    # --- builder pairs ---
    friendly = {}
    enemies = []
    dead = []
    for pair in range(_MAX_BUILDERS):
        lo = _pair_lo(pair)
        val = _read_pair(lo)
        last = _slot_last_raw[pair]
        _slot_last_raw[pair] = val
        if val == 0:
            continue                  # empty / never written
        if last is not None and val == last:
            dead.append(pair)         # unchanged since last read -> dead, ignore
            continue
        prev = _comm_friendly.get(pair) if _comm_friendly else None
        pos, move, tiles, sym_possible, ens = decode_builder(val, prev)
        friendly[pair] = pos
        for tn, code in tiles:
            map_info.record_relayed_tile(tn, _WIRE_TO_ENV(code))
        map_info.note_comm_sym_possible(sym_possible)
        for ep, eid in ens:
            enemies.append((ep, eid))
    _comm_friendly = friendly
    _comm_enemies = enemies
    _dead_pairs = dead
    # push global bot knowledge into map_info. Friendlies carry their owner id (mod
    # 128) so map_info can prefer a local sighting over the relayed (last-turn) claim.
    friendly_claims = [(pos, _pair_id[pair]) for pair, pos in friendly.items()]
    map_info.set_comm_bots(friendly_claims, enemies)


def _absorb_core(rnd: int):
    global _income, _my_pair, _my_slot_lo, _can_comm, _pair_id, _core_alarm
    val = _read_pair(_CORE_LO)
    # We read at start of `rnd`, so the core word is its write from `rnd-1`.
    wrote_round = rnd - 1
    if wrote_round < 0:
        return
    info = decode_core(wrote_round, val)
    if "sym" in info:
        map_info.set_comm_core_sym(info["sym"])
    if "income" in info:
        _income = info["income"]
    if "alarm" in info:
        _core_alarm = info["alarm"]
    # Learn pair -> owner id from the core's broadcasts (round-4 word carries the 4
    # original ids; a just_spawned word carries every late pair's owner). Kept for
    # every unit so relayed friendlies can be matched to local sightings by id.
    if "orig_ids" in info:
        for k in range(_NUM_ORIGINAL):
            _pair_id[k] = info["orig_ids"][k]        # original ids are < 32, lossless
    if "pair_owner" in info:
        owners = info["pair_owner"]
        for k in range(_NUM_ORIGINAL, _MAX_BUILDERS):
            if owners[k]:
                _pair_id[k] = owners[k]
    if info.get("just_spawned") and "pair_owner" in info and _am_builder and not _can_comm:
        owners = info["pair_owner"]
        mine = _my_id % _ID_MOD
        for k in range(_NUM_ORIGINAL, _MAX_BUILDERS):
            if owners[k] == mine:
                _my_pair = k
                _my_slot_lo = _pair_lo(k)
                _can_comm = True
                break


def _WIRE_TO_ENV(code: int) -> int:
    if code == _WIRE_WALL:
        return map_info._IDX_ENV_WALL
    if code == _WIRE_ORE:
        return map_info._IDX_ENV_ORE_TI
    return map_info._IDX_ENV_EMPTY


# =========================================================================== #
# write()  -- END of turn
# =========================================================================== #
def write():
    global _my_prev_pos
    if _am_core:
        _write_core()
    elif _am_builder and _can_comm:
        _write_builder()
    # The siege word lives in its own slot, so the rusher can publish it whether
    # or not it owns a builder pair -- it never collides with the pair stream.
    _write_siege()
    _my_prev_pos = map_info._my_pos


def _write_core():
    global _just_spawned, _pending_core_plan
    rnd = rc.get_current_round()
    sym3 = map_info.comm_core_sym3()
    plan = _pending_core_plan
    val = encode_core(rnd, plan, sym3, _orig_ids, _income_raw,
                      1 if _just_spawned else 0, _pair_owner, _core_alarm_raw,
                      _heal_ok_raw)
    _write_pair(_CORE_LO, val)
    # Consume the plan: it's for the ONE builder spawned this round. Leaving it set
    # made later fanout rounds (still < _NUM_ORIGINAL) rebroadcast this stale plan,
    # so a fanout builder that spawned without a plan of its own would read it and
    # decode garbage conveyors. Cleared -> those rounds write a plan-less 0 word.
    _pending_core_plan = None
    _just_spawned = False
    # Clean up builder pairs that read() flagged dead, so the debug view stays
    # readable (their words are already ignored either way).
    for pair in _dead_pairs:
        _write_pair(_pair_lo(pair), 0)
        _slot_last_raw[pair] = 0


def _write_builder():
    my_pos = map_info._my_pos
    prev = _my_prev_pos
    if prev is None:
        move = _MOVE_LAUNCH                      # first write: sync via absolute pos
    else:
        dx, dy = my_pos.x - prev.x, my_pos.y - prev.y
        if (dx, dy) == (0, 0):
            move = _MOVE_STAY
        elif (dx, dy) in _MOVE_DELTA:
            move = _MOVE_DELTA.index((dx, dy))
        else:
            move = _MOVE_LAUNCH
    tile_codes = []
    if move < 4:
        for ox, oy in _STEP_NEW[move]:
            x, y = my_pos.x + ox, my_pos.y + oy
            if 0 <= x < _width and 0 <= y < _height:
                tile_codes.append(_env_code(x + y * _width))
    # enemies: union of pre-move and post-move sightings (dedup by tile)
    seen = {}
    for ep, eid in _enemies_pre:
        seen[ep.x + ep.y * _width] = (ep, eid)
    for ep, eid in _local_enemies():
        seen[ep.x + ep.y * _width] = (ep, eid)
    enemies = list(seen.values())
    val = encode_builder(my_pos, move, tile_codes, _sym_possible_mask(),
                         enemies, prev)
    val |= (_my_hb & 1) << 63
    _write_pair(_my_slot_lo, val)


# =========================================================================== #
# Siege word (slot 14) -- bird2
# =========================================================================== #
# The rusher is the only unit that can see our sentinels: they stand at the
# ENEMY core, far outside our own core's vision, so the core cannot count them
# itself. The rusher publishes the live count here and the core reads it to size
# ammunition conversion.
#
# Layout: bits 0-3 sentinel count (0..15), bit 4 "siege is live".
_pending_siege = None


def set_siege_sentinels(count: int) -> None:
    """Rusher: publish how many of our sentinels are standing at the enemy core."""
    global _pending_siege
    _pending_siege = (min(15, max(0, int(count))) & 0xF) | 0x10


def _write_siege() -> None:
    if _pending_siege is not None:
        rc.write_store(_SIEGE_SLOT, _pending_siege)


def siege_sentinels() -> int:
    """Anyone: sentinels the rusher reported last round, 0 if no siege is live."""
    try:
        v = rc.read_store(_SIEGE_SLOT)
    except Exception:
        return 0
    return (v & 0xF) if (v & 0x10) else 0


def siege_active() -> bool:
    try:
        return bool(rc.read_store(_SIEGE_SLOT) & 0x10)
    except Exception:
        return False


def am_rusher() -> bool:
    """True for the round-0 builder -- the one running the sentinel rush. Lives
    here rather than in rush_attack so map_info can ask without importing a
    builder state (which would import map_info straight back)."""
    return _am_builder and _my_pair == 0


# --- healing-race bit (core word, bit 61) ------------------------------------
_heal_ok_raw = 0


def set_heal_ok(on: bool) -> None:
    """Core: broadcast that our core is NOT losing HP -- the healers are keeping
    up with whatever is shooting it."""
    global _heal_ok_raw
    _heal_ok_raw = 1 if on else 0


def heal_race_won() -> bool:
    """Any unit: are we out-healing the damage on our core right now?

    Read straight from the store (like core_income) so a turret, which never runs
    a full read(), can consult it. Bit 61 sits above the pair_owner field, so it
    survives a just_spawned round.
    """
    try:
        val = _read_pair(_CORE_LO)
        rnd = rc.get_current_round() - 1
        if not (0 <= rnd < _NUM_ORIGINAL) and rnd != _NUM_ORIGINAL:
            return bool((val >> 61) & 1)
    except Exception:
        pass
    return False
