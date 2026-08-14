"""Record a sandbox game and serialise it to the real `.replay26` wire format,
so sandbox sessions (including your free edits) can be watched in the 2D viewer
or analysed with tools/replay.py.

Wire format (recovered from a real replay -- see tools/replay.py):
  top    f1 = map {f1 w, f2 h, f3* row{f1: tile bytes}, f4* core{f1 team_id,
             f2 team_idx?, f3 pos}} ; f3* = turn
  turn   f1* = event
  event  f1 spawn{f1 entity}, f2 move{f1 id,f2 pos}, f3 death{f1 id},
             f5 damage{f1 id,f2 signed}, f6 econ{f1{f1 tiA},f2{f1 tiB}}
  entity f1 id, f2 team?(omit 0), f3 pos{f1 x,f2 y}, f4 hp, f5 maxhp,
             f<TYPE> payload  (TYPE field number = kind; directed kinds carry
             {f1: dir}, harvester {f2:1}, others {})
We record turns by diffing engine snapshots each round.
"""
from fcode_shim import Direction, Environment, EntityType, Team

# entity kind -> protobuf field number inside the entity message
KIND_FIELD = {
    EntityType.BUILDER_BOT: 10, EntityType.CONVEYOR: 11, EntityType.SPLITTER: 12,
    EntityType.HARVESTER: 15, EntityType.BARRIER: 18, EntityType.GUNNER: 21,
    EntityType.SENTINEL: 22, EntityType.LAUNCHER: 24,
}
DIRECTED = {EntityType.CONVEYOR, EntityType.SPLITTER, EntityType.GUNNER, EntityType.SENTINEL}
# 1-indexed compass (N=1 .. NW=8), CENTRE=0 -- matches the engine's replay (EAST=3)
DIR_WIRE = {Direction.NORTH: 1, Direction.NORTHEAST: 2, Direction.EAST: 3,
            Direction.SOUTHEAST: 4, Direction.SOUTH: 5, Direction.SOUTHWEST: 6,
            Direction.WEST: 7, Direction.NORTHWEST: 8, Direction.CENTRE: 0}
ENV_WIRE = {Environment.EMPTY: 0, Environment.WALL: 1, Environment.ORE_TITANIUM: 2}
MASK64 = (1 << 64) - 1


# ---- protobuf primitives ----
def _v(n):
    out = bytearray()
    n &= MASK64
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _tag(f, w):
    return _v((f << 3) | w)


def _vfield(f, val):
    return _tag(f, 0) + _v(val)


def _lfield(f, payload):
    return _tag(f, 2) + _v(len(payload)) + payload


def _pos(x, y):
    return _vfield(1, x) + _vfield(2, y)


class Recorder:
    def __init__(self, engine):
        self.e = engine
        self.turns = []                 # list of serialized turn payloads
        self.prev = self._snapshot()    # initial state (cores only)

    def _snapshot(self):
        s = {}
        for en in self.e.entities.values():
            s[en.id] = (0 if en.team == Team.A else 1, en.x, en.y, en.hp,
                        en.max_hp, en.type, en.direction)
        return s

    def record(self):
        """Emit one turn = the diff since the previous snapshot."""
        cur = self._snapshot()
        ev = bytearray()
        # spawns (new ids) + moves/damage (changed)
        for eid, c in cur.items():
            p = self.prev.get(eid)
            if p is None:
                ev += _lfield(1, self._entity(eid, c))          # spawn
            else:
                if (c[1], c[2]) != (p[1], p[2]):
                    ev += _lfield(2, _vfield(1, eid) + _lfield(3, _pos(c[1], c[2])))  # move
                if c[3] != p[3]:
                    ev += _lfield(5, _vfield(1, eid) + _vfield(2, c[3] - p[3]))       # damage/heal
        for eid in self.prev:                                    # deaths
            if eid not in cur:
                ev += _lfield(3, _vfield(1, eid))
        # economy snapshot: f6{ f1{ f1{f1:tiA}, f2{f1:tiB} } }
        ti_a = self.e.teams[Team.A].titanium
        ti_b = self.e.teams[Team.B].titanium
        inner = _lfield(1, _vfield(1, ti_a)) + _lfield(2, _vfield(1, ti_b))
        ev += _lfield(6, _lfield(1, inner))
        self.turns.append(_lfield(1, bytes(ev)))                 # wrap: turn.f1 = events
        self.prev = cur

    def _entity(self, eid, c):
        team, x, y, hp, maxhp, etype, direction = c
        b = _vfield(1, eid)
        if team:
            b += _vfield(2, team)
        b += _lfield(3, _pos(x, y)) + _vfield(4, hp) + _vfield(5, maxhp)
        fn = KIND_FIELD.get(etype)
        if fn is not None:
            if etype in DIRECTED:
                payload = _vfield(1, DIR_WIRE.get(direction, 0))
            elif etype == EntityType.HARVESTER:
                payload = _vfield(2, 1)
            else:
                payload = b""
            b += _lfield(fn, payload)
        return _lfield(1, b)                                      # entity wrapped as spawn.f1

    def serialize(self):
        e = self.e
        # map header
        rows = b"".join(
            _lfield(3, _lfield(1, bytes(ENV_WIRE.get(e.terrain[y][x], 0)
                                        for x in range(e.width))))
            for y in range(e.height))
        cores = b""
        for team, cid in e.core_id.items():
            en = e.entities.get(cid)
            if en is None:
                continue
            tid = 1 if team == Team.A else 2
            body = _vfield(1, tid)
            if team == Team.B:
                body += _vfield(2, 1)
            body += _lfield(3, _pos(en.x, en.y))
            cores += _lfield(4, body)
        header = _lfield(1, _vfield(1, e.width) + _vfield(2, e.height) + rows + cores)
        turns = b"".join(_lfield(3, t) for t in self.turns)
        return header + turns

    def save(self, path):
        with open(path, "wb") as f:
            f.write(self.serialize())
        return path
