#!/usr/bin/env python3
"""Correctness sweep for the hardcoded-map layer.

Win rate cannot tell you whether the map table is being recognised, or whether
it is quietly being contradicted and reverted on half the board -- both look
like "the bot played". This runs the bot on every map from both sides with
`hardcode.DEBUG` on and asserts the three things that must hold:

    identified   every map reaches ACTIVE, from both sides
    correct      no unit ever REVERTs (the table matches the real board)
    clean        no unit raises (main.Player.run prints "Error:" and the engine
                 destroys that unit permanently, which is silent in the score)

It also reports how many rounds recognition took, which should be 0 everywhere
-- a nonzero number means two table entries share a size and a core and the
terrain check needed extra vision to separate them.

    python3 tools/hc_check.py [--bot V6_hardcode] [--opp Base_hc] [--jobs N]
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import botpath  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HC_LINE = re.compile(r"^HC r(\d+) u(\d+) (Team\.\w+): (.*)$", re.M)


def debug_copy(bot: str, dest: Path) -> Path:
    """A copy of the bot with hardcode.DEBUG flipped on."""
    src = botpath.resolve(bot)
    out = dest / src.name
    shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__"))
    hc = out / "hardcode.py"
    if not hc.exists():
        raise SystemExit(f"{bot} has no hardcode.py -- nothing to check")
    hc.write_text(hc.read_text().replace("DEBUG = False", "DEBUG = True", 1))
    return out


def run_one(args) -> dict:
    bot_dir, opp_dir, mp, side, tle = args
    a, b = (bot_dir, opp_dir) if side == "A" else (opp_dir, bot_dir)
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(
            ["fcode", "run", str(a), str(b), f"maps/{mp}.map26",
             "--seed", "1", "--tle", str(tle), "--replay", "/dev/null", "--json"],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
            env={**__import__("os").environ, "PYTHONPYCACHEPREFIX": tmp})
    events = [(int(rd), int(uid), team, msg)
              for rd, uid, team, msg in HC_LINE.findall(r.stderr)]
    # Only our side's units trace; the opponent has no hardcode.py.
    active = [e for e in events if e[3].startswith("ACTIVE")]
    reverts = [e for e in events if e[3].startswith("REVERT")]
    misses = [e for e in events if e[3].startswith("no ")]
    errors = [ln for ln in r.stdout.splitlines() + r.stderr.splitlines()
              if ln.startswith("Error:")]
    last = next((ln for ln in reversed(r.stdout.splitlines())
                 if ln.startswith("{")), None)
    return {
        "map": mp, "side": side,
        "n_active": len(active), "n_revert": len(reverts), "n_miss": len(misses),
        "first_active_round": min((e[0] for e in active), default=None),
        "worst_active_round": max((e[0] for e in active), default=None),
        "names": sorted({e[3].split()[1] for e in active}),
        "revert_msgs": [e[3] for e in reverts][:3],
        "miss_msgs": [e[3] for e in misses][:3],
        "errors": errors[:3],
        "result": json.loads(last) if last else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bot", default="V6_hardcode")
    ap.add_argument("--opp", default="Base_hc")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--tle", type=int, default=5000,
                    help="generous by default: a CPU cutoff would hide a bug "
                         "behind a skipped turn rather than surfacing it")
    ap.add_argument("--maps", default="all")
    a = ap.parse_args()

    maps = (sorted(p.stem for p in (PROJECT_ROOT / "maps").glob("*.map26"))
            if a.maps == "all" else a.maps.replace(",", " ").split())

    with tempfile.TemporaryDirectory() as tmp:
        bot_dir = debug_copy(a.bot, Path(tmp))
        opp_dir = botpath.resolve(a.opp)
        jobs = [(bot_dir, opp_dir, m, s, a.tle) for m in maps for s in ("A", "B")]
        with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
            rows = list(ex.map(run_one, jobs))

    bad = collections.Counter()
    print(f"{'map':14s} {'side':>4s} {'active':>7s} {'first':>7s} "
          f"{'revert':>7s} {'miss':>5s} {'err':>4s}  identified-as")
    for r in sorted(rows, key=lambda d: (d["map"], d["side"])):
        flag = ""
        if not r["n_active"]:
            bad["never identified"] += 1
            flag = "  <-- NEVER IDENTIFIED"
        if r["n_revert"]:
            bad["reverted"] += 1
            flag = f"  <-- REVERT {r['revert_msgs']}"
        if r["n_miss"]:
            bad["gave up"] += 1
            flag = f"  <-- GAVE UP {r['miss_msgs']}"
        if r["errors"]:
            bad["unit exception"] += 1
            flag += f"  <-- ERROR {r['errors']}"
        if len(r["names"]) > 1:
            bad["ambiguous"] += 1
            flag += "  <-- MULTIPLE NAMES"
        print(f"{r['map']:14s} {r['side']:>4s} {r['n_active']:>7} "
              f"{str(r['first_active_round']):>7} {r['n_revert']:>7} "
              f"{r['n_miss']:>5} {len(r['errors']):>4}  "
              f"{','.join(r['names']) or '-'}{flag}")

    print()
    if bad:
        for k, v in bad.most_common():
            print(f"FAIL {k}: {v}")
        return 1
    late = [r for r in rows if (r["first_active_round"] or 0) > 0]
    print(f"OK: {len(rows)} runs, every map identified from both sides, "
          f"no reverts, no unit exceptions.")
    if late:
        print("the FIRST unit needed more than turn 0 to recognise the map on: "
              + ", ".join(f"{r['map']}/{r['side']}@{r['first_active_round']}"
                          for r in late))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
