#!/usr/bin/env python3
"""Global non-transitivity of the ladder over the past day.

Pulls every completed match from the last `--hours` (default 24) via the
platform's /api/matches endpoint, builds a skew-symmetric pairwise-advantage
matrix A over the teams that played, computes the least-squares transitive
"elo" score

    s_i = (1/n) * sum_j A_ij

and the global non-transitivity

    NT = sqrt( sum_{i<j} [A_ij - (s_i - s_j)]^2  /  sum_{i<j} A_ij^2 ).

A_ij is the empirical game-level win advantage of team i over team j:
    A_ij = (g_ij - g_ji) / (g_ij + g_ji)          (0 if they never met)
where g_ij is the number of individual games i won against j (scoreA/scoreB
inside each best-of-N match). A is skew-symmetric and in [-1, 1].

Point it at a different platform with FCODE_API_URL (uses fcode's stored
credentials + api_get, so the same CLI login works for any compatible host).
"""

import argparse
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Backend is chosen at runtime (--platform); fcode and cambc expose an
# identical api_get / get_api_url and the same /api/matches shape.
api_get = None
get_api_url = None


def _select_backend(platform: str):
    global api_get, get_api_url
    mod = __import__(f"{platform}.api", fromlist=["api_get"])
    auth = __import__(f"{platform}.auth", fromlist=["get_api_url"])
    api_get = mod.api_get
    get_api_url = auth.get_api_url


def _parse_ts(s: str) -> datetime:
    # e.g. "2026-08-12T19:10:34.705Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def latest_completed_ts():
    """completedAt of the most recent completed match, or None."""
    data = api_get("/api/matches", {"limit": "1"})
    for m in data.get("matches", []):
        ts = m.get("completedAt") or m.get("createdAt")
        if ts:
            return _parse_ts(ts)
    return None


def fetch_matches(hours: float, end=None, verbose: bool = True):
    """Yield completed matches in the [end-hours, end] window.

    end defaults to now (a true "past N hours"); pass the latest match's
    timestamp to anchor on the last active day instead. /api/matches returns
    newest-first with a nextCursor; the cursor is simply a completedAt
    timestamp ("matches before this"), so when end is set we START at that
    boundary instead of paging backward from now. Page until completedAt drops
    below cutoff.
    """
    seed_cursor = None
    if end is None:
        end = datetime.now(timezone.utc)
    else:
        # Jump straight to the window's end rather than walking back from now.
        # Cursor is "matches strictly before this ts"; +1s keeps end inclusive.
        seed = end + timedelta(seconds=1)
        seed_cursor = seed.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    cutoff = end - timedelta(hours=hours)
    cursor = seed_cursor
    pages = 0
    while True:
        params = {"limit": "100"}
        if cursor:
            params["cursor"] = cursor
        data = api_get("/api/matches", params)
        matches = data.get("matches", [])
        if not matches:
            break
        pages += 1
        stop = False
        for m in matches:
            ts = m.get("completedAt") or m.get("createdAt")
            if not ts:
                continue
            when = _parse_ts(ts)
            if when < cutoff:
                stop = True
                break
            if when > end:
                continue
            if m.get("status") == "complete":
                yield m
        cursor = data.get("nextCursor")
        if verbose:
            print(f"  ...page {pages} ({len(matches)} matches)", file=sys.stderr)
        if stop or not cursor:
            break


def build_matrix(matches):
    """Aggregate directional game wins. g[(i,j)] = games i won vs j."""
    g = defaultdict(int)
    names = {}
    n_matches = 0
    n_games = 0
    for m in matches:
        a, b = m["teamAId"], m["teamBId"]
        names[a] = m.get("teamAName", a)
        names[b] = m.get("teamBName", b)
        sa, sb = m.get("scoreA") or 0, m.get("scoreB") or 0
        g[(a, b)] += sa
        g[(b, a)] += sb
        n_matches += 1
        n_games += sa + sb
    teams = sorted(names, key=lambda t: names[t].lower())
    return g, teams, names, n_matches, n_games


