#!/usr/bin/env python3
"""Summarise unrated matches, grouped by which of OUR submissions played them.

URs are the only local signal calibrated to the live field, but attributing a
batch by wall-clock is unreliable: opponents queue URs against us too, and those
run whatever we had active at the time. The match record carries
teamAVersion/teamBVersion, so group by that instead and the question "how did
v41 actually do" has an exact answer.

    python3 tools/ur_summary.py [--limit N] [--version V] [--ladder]
"""
import argparse, collections, json, subprocess, sys

TEAM = "Pantheon"


def fetch(limit, kind):
    out = subprocess.run(
        ["fcode", "match", "list", "--mine", "--type", kind, "--limit", str(limit), "--json"],
        capture_output=True, text=True).stdout
    line = next((l for l in reversed(out.splitlines()) if l.strip().startswith(("{", "["))), None)
    if not line:
        return []
    d = json.loads(line)
    return d["matches"] if isinstance(d, dict) else d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--version", type=int, help="only this submission version")
    ap.add_argument("--ladder", action="store_true", help="ladder instead of unrated")
    a = ap.parse_args()

    rows = []
    for m in fetch(a.limit, "ladder" if a.ladder else "unrated"):
        if m.get("status") != "complete":
            continue
        if TEAM not in (m["teamAName"], m["teamBName"]):
            continue
        mine_a = m["teamAName"] == TEAM
        ver = m["teamAVersion"] if mine_a else m["teamBVersion"]
        opp = m["teamBName"] if mine_a else m["teamAName"]
        mine, theirs = ((m["scoreA"], m["scoreB"]) if mine_a else (m["scoreB"], m["scoreA"]))
        rows.append((ver, m["completedAt"][11:16], opp, mine, theirs))

    if a.version:
        rows = [r for r in rows if r[0] == a.version]

    by = collections.defaultdict(lambda: [0, 0, 0, 0])
    for ver, _t, _opp, mine, theirs in rows:
        by[ver][0] += mine > theirs
        by[ver][1] += mine < theirs
        by[ver][2] += mine
        by[ver][3] += theirs

    for ver in sorted(by):
        w, l, gm, gt = by[ver]
        g = gm + gt
        print(f"  v{ver}: {w}W-{l}L matches | games {gm}-{gt} "
              f"({100 * gm / g:.1f}% game win)" if g else f"  v{ver}: no games")

    if a.version:
        print()
        for ver, t, opp, mine, theirs in sorted(rows, key=lambda r: r[1]):
            print(f"    {t} vs {opp:24s} {mine}-{theirs} {'W' if mine > theirs else 'L'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
