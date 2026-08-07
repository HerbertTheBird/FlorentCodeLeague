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
    m = sub.add_parser("map", help="terrain of the map a replay was played on")
    m.add_argument("file")
    m.set_defaults(func=cmd_map)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
