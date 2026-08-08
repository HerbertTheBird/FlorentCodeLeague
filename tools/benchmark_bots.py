#!/usr/bin/env python3
"""Benchmark Florent Code League bots across maps, seeds, and opponents.

Two modes:

  Head-to-head   --bots A B
      Every map/seed is played twice with the sides swapped, and the two bots'
      head-to-head record is reported.

  Test suite     --bot A [--suite X Y Z]
      `A` is played against every bot in the suite (default: loki, Khaos,
      Hermod, Heimdall_v3) on every map/seed, both sides. Results are reported
      per opponent and as an aggregate win rate.

Matches are independent, so they run in parallel (``--jobs``, default: half the
cores). Each worker gets its own pycache prefix and replay path.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPS_DIR = PROJECT_ROOT / "maps"

# The regression suite. Ladder_v36 is a snapshot of what is actually playing
# ranked, and it leads deliberately: the other four are old bots that the field
# has long overtaken, and a change can gain several points against them while
# losing unrated matches against real opponents -- which is exactly what
# happened to the economy-first build. Treat the Ladder_v36 column as the signal
# and the rest as a check against regressions in play we already relied on.
DEFAULT_SUITE = ("Ladder_v36", "loki", "Khaos", "Hermod", "Heimdall_v3")

# Not every opponent is equally informative. Ladder_v36 is a snapshot of a build
# that actually played ranked, so it predicts ladder results far better than the
# rest, which are old bots the field has long overtaken -- a change once gained
# several points against them while losing unrated matches against real teams.
# The weighted line is the one to read; the flat one is kept as a regression
# check. Nothing here beats an actual UR against a top team.
# Pick the opponent for what you are testing. Champion_vN head-to-head answers
# "does this beat what we ship", but it is BLIND to anything neither side does.
# Sentinels are the known case: Khaos fields 4 a game, while loki, Hermod,
# Heimdall_v3 and our own bot all build ~0, so a sentinel-targeting change
# measured in self-play ranked two variants in the exact opposite order to the
# same change measured against Khaos. Use --bots X Khaos for turret-matchup work.
SUITE_WEIGHTS = {"Ladder_v36": 4.0}
DEFAULT_WEIGHT = 1.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run each selected map twice, swapping player A/B, and report the "
            "head-to-head result (--bots) or a full suite record (--bot)."
        )
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--bots",
        nargs=2,
        metavar=("BOT_1", "BOT_2"),
        help="Head-to-head mode: the two bot names or paths.",
    )
    selection.add_argument(
        "--bot",
        metavar="BOT",
        help="Suite mode: the bot under test, played against every --suite bot.",
    )
    parser.add_argument(
        "--suite",
        nargs="+",
        metavar="BOT",
        help=(
            "Opponents for suite mode (space- or comma-separated). "
            f"Default: {' '.join(DEFAULT_SUITE)}."
        ),
    )
    parser.add_argument(
        "--maps",
        nargs="+",
        metavar="MAP",
        help=(
            "Map names or paths (space- or comma-separated). Omit this option, "
            "or pass 'all', for every maps/*.map26 file — the pool rotates, so "
            "the wider set is the better generalisation proxy. Pass 'pool' to "
            "restrict to the current competition pool."
        ),
    )
    parser.add_argument(
        "--seeds",
        nargs="+",
        type=int,
        default=(1,),
        metavar="SEED",
        help="One or more seeds per map (default: 1).",
    )
    parser.add_argument(
        "--tle",
        type=int,
        default=10,
        help="Per-turn time limit in milliseconds (default: 10, matching server).",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=max(1, (os.cpu_count() or 4) // 4),
        help=(
            "Matches to run concurrently (default: a quarter of the cores). Each "
            "match is single-threaded, but every one of them is racing a 10ms "
            "per-turn budget, so oversubscribing the machine turns real results "
            "into spurious TLE losses. A quarter leaves headroom for whatever "
            "else is running."
        ),
    )
    parser.add_argument(
        "--fcode",
        default="fcode",
        help="fcode executable or command (default: fcode).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of the text table.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Also save the full JSON results to this path.",
    )
    args = parser.parse_args()
    if args.bots is None and args.bot is None:
        args.bot = "Heimdall_v6"
    return args


def _split_list_args(values: list[str] | None) -> list[str]:
    if not values:
        return []
    result = []
    for value in values:
        result.extend(part.strip() for part in value.split(",") if part.strip())
    return result


def competition_pool() -> list[str] | None:
    """Map names in the current competition pool, or None if we can't reach it.

    This is the set that actually decides the ladder. maps/ also accumulates
    older maps that have since been dropped from the pool — benchmarking over
    everything in the directory silently weights results toward maps that no
    longer count, so the pool is the default and `--maps all` is the opt-out.
    """
    try:
        from fcode.commands.maps import fetch_pool
        return [entry["name"] for entry in fetch_pool()]
    except Exception as exc:  # offline, unauthenticated, API change
        print(f"warning: could not fetch competition map pool ({exc}); "
              f"falling back to every map in {DEFAULT_MAPS_DIR}", file=sys.stderr)
        return None


def resolve_maps(values: list[str] | None) -> list[Path]:
    """Maps to benchmark on.

    Default is every map in maps/, not just the current competition pool. The
    pool rotates, so a bot tuned only against today's fifteen is tuned against a
    set that will not be the one it plays next week; the wider directory is the
    better proxy for generalisation. `--maps pool` restricts to the live pool
    when you specifically want to know how the current rotation looks.
    """
    requested = _split_list_args(values)
    if requested and any(value.lower() == "pool" for value in requested):
        pool = competition_pool()
        if pool is not None:
            maps, missing = [], []
            for name in sorted(pool):
                path = DEFAULT_MAPS_DIR / f"{name}.map26"
                (maps.append(path) if path.is_file() else missing.append(name))
            if missing:
                print(f"warning: pool maps missing locally (run `fcode maps sync`): "
                      f"{', '.join(missing)}", file=sys.stderr)
            if maps:
                return list(dict.fromkeys(maps))
        maps = sorted(DEFAULT_MAPS_DIR.glob("*.map26"))
    elif not requested or any(value.lower() == "all" for value in requested):
        maps = sorted(DEFAULT_MAPS_DIR.glob("*.map26"))
    else:
        maps = []
        for value in requested:
            raw = Path(value).expanduser()
            candidates = [raw]
            if not raw.is_absolute():
                candidates.append(PROJECT_ROOT / raw)
                candidates.append(DEFAULT_MAPS_DIR / raw)
            if raw.suffix != ".map26":
                candidates.extend(path.with_suffix(".map26") for path in tuple(candidates))
            match = next((path.resolve() for path in candidates if path.is_file()), None)
            if match is None:
                raise ValueError(f"map not found: {value}")
            maps.append(match)

    unique = list(dict.fromkeys(maps))
    if not unique:
        raise ValueError(f"no .map26 files found in {DEFAULT_MAPS_DIR}")
    return unique


def snapshot_bots(names: list[str], dest: Path) -> dict[str, str]:
    """Copy each bot's source into `dest`, returning name -> path to run.

    A benchmark over every map takes minutes, and fcode reads a bot's source when
    each match starts. Without a snapshot, editing a bot mid-run silently splits
    the results between two versions. Copying up front pins one version for the
    whole run and leaves the working tree free to keep changing.
    """
    dest.mkdir(parents=True, exist_ok=True)
    resolved = {}
    for name in names:
        source = Path(name).expanduser()
        if not source.is_dir():
            source = PROJECT_ROOT / "bots" / name
        if not source.is_dir():
            raise ValueError(f"bot not found: {name}")
        target = dest / Path(name).name
        shutil.copytree(
            source, target, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        resolved[name] = str(target)
    return resolved


def _parse_fcode_json(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("fcode did not emit a JSON result")


def run_match(
    fcode_command: str,
    bot_a: str,
    bot_b: str,
    map_path: Path,
    seed: int,
    tle: int,
    replay_path: Path,
    pycache_dir: Path,
) -> dict[str, Any]:
    command = [
        *shlex.split(fcode_command),
        "run",
        bot_a,
        bot_b,
        str(map_path),
        "--seed",
        str(seed),
        "--tle",
        str(tle),
        "--replay",
        str(replay_path),
        "--json",
    ]
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(pycache_dir)
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command exited {completed.returncode}: {' '.join(command)}\n{combined.strip()}"
        )
    result = _parse_fcode_json(combined)
    if "error" in result:
        raise RuntimeError(str(result["error"]))
    result.update(
        {
            "map": map_path.stem,
            "map_path": str(map_path),
            "seed": seed,
            "bot_a": bot_a,
            "bot_b": bot_b,
            "winner_bot": (
                bot_a if result.get("winner") == "A"
                else bot_b if result.get("winner") == "B"
                else None
            ),
            "runtime_warning": "Traceback" in combined,
        }
    )
    return result


def perspective_result(match: dict[str, Any], bot: str) -> str:
    if match.get("error"):
        return "ERROR"
    winner = match.get("winner_bot")
    outcome = "D" if winner is None else ("W" if winner == bot else "L")
    warning = "!" if match.get("runtime_warning") else ""
    return f"{outcome}{warning} ({match.get('turns', '?')}t)"


def record(matches: list[dict[str, Any]], bot: str) -> dict[str, int]:
    """Win/loss/draw/error tally from `bot`'s perspective."""
    tally = {"W": 0, "L": 0, "D": 0, "E": 0}
    for match in matches:
        if match.get("error"):
            tally["E"] += 1
        elif match.get("winner_bot") is None:
            tally["D"] += 1
        elif match["winner_bot"] == bot:
            tally["W"] += 1
        else:
            tally["L"] += 1
    return tally


