#!/usr/bin/env python3
"""Decode .replay26 files and summarise what actually happened in a game.

The visualiser in tools/visualiser2d is a browser app, which is fine for
watching one game and useless for asking "why do we lose on antler" across
twenty of them. This reads the wire format directly so games can be diffed,
aggregated, and grepped.

The format is protobuf without a published schema, so the field numbers below
were recovered by inspection. Only the parts needed for post-mortems are
decoded; everything else is skipped rather than guessed at.

    top level        f1 = map (width, height, terrain rows), f3 = repeated turn
    turn             f1 = repeated event
    event f1         spawn   {entity: {f1 id, f2 team, f3 pos, f4 hp, f5 maxhp}}
    event f2         move    {f1 id, f2 pos}
    event f3         death   {f1 id}
    event f5         damage  {f1 id, f2 signed delta}
    event f6         economy {f1: {f1 teamA titanium}, f2: {f1 teamB titanium}}

Team is absent on team A's spawns (protobuf omits zero), so a missing f2 means
team A.

Usage:
    python3 tools/replay.py summary FILE...        one line per game
    python3 tools/replay.py curve FILE [--every N] titanium/units/core HP curve
    python3 tools/replay.py map FILE               terrain, ore, and core sites
"""

from __future__ import annotations

import argparse
import collections
from pathlib import Path
import sys


