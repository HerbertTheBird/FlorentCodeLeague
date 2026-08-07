#!/usr/bin/env python3
"""Summarise recent unrated matches — the closest thing to a real benchmark.

URs run the *active* submission against another team's live bot, so they are the
only local-ish signal calibrated to the current field. Rate-limited to 5 per 10
minutes, so treat each batch as a small sample and read game totals, not just
match wins.

    python3 tools/ur_summary.py [--since ISO8601] [--limit N]
"""
import argparse, json, subprocess, sys, collections

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO timestamp; only matches completed after")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    raw = subprocess.run(["fcode", "match", "list", "--mine", "--type", "unrated",
                          "--limit", str(a.limit), "--json"],
                         capture_output=True, text=True).stdout
    line = next((l for l in reversed(raw.splitlines()) if l.strip().startswith(("{", "["))), None)
    if not line:
        print("no JSON from fcode", file=sys.stderr); return 1
    d = json.loads(line)
    ms = d["matches"] if isinstance(d, dict) else d
    rows, tally = [], collections.Counter()
    for m in ms:
        if m.get("status") != "complete":
            continue
        if a.since and m["completedAt"] < a.since:
            continue
        mine_is_a = m["teamAName"] == "Pantheon"
        opp = m["teamBName"] if mine_is_a else m["teamAName"]
        mine, theirs = ((m["scoreA"], m["scoreB"]) if mine_is_a
                        else (m["scoreB"], m["scoreA"]))
        rows.append((m["completedAt"][11:16], opp, mine, theirs))
        tally["W" if mine > theirs else "L"] += 1
        tally["gm"] += mine; tally["gt"] += theirs
    for t, opp, mi, th in sorted(rows):
        print(f"  {t} vs {opp:22s} {mi}-{th} {'W' if mi > th else 'L'}")
    g = tally["gm"] + tally["gt"]
    if g:
        print(f"\n  {tally['W']}W-{tally['L']}L matches | games {tally['gm']}-{tally['gt']} "
              f"({100 * tally['gm'] / g:.1f}% game win)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