def _score_line(tally: dict[str, int]) -> str:
    played = tally["W"] + tally["L"] + tally["D"]
    rate = f"{100.0 * (tally['W'] + 0.5 * tally['D']) / played:5.1f}%" if played else "    -"
    extra = f", {tally['E']} errors" if tally["E"] else ""
    return f"{tally['W']}W-{tally['L']}L-{tally['D']}D  {rate}{extra}"


def _print_grid(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers))]

    def format_row(row: list[str]) -> str:
        return " | ".join(value.ljust(widths[i]) for i, value in enumerate(row))

    print(format_row(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(format_row(row))


def print_head_to_head(matches: list[dict[str, Any]], bots: list[str]) -> None:
    bot_1, bot_2 = bots
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for match in matches:
        grouped.setdefault((match["map"], match["seed"]), []).append(match)

    rows = []
    for (map_name, seed), pair in grouped.items():
        bot_1_as_a = next(match for match in pair if match["bot_a"] == bot_1)
        bot_2_as_a = next(match for match in pair if match["bot_a"] == bot_2)
        tally = record(pair, bot_1)
        score = f"{tally['W']}-{tally['L']}"
        if tally["D"]:
            score += f" ({tally['D']}D)"
        rows.append(
            [
                map_name,
                str(seed),
                perspective_result(bot_1_as_a, bot_1),
                perspective_result(bot_2_as_a, bot_2),
                score,
            ]
        )

    _print_grid(rows, ["Map", "Seed", f"{bot_1} as A", f"{bot_2} as A", f"{bot_1}-{bot_2}"])
    print()
    print(f"Total for {bot_1}: {_score_line(record(matches, bot_1))} ({len(matches)} matches)")
    if any(match.get("runtime_warning") for match in matches):
        print("! indicates that the bot emitted a Python traceback during the match.")


def print_suite(matches: list[dict[str, Any]], bot: str, suite: list[str]) -> None:
    """Per-map x per-opponent grid, then a per-opponent summary."""
    by_opponent: dict[str, list[dict[str, Any]]] = {name: [] for name in suite}
    for match in matches:
        opponent = match["bot_b"] if match["bot_a"] == bot else match["bot_a"]
        by_opponent.setdefault(opponent, []).append(match)

    keys = sorted({(match["map"], match["seed"]) for match in matches})
    rows = []
    for map_name, seed in keys:
        cells = []
        for opponent in suite:
            pair = [
                match
                for match in by_opponent[opponent]
                if match["map"] == map_name and match["seed"] == seed
            ]
            tally = record(pair, bot)
            cells.append(f"{tally['W']}-{tally['L']}" + (f" ({tally['D']}D)" if tally["D"] else ""))
        rows.append([map_name, str(seed), *cells])

    _print_grid(rows, ["Map", "Seed", *suite])

    print()
    print(f"{bot} vs suite (W-L-D from {bot}'s perspective, both sides played):")
    for opponent in suite:
        print(f"  {opponent:<16} {_score_line(record(by_opponent[opponent], bot))}")
    print(f"  {'OVERALL':<16} {_score_line(record(matches, bot))} ({len(matches)} matches)")
    num = den = 0.0
    for opponent in suite:
        tally = record(by_opponent[opponent], bot)
        played = tally["W"] + tally["L"] + tally["D"]
        if not played:
            continue
        w = SUITE_WEIGHTS.get(opponent, DEFAULT_WEIGHT)
        num += w * (tally["W"] + 0.5 * tally["D"]) / played
        den += w
    if den:
        weights = ", ".join(f"{o}x{SUITE_WEIGHTS.get(o, DEFAULT_WEIGHT):g}" for o in suite)
        print(f"  {'WEIGHTED':<16} {100 * num / den:5.1f}%   ({weights})")
    if any(match.get("runtime_warning") for match in matches):
        print("! indicates that a bot emitted a Python traceback during the match.")


def main() -> int:
    args = parse_args()
    try:
        maps = resolve_maps(args.maps)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    suite_mode = args.bot is not None
    if suite_mode:
        suite = _split_list_args(args.suite) or list(DEFAULT_SUITE)
        suite = [name for name in dict.fromkeys(suite) if name != args.bot]
        if not suite:
            print("error: suite is empty", file=sys.stderr)
            return 2
        bots = [args.bot, *suite]
        pairings = [(args.bot, opponent) for opponent in suite]
    else:
        suite = []
        bots = list(args.bots)
        pairings = [(bots[0], bots[1])]

    match_specs = []
    for (first, second), map_path, seed in itertools.product(pairings, maps, args.seeds):
        match_specs.append((first, second, map_path, seed))
        match_specs.append((second, first, map_path, seed))

    total = len(match_specs)
    done = 0
    lock = threading.Lock()
    matches: list[dict[str, Any] | None] = [None] * total

    with tempfile.TemporaryDirectory(prefix="fcode-benchmark-") as temp:
        temp_dir = Path(temp)
        replay_dir = temp_dir / "replays"
        replay_dir.mkdir()
        try:
            snapshot = snapshot_bots(bots, temp_dir / "bots")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        def work(index: int, spec) -> None:
            nonlocal done
            bot_a, bot_b, map_path, seed = spec
            replay = replay_dir / f"{index:04d}-{map_path.stem}-{seed}.replay26"
            # One pycache prefix per worker slot, so parallel interpreters never
            # race writing the same .pyc.
            pycache_dir = temp_dir / f"pycache-{index % args.jobs}"
            try:
                match = run_match(
                    args.fcode, snapshot[bot_a], snapshot[bot_b],
                    map_path, seed, args.tle, replay, pycache_dir,
                )
                # Report under the friendly names, not the snapshot paths.
                match["bot_a"], match["bot_b"] = bot_a, bot_b
                match["winner_bot"] = (
                    bot_a if match.get("winner") == "A"
                    else bot_b if match.get("winner") == "B"
                    else None
                )
            except Exception as exc:
                match = {
                    "map": map_path.stem,
                    "map_path": str(map_path),
                    "seed": seed,
                    "bot_a": bot_a,
                    "bot_b": bot_b,
                    "winner_bot": None,
                    "error": str(exc),
                }
            matches[index] = match
            with lock:
                done += 1
                print(
                    f"[{done}/{total}] {map_path.stem} seed={seed}: "
                    f"{bot_a} (A) vs {bot_b} (B) -> {match.get('winner_bot') or 'draw/error'}",
                    file=sys.stderr,
                )

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            list(pool.map(lambda pair: work(*pair), enumerate(match_specs)))

    results = [match for match in matches if match is not None]
    payload = {
        "config": {
            "bot": args.bot,
            "bots": bots,
            "suite": suite,
            "maps": [path.stem for path in maps],
            "seeds": list(args.seeds),
            "tle": args.tle,
            "jobs": args.jobs,
            "sides_swapped": True,
        },
        "summary": (
            {opponent: record([m for m in results
                               if opponent in (m["bot_a"], m["bot_b"])], args.bot)
             for opponent in suite}
            if suite_mode else record(results, bots[0])
        ),
        "overall": record(results, args.bot if suite_mode else bots[0]),
        "matches": results,
    }
    if args.output:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2))
    elif suite_mode:
        print_suite(results, args.bot, suite)
    else:
        print_head_to_head(results, bots)
    return 1 if payload["overall"]["E"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
