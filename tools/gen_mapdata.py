#!/usr/bin/env python3
"""Bake maps/*.map26 into a bot-side lookup table (`mapdata.py`).

Every map in the competition pool is a fixed, published file, so a bot does not
have to discover the terrain -- it can recognise which map it is on and read the
walls and ore straight out of a table. This generator is the offline half of
that: it decodes the .map26 protobuf and emits one Python module the bot imports.

The runtime half is `hardcode.py` inside the bot, which does the recognising and
-- crucially -- keeps checking. Nothing here is trusted blindly: the bot only
adopts a table entry while every tile it has actually looked at agrees with it,
and falls back to ordinary exploration the moment one does not. That is what
makes it safe to ship a table for a pool that can rotate under us.

Identification key is (width, height) -> candidates, narrowed by the position of
our own core. Across all 33 local maps that pair is already unique; the terrain
check is the backstop, not the primary discriminator.

    python3 tools/gen_mapdata.py [--out PATH] [--maps DIR]
"""

from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

EMPTY, WALL, ORE = 0, 1, 2


# --- .map26 wire format -----------------------------------------------------
# Protobuf without a published schema; field numbers recovered by inspection and
# cross-checked against a probe bot run in the real engine (see git history):
#   f1 width, f2 height, f3 repeated row {f1: tiles},
#   f4 repeated core {f1 id, f2 team (absent == 0 == A), f3 pos {f1 x, f2 y}}
# Row tiles are terrain codes; rows[y][x] matches the engine's get_tile_env.
#
# A row's tiles come in two encodings and both are still in the pool. The maps
# shipped up to the August sync pack a row as a single length-delimited blob,
# one byte per tile; the ten maps added by that sync write the same row as a
# repeated varint field instead (a proto3 writer that stopped packing, most
# likely). They are distinguishable by wire type alone, so both are accepted --
# an old map read under only the new rule yields zero rows and the dimension
# check below fires, which is exactly how this was found.
def _varint(b: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = b[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def _fields(b: bytes) -> list[tuple[int, int, object]]:
    out = []
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.append((fn, wt, v))
    return out


def parse_map(path: Path) -> dict:
    b = path.read_bytes()
    w = h = None
    rows: list[list[int]] = []
    cores: dict[int, tuple[int, int]] = {}
    for fn, wt, v in _fields(b):
        if fn == 1 and wt == 0:
            w = v
        elif fn == 2 and wt == 0:
            h = v
        elif fn == 3 and wt == 2:
            row: list[int] = []
            for sfn, swt, sv in _fields(v):
                if sfn != 1:
                    continue
                if swt == 2:      # blob encoding: one byte per tile
                    row.extend(sv)
                elif swt == 0:    # varint encoding: one field per tile
                    row.append(sv)
            rows.append(row)
        elif fn == 4 and wt == 2:
            team = 0
            pos = None
            for sfn, swt, sv in _fields(v):
                if sfn == 2:
                    team = sv
                elif sfn == 3:
                    px = py = 0
                    for tfn, _twt, tv in _fields(sv):
                        if tfn == 1:
                            px = tv
                        elif tfn == 2:
                            py = tv
                    pos = (px, py)
            if pos is not None:
                cores[team] = pos
    if w is None or h is None:
        raise ValueError(f"{path}: missing dimensions")
    if len(rows) != h or any(len(r) != w for r in rows):
        raise ValueError(f"{path}: terrain is {len(rows)} rows, expected {h}x{w}")
    if 0 not in cores or 1 not in cores:
        raise ValueError(f"{path}: expected one core per team, got {cores}")
    return {"name": path.stem, "w": w, "h": h, "rows": rows,
            "core_a": cores[0], "core_b": cores[1]}


# --- derived facts ----------------------------------------------------------
def masks(m: dict) -> tuple[int, int]:
    """(wall, ore) bitmasks over bit index x + y*w."""
    w, rows = m["w"], m["rows"]
    wall = ore = 0
    for y, row in enumerate(rows):
        base = y * w
        for x, code in enumerate(row):
            if code == WALL:
                wall |= 1 << (base + x)
            elif code == ORE:
                ore |= 1 << (base + x)
    return wall, ore


def symmetry(m: dict) -> str:
    """Which single flip both preserves the terrain and swaps the two cores.

    map_info's `flip()` tries hor, then ver, then rot, so the bot is told exactly
    one of them; a map invariant under several still only needs one that is
    right. Returns '' when none holds, which means the map is not a symmetric
    pair and the table entry must not claim a symmetry.
    """
    w, h, rows = m["w"], m["h"], m["rows"]
    ax, ay = m["core_a"]
    bx, by = m["core_b"]
    # Core positions are the 2x2's top-left corner, so a flipped corner steps
    # back one on each flipped axis (map_info.{hor,ver,rot}_flip_core).
    checks = (
        ("h", lambda x, y: (w - 1 - x, y), (w - 2 - ax, ay)),
        ("v", lambda x, y: (x, h - 1 - y), (ax, h - 2 - ay)),
        ("r", lambda x, y: (w - 1 - x, h - 1 - y), (w - 2 - ax, h - 2 - ay)),
    )
    for tag, flip, flipped_core in checks:
        if flipped_core != (bx, by):
            continue
        if all(rows[y][x] == rows[flip(x, y)[1]][flip(x, y)[0]]
               for y in range(h) for x in range(w)):
            return tag
    return ""


def core_tiles(core: tuple[int, int]) -> list[tuple[int, int]]:
    cx, cy = core
    return [(cx, cy), (cx + 1, cy), (cx, cy + 1), (cx + 1, cy + 1)]


def passable_grid(m: dict) -> list[list[bool]]:
    """Tiles a builder bot could stand on ignoring buildings: anything not wall.

    Core footprints are excluded -- they are permanently occupied, and a distance
    field that walks through them would understate every route that has to go
    around a core.
    """
    w, h, rows = m["w"], m["h"], m["rows"]
    blocked = set(core_tiles(m["core_a"])) | set(core_tiles(m["core_b"]))
    return [[rows[y][x] != WALL and (x, y) not in blocked
             for x in range(w)] for y in range(h)]


def bfs_from(m: dict, sources: list[tuple[int, int]]) -> list[list[int]]:
    """Cardinal-step distance field over passable tiles; -1 where unreachable.

    Sources are seeded at distance 0 even when they are themselves impassable
    (a core footprint is), so "distance from the core" means steps from its edge.
    """
    w, h = m["w"], m["h"]
    ok = passable_grid(m)
    dist = [[-1] * w for _ in range(h)]
    q: deque[tuple[int, int]] = deque()
    for x, y in sources:
        if 0 <= x < w and 0 <= y < h and dist[y][x] < 0:
            dist[y][x] = 0
            q.append((x, y))
    while q:
        x, y = q.popleft()
        d = dist[y][x] + 1
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and dist[ny][nx] < 0 and ok[ny][nx]:
                dist[ny][nx] = d
                q.append((nx, ny))
    return dist


def ore_report(m: dict) -> dict:
    """Per-side ore accounting: how much ore each core owns, and how far away.

    `contested` is ore that both sides reach in a similar number of steps; it is
    the ore that decides games, and how much of it there is separates a map you
    can farm quietly from one where the economy is a fight.
    """
    w, h, rows = m["w"], m["h"], m["rows"]
    da = bfs_from(m, core_tiles(m["core_a"]))
    db = bfs_from(m, core_tiles(m["core_b"]))
    mine = theirs = contested = unreachable = 0
    near_a = []
    for y in range(h):
        for x in range(w):
            if rows[y][x] != ORE:
                continue
            a, b = da[y][x], db[y][x]
            if a < 0 and b < 0:
                unreachable += 1
                continue
            if a >= 0:
                near_a.append(a)
            if a < 0:
                theirs += 1
            elif b < 0:
                mine += 1
            elif abs(a - b) <= 2:
                contested += 1
            elif a < b:
                mine += 1
            else:
                theirs += 1
    near_a.sort()
    return {"a": mine, "b": theirs, "contested": contested,
            "unreachable": unreachable,
            "nearest": near_a[:6]}


def core_distance(m: dict) -> int:
    """Cardinal steps between the two core footprints, -1 if disconnected.

    Measured to the tiles *around* the enemy core, not the footprint itself:
    `passable_grid` treats both footprints as blocked (they are), so the
    footprint tiles never get a distance and reading them would always say -1.
    """
    w, h = m["w"], m["h"]
    da = bfs_from(m, core_tiles(m["core_a"]))
    best = -1
    for cx, cy in core_tiles(m["core_b"]):
        for x, y in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            if not (0 <= x < w and 0 <= y < h):
                continue
            d = da[y][x]
            if d >= 0 and (best < 0 or d + 1 < best):
                best = d + 1
    return best


def chokepoints(m: dict) -> list[tuple[int, int]]:
    """Tiles that EVERY route from one core to the other has to cross.

    Found by deletion: block one passable tile, re-run the flood, and if the
    enemy core is no longer reachable then nothing can get past that tile
    without going through it. That is a cut vertex of the core-to-core
    connectivity, which is exactly what a turret wants to be looking at -- a
    gunner covering one of these covers the whole map's traffic, and a barrier
    on one is worth a dozen anywhere else.

    O(passable * board) per map, which is milliseconds offline and free at
    runtime. Returned in order of distance from core A, i.e. nearest-to-us
    first from A's point of view; B reads the same list reversed in spirit
    (the set is symmetric, the ordering is not).
    """
    w, h = m["w"], m["h"]
    ok = passable_grid(m)
    src = core_tiles(m["core_a"])
    goal = set(core_tiles(m["core_b"]))

    def reaches(blocked: tuple[int, int] | None) -> bool:
        dist = [[False] * w for _ in range(h)]
        q: deque[tuple[int, int]] = deque()
        for x, y in src:
            if (x, y) != blocked:
                dist[y][x] = True
                q.append((x, y))
        while q:
            x, y = q.popleft()
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if not (0 <= nx < w and 0 <= ny < h) or dist[ny][nx]:
                    continue
                if (nx, ny) in goal:
                    return True
                if ok[ny][nx] and (nx, ny) != blocked:
                    dist[ny][nx] = True
                    q.append((nx, ny))
        return False

    if not reaches(None):
        return []
    da = bfs_from(m, src)
    out = []
    for y in range(h):
        for x in range(w):
            if not ok[y][x] or da[y][x] < 0:
                continue
            if not reaches((x, y)):
                out.append((x, y))
    out.sort(key=lambda p: da[p[1]][p[0]])
    return out


def min_cut(m: dict) -> tuple[int, list[tuple[int, int]]]:
    """Narrowest passage between the two cores: (width, the tiles forming it).

    `chokepoints` asks whether any single tile is load-bearing; on a competition
    map the answer is always no. The useful question is the next one -- how many
    tiles you would have to hold to seal the map -- and that is a minimum VERTEX
    cut, so it is a max-flow with each tile split into in/out at capacity 1.

    The number is a map's whole defensive character in one integer: a width-4
    corridor can be walled off by four barriers and a width-40 plain cannot be
    defended at all, only out-fought. The tiles are where a barrier or a turret
    is worth the most.

    Boards top out around 700 tiles, so plain BFS augmentation is fast enough
    offline and the result is a constant at runtime.
    """
    w, h = m["w"], m["h"]
    ok = passable_grid(m)
    src_tiles = core_tiles(m["core_a"])
    dst_tiles = set(core_tiles(m["core_b"]))

    # Node ids: tile n splits into IN=2n and OUT=2n+1. Core footprints are not
    # passable, so they get no split and act as pure source/sink terminals.
    N = w * h
    S, T = 2 * N, 2 * N + 1
    cap: dict[int, dict[int, int]] = {}

    def edge(u: int, v: int, c: int) -> None:
        cap.setdefault(u, {})[v] = cap.setdefault(u, {}).get(v, 0) + c
        cap.setdefault(v, {}).setdefault(u, 0)

    for y in range(h):
        for x in range(w):
            if not ok[y][x]:
                continue
            n = x + y * w
            edge(2 * n, 2 * n + 1, 1)
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                if (nx, ny) in dst_tiles:
                    edge(2 * n + 1, T, 10 ** 6)
                elif ok[ny][nx]:
                    edge(2 * n + 1, 2 * (nx + ny * w), 10 ** 6)
    for cx, cy in src_tiles:
        for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            if 0 <= nx < w and 0 <= ny < h and ok[ny][nx]:
                edge(S, 2 * (nx + ny * w), 10 ** 6)

    flow = 0
    while True:
        parent = {S: None}
        q = deque([S])
        while q and T not in parent:
            u = q.popleft()
            for v, c in cap.get(u, {}).items():
                if c > 0 and v not in parent:
                    parent[v] = u
                    q.append(v)
        if T not in parent:
            break
        push, v = 10 ** 9, T
        while parent[v] is not None:
            push = min(push, cap[parent[v]][v])
            v = parent[v]
        v = T
        while parent[v] is not None:
            u = parent[v]
            cap[u][v] -= push
            cap[v][u] = cap[v].get(u, 0) + push
            v = u
        flow += push

    # Cut tiles = split edges crossing the residual reachable/unreachable line.
    reach = {S}
    q = deque([S])
    while q:
        u = q.popleft()
        for v, c in cap.get(u, {}).items():
            if c > 0 and v not in reach:
                reach.add(v)
                q.append(v)
    tiles = [(n % w, n // w) for n in range(N)
             if 2 * n in reach and 2 * n + 1 not in reach]
    da = bfs_from(m, src_tiles)
    tiles.sort(key=lambda p: da[p[1]][p[0]])
    return flow, tiles


def voronoi(m: dict) -> tuple[list[list[int]], list[list[int]]]:
    """(dist-from-A, dist-from-B) fields, the basis of "whose half is this"."""
    return bfs_from(m, core_tiles(m["core_a"])), bfs_from(m, core_tiles(m["core_b"]))


def frontier_tiles(m: dict) -> list[tuple[int, int]]:
    """Tiles where the two cores' reach meets -- the natural defensive line."""
    w, h = m["w"], m["h"]
    da, db = voronoi(m)
    return [(x, y) for y in range(h) for x in range(w)
            if da[y][x] >= 0 and db[y][x] >= 0 and abs(da[y][x] - db[y][x]) <= 1]


def ore_order(m: dict, side: str) -> list[tuple[int, int, int, int]]:
    """Ore we should take, nearest first: (x, y, our steps, their steps).

    Restricted to ore we reach no later than the enemy does -- ore on their side
    of the line is not economy, it is a fight, and the opening should not plan
    around winning one.
    """
    w, h, rows = m["w"], m["h"], m["rows"]
    da, db = voronoi(m)
    mine, theirs = (da, db) if side == "a" else (db, da)
    out = [(x, y, mine[y][x], theirs[y][x])
           for y in range(h) for x in range(w)
           if rows[y][x] == ORE and mine[y][x] >= 0
           and (theirs[y][x] < 0 or mine[y][x] <= theirs[y][x])]
    out.sort(key=lambda t: (t[2], -t[3]))
    return out


# --- emit -------------------------------------------------------------------
HEADER = '''"""Baked terrain for every known map. GENERATED by tools/gen_mapdata.py.

Do not edit by hand; re-run the generator after `fcode maps sync`.

Layout is chosen so that startup costs almost nothing. `MAPS` is keyed by
(width, height) and the values are plain tuples of *strings* -- the wall and ore
bitmasks stay as hex text until a candidate actually matches, so a bot on a
30x30 map never pays to parse the 10x10 entries. `hardcode.py` owns all of that.

Per entry:
    (name, core_a, core_b, sym, wall_hex, ore_hex, facts)
      core_*   top-left corner of that team's 2x2 core (team A first)
      sym      'h' | 'v' | 'r': the single flip that both preserves the terrain
               and maps core A onto core B, matching map_info.flip()'s ordering.
               '' when the map is not a symmetric pair.
      *_hex    bitmask over bit index x + y*width
      facts    strategy values derived from the terrain offline, so no unit ever
               pays to recompute them:
                 cd     cardinal BFS steps between the two core footprints --
                        how close the fight starts. 6 on meander, 36 on saga.
                 ore_a  ore we should farm as team A, as tile indices, nearest
                        first, restricted to ore we reach no later than the
                        enemy. ore_b is the same list for team B.
"""

'''


def emit(entries: list[dict]) -> str:
    by_size: dict[tuple[int, int], list[dict]] = {}
    for e in entries:
        by_size.setdefault((e["w"], e["h"]), []).append(e)

    lines = [HEADER, "MAPS = {\n"]
    for (w, h) in sorted(by_size):
        lines.append(f"    ({w}, {h}): (\n")
        for e in sorted(by_size[(w, h)], key=lambda d: d["name"]):
            lines.append(
                f'        ("{e["name"]}", {e["core_a"]}, {e["core_b"]}, '
                f'"{e["sym"]}",\n'
                f'         "{e["wall_hex"]}",\n'
                f'         "{e["ore_hex"]}"),\n'
            )
        lines.append("    ),\n")
    lines.append("}\n")
    return "".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--maps", default=str(PROJECT_ROOT / "maps"))
    ap.add_argument("--out", default=str(PROJECT_ROOT / "bots" / "Heimdall_v6" / "mapdata.py"))
    ap.add_argument("--report", action="store_true",
                    help="print per-map terrain/ore/distance facts instead of writing")
    args = ap.parse_args()

    entries = []
    seen_keys: dict[tuple, str] = {}
    for path in sorted(Path(args.maps).glob("*.map26")):
        m = parse_map(path)
        wall, ore = masks(m)
        sym = symmetry(m)
        if not sym:
            print(f"WARNING {m['name']}: no flip both preserves terrain and swaps "
                  f"cores; entry will not claim a symmetry")
        key = (m["w"], m["h"], m["core_a"])
        if key in seen_keys:
            print(f"WARNING {m['name']}: identification key {key} collides with "
                  f"{seen_keys[key]}; both stay in the table and the terrain "
                  f"check will separate them")
        seen_keys[key] = m["name"]
        nibbles = (m["w"] * m["h"] + 3) // 4
        entries.append({
            "name": m["name"], "w": m["w"], "h": m["h"],
            "core_a": m["core_a"], "core_b": m["core_b"], "sym": sym,
            "wall_hex": format(wall, f"0{nibbles}x"),
            "ore_hex": format(ore, f"0{nibbles}x"),
            "_map": m,
        })

    if args.report:
        print(f"{'map':14s} {'size':>7s} {'sym':>4s} {'walls':>6s} {'ore':>4s} "
              f"{'mine':>5s} {'cont':>5s} {'core-d':>7s} nearest-ore")
        for e in entries:
            m = e["_map"]
            rep = ore_report(m)
            wall_n = bin(int(e["wall_hex"], 16)).count("1")
            ore_n = bin(int(e["ore_hex"], 16)).count("1")
            print(f"{e['name']:14s} {e['w']:>3}x{e['h']:<3} {e['sym']:>4s} "
                  f"{wall_n:>6} {ore_n:>4} {rep['a']:>5} {rep['contested']:>5} "
                  f"{core_distance(m):>7} {rep['nearest']}")
        return 0

    out = Path(args.out)
    out.write_text(emit(entries))
    total = sum(e["w"] * e["h"] for e in entries)
    print(f"wrote {out} -- {len(entries)} maps, {total} tiles, {out.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