# --- wire format primitives -------------------------------------------------
def _varint(b: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = b[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def fields(b: bytes) -> list[tuple[int, int, object]]:
    """Yield (field_number, wire_type, value) for one protobuf message."""
    out = []
    i = 0
    while i < len(b):
        try:
            key, i = _varint(b, i)
        except IndexError:
            break
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
            out.append((num, 0, v))
        elif wire == 2:
            ln, i = _varint(b, i)
            out.append((num, 2, b[i:i + ln]))
            i += ln
        elif wire == 5:
            out.append((num, 5, b[i:i + 4]))
            i += 4
        elif wire == 1:
            out.append((num, 1, b[i:i + 8]))
            i += 8
        else:
            break
    return out


def get(msg, num: int, default=None):
    if not isinstance(msg, (bytes, bytearray)):
        return default
    for f, _w, v in fields(msg):
        if f == num:
            return v
    return default


def sub(msg, *nums):
    """Follow a chain of nested length-delimited fields, or None."""
    cur = msg
    for n in nums:
        cur = get(cur, n)
        if not isinstance(cur, (bytes, bytearray)):
            return None
    return cur


def _signed(v: int) -> int:
    """Protobuf stores negative varints as two's complement in 64 bits."""
    return v - (1 << 64) if v >= (1 << 63) else v


def _pos(msg: bytes | None) -> tuple[int, int] | None:
    if msg is None:
        return None
    return (get(msg, 1, 0), get(msg, 2, 0))


# --- game model -------------------------------------------------------------
CORE_MAX_HP = 500

# A spawn record carries no type field. It does carry exactly one payload field
# per entity kind, whose *number* identifies the kind -- recovered by
# cross-referencing max HP against GameConstants and, for the three kinds that
# share 30 HP, by whether the tile is ore (harvester) and which side built it.
#
# Note the real constants are not the ones in CAMBRIDGE_SPECS.md: a gunner is 25 HP
# and a sentinel 40, the reverse of what the Cambridge docs say.
PAYLOAD_KIND = {
    10: "builder",     # 40 hp, the only mobile kind
    11: "conveyor",    # 20
    15: "harvester",   # 30, always on ore
    18: "barrier",     # 30
    21: "gunner",      # 25
    22: "sentinel",    # 40
    24: "launcher",    # 30
}


class Game:
    def __init__(self, path: Path):
        self.path = path
        raw = path.read_bytes()
        top = fields(raw)
        header = next((v for f, _w, v in top if f == 1), b"")
        self.width = get(header, 1, 0)
        self.height = get(header, 2, 0)
        self.rows = [list(get(r, 1) or b"") for f, _w, r in fields(header) if f == 3]
        self.turns = [v for f, _w, v in top if f == 3]

    # --- per-turn walk ------------------------------------------------------
    def replay(self):
        """Yield a dict of state after each turn.

        Tracks only what the post-mortems need: titanium per team, live unit
        count per team, and core HP per team.
        """
        hp: dict[int, int] = {}
        team: dict[int, int] = {}
        maxhp: dict[int, int] = {}
        cores: dict[int, int] = {}          # team -> entity id
        ti = [0, 0]
        for n, turn in enumerate(self.turns):
            for ef, _w, ev in fields(turn):
                if ef != 1:
                    continue
                for kind, _kw, body in fields(ev):
                    if kind == 1:                       # spawn
                        ent = sub(body, 1)
                        if ent is None:
                            continue
                        eid = get(ent, 1)
                        if eid is None:
                            continue
                        team[eid] = get(ent, 2, 0)
                        hp[eid] = get(ent, 4, 0)
                        maxhp[eid] = get(ent, 5, 0)
                        if maxhp[eid] == CORE_MAX_HP:
                            cores[team[eid]] = eid
                    elif kind == 3:                     # death
                        eid = get(body, 1)
                        hp.pop(eid, None)
                    elif kind == 5:                     # damage / heal
                        eid = get(body, 1)
                        if eid in hp:
                            hp[eid] += _signed(get(body, 2, 0))
                    elif kind == 6:                     # economy snapshot
                        a = sub(body, 1, 1)
                        b = sub(body, 1, 2)
                        if a is not None:
                            ti[0] = get(a, 1, ti[0])
                        if b is not None:
                            ti[1] = get(b, 1, ti[1])
            units = [0, 0]
            for eid in hp:
                t = team.get(eid, 0)
                if 0 <= t < 2:
                    units[t] += 1
            yield {
                "turn": n,
                "titanium": list(ti),
                "units": units,
                "core_hp": [hp.get(cores.get(0, -1), 0), hp.get(cores.get(1, -1), 0)],
            }

    def idle_streaks(self, team: int, min_run: int = 30):
        """Longest run of consecutive turns each mobile unit spent not moving.

        Builder bots are the only mobile unit, and they are identified by having
        emitted at least one move event rather than by type (the spawn record
        does not carry one). A long idle run means a bot that is alive, costing
        us a unit slot and its share of builder cost scaling, and doing nothing
        that shows up on the board.

        Returns [(entity id, longest run, first turn of that run, last position)]
        for units whose longest run reaches `min_run`, worst first.
        """
        pos: dict[int, tuple[int, int]] = {}
        owner: dict[int, int] = {}
        moved: set[int] = set()
        alive: dict[int, tuple[int, int]] = {}    # id -> (spawn turn, death turn)
        last_move: dict[int, int] = {}
        best: dict[int, tuple[int, int]] = {}     # id -> (run, start turn)
        for n, turn in enumerate(self.turns):
            for ef, _w, ev in fields(turn):
                if ef != 1:
                    continue
                for kind, _kw, body in fields(ev):
                    if kind == 1:
                        ent = sub(body, 1)
                        if ent is None:
                            continue
                        eid = get(ent, 1)
                        if eid is None:
                            continue
                        owner[eid] = get(ent, 2, 0)
                        pos[eid] = _pos(get(ent, 3)) or (0, 0)
                        alive[eid] = (n, None)
                        last_move[eid] = n
                    elif kind == 2:
                        eid = get(body, 1)
                        if eid is None:
                            continue
                        moved.add(eid)
                        run = n - last_move.get(eid, n)
                        if run > best.get(eid, (0, 0))[0]:
                            best[eid] = (run, last_move.get(eid, n))
                        last_move[eid] = n
                        p2 = _pos(get(body, 2))
                        if p2:
                            pos[eid] = p2
                    elif kind == 3:
                        eid = get(body, 1)
                        if eid in alive:
                            alive[eid] = (alive[eid][0], n)
        end = len(self.turns)
        for eid in moved:
            death = alive.get(eid, (0, None))[1] or end
            run = death - last_move.get(eid, death)
            if run > best.get(eid, (0, 0))[0]:
                best[eid] = (run, last_move.get(eid, death))
        out = [(eid, r, start, pos.get(eid))
               for eid, (r, start) in best.items()
               if r >= min_run and owner.get(eid, 0) == team]
        out.sort(key=lambda x: -x[1])
        return out

    def composition(self):
        """[team][kind] -> how many distinct entities of that kind a team built.

        Deduplicated by entity id: the replay re-emits a spawn record for an
        entity on later turns, so counting events inflates the totals several
        times over -- badly enough to invent a 10x gunner gap that does not
        exist.
        """
        out = [collections.Counter(), collections.Counter()]
        seen = set()
        for turn in self.turns:
            for ef, _w, ev in fields(turn):
                if ef != 1:
                    continue
                for kind, _kw, body in fields(ev):
                    if kind != 1:
                        continue
                    ent = sub(body, 1)
                    if ent is None:
                        continue
                    payload = [f for f, _w2, _v in fields(ent) if f >= 6]
                    name = PAYLOAD_KIND.get(payload[0] if payload else -1)
                    if name is None:
                        continue
                    eid = get(ent, 1)
                    if eid in seen:
                        continue
                    seen.add(eid)
                    team = get(ent, 2, 0)
                    if 0 <= team < 2:
                        out[team][name] += 1
        return out

    def final(self) -> dict:
        state = None
        for state in self.replay():
            pass
        return state or {}


# --- commands ---------------------------------------------------------------
def cmd_summary(args) -> int:
    print(f"{'file':46s} {'turns':>5s} {'ti A':>7s} {'ti B':>7s} "
          f"{'unitA':>5s} {'unitB':>5s} {'hpA':>4s} {'hpB':>4s}")
    for path in args.files:
        g = Game(Path(path))
        s = g.final()
        if not s:
            print(f"{Path(path).name[:46]:46s}  (no turns)")
            continue
        print(f"{Path(path).name[:46]:46s} {s['turn']:5d} "
              f"{s['titanium'][0]:7d} {s['titanium'][1]:7d} "
              f"{s['units'][0]:5d} {s['units'][1]:5d} "
              f"{s['core_hp'][0]:4d} {s['core_hp'][1]:4d}")
    return 0


def cmd_curve(args) -> int:
    g = Game(Path(args.file))
    print(f"{Path(args.file).name}  {g.width}x{g.height}")
    print(f"{'turn':>5s} {'tiA':>6s} {'tiB':>6s} {'uA':>3s} {'uB':>3s} "
          f"{'hpA':>4s} {'hpB':>4s}")
    for s in g.replay():
        if s["turn"] % args.every:
            continue
        print(f"{s['turn']:5d} {s['titanium'][0]:6d} {s['titanium'][1]:6d} "
              f"{s['units'][0]:3d} {s['units'][1]:3d} "
              f"{s['core_hp'][0]:4d} {s['core_hp'][1]:4d}")
    return 0


def cmd_stuck(args) -> int:
    for path in args.files:
        g = Game(Path(path))
        rows = g.idle_streaks(args.team, args.min_run)
        total = len(g.turns)
        frozen = sum(r[1] for r in rows)
        print(f"{Path(path).name[:52]:52s} turns={total:4d} "
              f"stuck_units={len(rows):2d} frozen_unit_turns={frozen}")
        for eid, run, start, at in rows[:8]:
            print(f"    id={eid:4d} idle {run:4d} turns from t{start:4d} at {at}")
    return 0


KINDS = ["builder", "conveyor", "harvester", "barrier", "gunner", "sentinel", "launcher"]


def cmd_compose(args) -> int:
    head = " ".join(f"{k[:5]:>5s}" for k in KINDS)
    print(f"{'file':40s} {'side':>4s} {head}")
    tot = [collections.Counter(), collections.Counter()]
    for path in args.files:
        g = Game(Path(path))
        comp = g.composition()
        for team in (0, 1):
            tot[team].update(comp[team])
            row = " ".join(f"{comp[team][k]:5d}" for k in KINDS)
            print(f"{Path(path).name[:40]:40s} {'AB'[team]:>4s} {row}")
    print()
    for team in (0, 1):
        row = " ".join(f"{tot[team][k]:5d}" for k in KINDS)
        print(f"{'TOTAL':40s} {'AB'[team]:>4s} {row}")
    return 0


def cmd_map(args) -> int:
    g = Game(Path(args.file))
    glyph = {0: ".", 1: "#", 2: "o"}
    print(f"{g.width}x{g.height}")
    for row in g.rows:
        print("  " + "".join(glyph.get(v, "?") for v in row))
    ore = sum(r.count(2) for r in g.rows)
    wall = sum(r.count(1) for r in g.rows)
    print(f"  ore={ore} wall={wall} open={g.width * g.height - wall}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary", help="one line per replay")
    s.add_argument("files", nargs="+")
    s.set_defaults(func=cmd_summary)
    c = sub.add_parser("curve", help="per-turn curve for one replay")
    c.add_argument("file")
    c.add_argument("--every", type=int, default=50)
    c.set_defaults(func=cmd_curve)
    k = sub.add_parser("stuck", help="mobile units that stopped moving for long runs")
    k.add_argument("files", nargs="+")
    k.add_argument("--team", type=int, default=1, help="0 = player A, 1 = player B")
    k.add_argument("--min-run", type=int, default=30)
    k.set_defaults(func=cmd_stuck)
    c2 = sub.add_parser("compose", help="what each side built, by entity kind")
    c2.add_argument("files", nargs="+")
    c2.set_defaults(func=cmd_compose)
    m = sub.add_parser("map", help="terrain of the map a replay was played on")
    m.add_argument("file")
    m.set_defaults(func=cmd_map)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
