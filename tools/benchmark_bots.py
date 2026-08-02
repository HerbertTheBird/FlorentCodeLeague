#!/usr/bin/env python3
"""Benchmark two Florent Code League bots across one or more maps.

Every map/seed pairing is played twice with player sides swapped. By default
the script compares Loki and Heimdall v0 on every map in ``maps/`` using the
server turn-time limit.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPS_DIR = PROJECT_ROOT / "maps"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run each selected map twice, swapping player A/B, and report the "
            "head-to-head result."
        )
    )
    parser.add_argument(
        "--maps",
        nargs="+",
        metavar="MAP",
        help=(
            "Map names or paths (space- or comma-separated). Omit this option, "
            "or pass 'all', to benchmark every maps/*.map26 file."
        ),
    )
    parser.add_argument(
        "--bots",
        nargs=2,
        default=("loki", "Heimdall_v0"),
        metavar=("BOT_1", "BOT_2"),
        help="The two bot names or paths (default: loki Heimdall_v0).",
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
    return parser.parse_args()


def _split_map_args(values: list[str] | None) -> list[str]:
    if not values:
        return []
    result = []
    for value in values:
        result.extend(part.strip() for part in value.split(",") if part.strip())
    return result


def resolve_maps(values: list[str] | None) -> list[Path]:
    requested = _split_map_args(values)
    if not requested or any(value.lower() == "all" for value in requested):
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


def win_totals(matches: list[dict[str, Any]], bots: tuple[str, str] | list[str]) -> dict[str, int]:
    totals = {bots[0]: 0, bots[1]: 0, "draws": 0, "errors": 0}
    for match in matches:
        if match.get("error"):
            totals["errors"] += 1
        elif match.get("winner_bot") is None:
            totals["draws"] += 1
        else:
            totals[match["winner_bot"]] += 1
    return totals


def print_table(matches: list[dict[str, Any]], bots: list[str]) -> None:
    bot_1, bot_2 = bots
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for match in matches:
        grouped.setdefault((match["map"], match["seed"]), []).append(match)

    rows = []
    for (map_name, seed), pair in grouped.items():
        bot_1_as_a = next(match for match in pair if match["bot_a"] == bot_1)
        bot_2_as_a = next(match for match in pair if match["bot_a"] == bot_2)
        totals = win_totals(pair, bots)
        score = f"{totals[bot_1]}-{totals[bot_2]}"
        if totals["draws"]:
            score += f" ({totals['draws']}D)"
        rows.append(
            [
                map_name,
                str(seed),
                perspective_result(bot_1_as_a, bot_1),
                perspective_result(bot_2_as_a, bot_2),
                score,
            ]
        )

    headers = ["Map", "Seed", f"{bot_1} as A", f"{bot_2} as A", f"{bot_1}-{bot_2}"]
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers))]

    def format_row(row: list[str]) -> str:
        return " | ".join(value.ljust(widths[i]) for i, value in enumerate(row))

    print(format_row(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(format_row(row))

    totals = win_totals(matches, bots)
    print()
    print(
        f"Total: {bot_1} {totals[bot_1]} wins, {bot_2} {totals[bot_2]} wins, "
        f"{totals['draws']} draws, {totals['errors']} errors ({len(matches)} matches)."
    )
    if any(match.get("runtime_warning") for match in matches):
        print("! indicates that the bot emitted a Python traceback during the match.")


def build_payload(
    matches: list[dict[str, Any]], bots: list[str], maps: list[Path], args: argparse.Namespace
) -> dict[str, Any]:
    return {
        "config": {
            "bots": bots,
            "maps": [path.stem for path in maps],
            "seeds": list(args.seeds),
            "tle": args.tle,
            "sides_swapped": True,
        },
        "summary": win_totals(matches, bots),
        "matches": matches,
    }


def main() -> int:
    args = parse_args()
    bots = list(args.bots)
    try:
        maps = resolve_maps(args.maps)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    match_specs = []
    for map_path in maps:
        for seed in args.seeds:
            match_specs.append((bots[0], bots[1], map_path, seed))
            match_specs.append((bots[1], bots[0], map_path, seed))

    matches = []
    with tempfile.TemporaryDirectory(prefix="fcode-benchmark-") as temp:
        temp_dir = Path(temp)
        replay_dir = temp_dir / "replays"
        replay_dir.mkdir()
        pycache_dir = temp_dir / "pycache"
        for index, (bot_a, bot_b, map_path, seed) in enumerate(match_specs, start=1):
            print(
                f"[{index}/{len(match_specs)}] {map_path.stem} seed={seed}: "
                f"{bot_a} (A) vs {bot_b} (B)",
                file=sys.stderr,
            )
            replay = replay_dir / f"{index:04d}-{map_path.stem}-{seed}.replay26"
            try:
                match = run_match(
                    args.fcode,
                    bot_a,
                    bot_b,
                    map_path,
                    seed,
                    args.tle,
                    replay,
                    pycache_dir,
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
            matches.append(match)

    payload = build_payload(matches, bots, maps, args)
    if args.output:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print_table(matches, bots)
    return 1 if payload["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
