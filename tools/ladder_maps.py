#!/usr/bin/env python3
"""Per-map ladder record, from the platform's own game log.

`ur_summary.py` answers "how did version N do"; this answers "which maps do we
lose on, to whom, and how". A match's JSON carries a `mapConfig` naming every
game's map alongside the per-game results, so the whole per-map table can be
built without downloading a single replay.

That question is the one worth asking before writing map-specific code: the pool
is fifteen fixed maps and our record is not flat across them, so effort belongs
where the losses are and not where a local benchmark happens to be noisy.

The per-game table only exists in `match info`'s rendered output -- the --json
form carries `mapConfig` but no game results -- so that text is what gets
parsed, and the map list is used to line games up with maps. Completed matches
never change, so responses are cached under `.ladder_cache/` and re-runs only
fetch what is new.

    python3 tools/ladder_maps.py [--limit N] [--type ladder|unrated]
                                 [--version V] [--since YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
from pathlib import Path
import re
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE = PROJECT_ROOT / ".ladder_cache"
TEAM = "Pantheon"


def _cli_json(args: list[str]):
    out = subprocess.run(["fcode", *args], capture_output=True, text=True).stdout
    line = next((l for l in reversed(out.splitlines())
                 if l.strip().startswith(("{", "["))), None)
    return json.loads(line) if line else None


def match_list(limit: int, kind: str) -> list[dict]:
    d = _cli_json(["match", "list", "--mine", "--type", kind,
                   "--limit", str(limit), "--json"])
    if d is None:
        return []
    return d["matches"] if isinstance(d, dict) else d


GAME_ROW = re.compile(
    r"^\s*│\s*(\d+)\s*│\s*(\S+)\s*│\s*([AB])\s*\([^)]*\)\s*"
    r"│\s*([^│]+?)\s*│\s*(\d+)\s*│\s*$", re.M)


def match_games(mid: str) -> list[dict]:
    """Per-game rows for one match, cached.

    Parsed out of `match info`'s rendered table rather than its JSON: the JSON
    stops at `mapConfig` and never says who won each game.
    """
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{mid}.games.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            path.unlink()
    out = subprocess.run(["fcode", "match", "info", mid],
                         capture_output=True, text=True).stdout
    rows = [{"n": int(n), "map": mp, "winner": side, "cond": cond.strip(),
             "turns": int(turns)}
            for n, mp, side, cond, turns in GAME_ROW.findall(out)]
    if rows:
        path.write_text(json.dumps(rows))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--type", default="ladder", choices=("ladder", "unrated"))
    ap.add_argument("--version", type=int, help="only this submission version")
    ap.add_argument("--since", help="ISO date; only matches completed on/after")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--by-opponent", action="store_true",
                    help="also break the per-map record down by opponent")
    a = ap.parse_args()

    matches = [m for m in match_list(a.limit, a.type)
               if m.get("status") == "complete"
               and TEAM in (m.get("teamAName"), m.get("teamBName"))]
    if a.since:
        matches = [m for m in matches if (m.get("completedAt") or "") >= a.since]
    if not matches:
        print("no completed matches found", file=sys.stderr)
        return 1

    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        infos = list(ex.map(lambda m: match_games(m["id"]), matches))

    rows = []
    skipped = 0
    for m, gs in zip(matches, infos):
        mine_a = m.get("teamAName") == TEAM
        ver = m.get("teamAVersion") if mine_a else m.get("teamBVersion")
        if a.version is not None and ver != a.version:
            continue
        opp = m.get("teamBName") if mine_a else m.get("teamAName")
        if not gs:
            skipped += 1
            continue
        me = "A" if mine_a else "B"
        for g in gs:
            if not g["map"] or not g["winner"]:
                continue
            rows.append({**g, "opp": opp, "ver": ver, "side": me,
                         "won": g["winner"] == me})

    if not rows:
        print(f"no per-game rows recovered (skipped {skipped} matches)",
              file=sys.stderr)
        return 1

    per_map = collections.defaultdict(lambda: [0, 0])
    per_map_side = collections.defaultdict(lambda: [0, 0])
    loss_cond = collections.defaultdict(collections.Counter)
    pair = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        per_map[r["map"]][not r["won"]] += 1
        per_map_side[(r["map"], r["side"])][not r["won"]] += 1
        pair[(r["map"], r["opp"])][not r["won"]] += 1
        if not r["won"]:
            loss_cond[r["map"]][r["cond"] or "?"] += 1

    vers = sorted({r["ver"] for r in rows if r["ver"] is not None})
    print(f"{len(rows)} games from {len(matches)} matches"
          f"{f', versions {vers[0]}-{vers[-1]}' if vers else ''}"
          f"{f', {skipped} skipped' if skipped else ''}\n")

    print(f"{'map':14s} {'W':>4s}{'L':>4s} {'rate':>7s}  {'asA':>7s} {'asB':>7s}"
          f"   how we lose")
    for mp in sorted(per_map, key=lambda k: per_map[k][0] / max(sum(per_map[k]), 1)):
        w, l = per_map[mp]
        aw, al = per_map_side[(mp, "A")]
        bw, bl = per_map_side[(mp, "B")]
        conds = ", ".join(f"{c}x{n}" for c, n in loss_cond[mp].most_common(3))
        print(f"{mp:14s} {w:>4}{l:>4} {100*w/max(w+l,1):6.1f}%  "
              f"{f'{aw}-{al}':>7} {f'{bw}-{bl}':>7}   {conds}")
    tw = sum(v[0] for v in per_map.values())
    tl = sum(v[1] for v in per_map.values())
    print(f"{'TOTAL':14s} {tw:>4}{tl:>4} {100*tw/max(tw+tl,1):6.1f}%")

    if a.by_opponent:
        print("\nper map x opponent (only where we are under 50%):")
        for (mp, opp), (w, l) in sorted(pair.items()):
            if w + l >= 3 and w / (w + l) < 0.5:
                print(f"  {mp:14s} vs {opp:24s} {w}-{l}  {100*w/(w+l):5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