def compute(g, teams):
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    A = [[0.0] * n for _ in range(n)]
    for i, ti in enumerate(teams):
        for j in range(i + 1, n):
            tj = teams[j]
            gij = g.get((ti, tj), 0)
            gji = g.get((tj, ti), 0)
            tot = gij + gji
            if tot == 0:
                continue
            a = (gij - gji) / tot
            A[i][j] = a
            A[j][i] = -a
    # s_i = (1/n) sum_j A_ij  (unplayed pairs contribute 0)
    s = [sum(A[i]) / n for i in range(n)]
    # Two readings of sum_{i<j}:
    #   all   -> literally every i<j (unplayed pairs add (s_i-s_j)^2 to numerator)
    #   edges -> only pairs that actually played (the HodgeRank/least-squares set)
    num_all = num_edge = den = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            played = g.get((teams[i], teams[j]), 0) + g.get((teams[j], teams[i]), 0) > 0
            resid = (A[i][j] - (s[i] - s[j])) ** 2
            num_all += resid
            if played:
                num_edge += resid
                den += A[i][j] ** 2
    nt_all = math.sqrt(num_all / den) if den > 0 else 0.0
    nt_edge = math.sqrt(num_edge / den) if den > 0 else 0.0
    return A, s, nt_all, nt_edge, num_all, num_edge, den


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", default="fcode", choices=["fcode", "cambc"],
                    help="Which ladder to query (default fcode)")
    ap.add_argument("--hours", type=float, default=24.0, help="Look-back window (default 24)")
    ap.add_argument("--anchor-latest", action="store_true",
                    help="End the window at the last completed match instead of now "
                         "(use when the ladder has been idle)")
    ap.add_argument("--day", default=None,
                    help="Compute over a specific UTC calendar day YYYY-MM-DD "
                         "(sets a 24h window ending at that day's 24:00 UTC)")
    ap.add_argument("--top", type=int, default=15, help="How many teams to show by score")
    ap.add_argument("--restrict-top", type=int, default=0,
                    help="Rank all teams by elo s_i, keep the top N, and recompute "
                         "A/s/NT treating those N as their own universe (0 = off)")
    args = ap.parse_args()

    _select_backend(args.platform)
    print(f"API: {get_api_url()}", file=sys.stderr)
    end = None
    hours = args.hours
    if args.day:
        y, mo, d = (int(x) for x in args.day.split("-"))
        end = datetime(y, mo, d, tzinfo=timezone.utc) + timedelta(days=1)
        hours = 24.0
        print(f"Window: UTC day {args.day} (ends {end})", file=sys.stderr)
    elif args.anchor_latest:
        end = latest_completed_ts()
        print(f"Anchoring window to last match: {end}", file=sys.stderr)
    matches = list(fetch_matches(hours, end=end))
    g, teams, names, n_matches, n_games = build_matrix(matches)
    if not teams:
        print("No completed matches in the window.")
        return
    A, s, nt_all, nt_edge, num_all, num_edge, den = compute(g, teams)

    if args.restrict_top and args.restrict_top < len(teams):
        keep = sorted(range(len(teams)), key=lambda i: s[i], reverse=True)[: args.restrict_top]
        teams = [teams[i] for i in keep]
        print(f"\n>>> Restricting to top {args.restrict_top} teams by elo s_i, "
              f"recomputing within that universe (n={len(teams)}).")
        A, s, nt_all, nt_edge, num_all, num_edge, den = compute(g, teams)

    n = len(teams)
    total_pairs = n * (n - 1) // 2
    played_pairs = sum(1 for i in range(n) for j in range(i + 1, n)
                       if g.get((teams[i], teams[j]), 0) + g.get((teams[j], teams[i]), 0) > 0)

    print(f"\nWindow: past {args.hours:g}h   API: {get_api_url()}")
    print(f"Matches: {n_matches}   Games: {n_games}   Teams: {n}")
    print(f"Pairs: {played_pairs} played / {total_pairs} possible ({played_pairs/total_pairs*100:.0f}% of graph)")

    print(f"\nGLOBAL NON-TRANSITIVITY  NT = {nt_all:.4f}   (literal: sum over ALL i<j)")
    print(f"  numerator  sum_{{i<j}}[A_ij-(s_i-s_j)]^2 = {num_all:.4f}")
    print(f"  denominator sum_{{i<j}} A_ij^2           = {den:.4f}")
    print(f"  NT^2 = {nt_all*nt_all:.4f}")
    print(f"\n  NT = {nt_edge:.4f}   (edges only: sum over pairs that actually played)")
    print(f"  numerator (played pairs) = {num_edge:.4f}   NT^2 = {nt_edge*nt_edge:.4f}")
    print(f"  => {nt_edge*nt_edge*100:.1f}% of the observed head-to-head variation is non-transitive\n")

    order = sorted(range(len(teams)), key=lambda i: s[i], reverse=True)
    print(f"Transitive score s_i = (1/n) sum_j A_ij   (n={len(teams)})")
    print(f"{'rank':>4}  {'score':>8}  team")
    show = order if args.top <= 0 else order[: args.top]
    for r, i in enumerate(show, 1):
        print(f"{r:>4}  {s[i]:>+8.4f}  {names[teams[i]]}")
    if 0 < args.top < len(teams):
        print(f"   ...  ({len(teams) - args.top} more)")


if __name__ == "__main__":
    main()
