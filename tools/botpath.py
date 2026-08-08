#!/usr/bin/env python3
"""Resolve a bot name to a directory, wherever it lives.

`bots/` holds the active submission and nothing else, so that the thing we ship
is never one directory among thirty and there is no question which source was
uploaded. Everything else is frozen:

    bots/                  the active submission (Heimdall_v6)
    frozen/champions/      our own past submissions, for head-to-head benchmarks
    frozen/opponents/      reference bots to measure against
    frozen/archive/        superseded lineages and zips, kept for provenance

Nothing under frozen/ is edited. A champion snapshot is the exact source of a
submitted version, which is the only reason a head-to-head number means
anything later; editing one silently invalidates every result measured against
it.

Every entry point takes a bare name and this finds it, so `--bots X Khaos` and
`./run2d Heimdall_v6 loki` keep working regardless of which folder a bot sits
in. An explicit path still wins, so a scratch copy outside the tree is fine.

    python3 tools/botpath.py --list          names, grouped
    python3 tools/botpath.py loki            absolute path to one bot
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Ordered: the active submission shadows a frozen bot of the same name, which is
# what you want while iterating on a champion you have also snapshotted.
SEARCH_DIRS = (
    PROJECT_ROOT / "bots",
    PROJECT_ROOT / "frozen" / "champions",
    PROJECT_ROOT / "frozen" / "opponents",
    PROJECT_ROOT / "frozen" / "archive",
)


def resolve(name: str) -> Path:
    """Return the directory for `name`, or raise ValueError.

    A name containing a separator, or naming an existing directory, is treated
    as a path and used as-is.
    """
    candidate = Path(name).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    if "/" in name or "\\" in name:
        raise ValueError(f"bot not found at path: {name}")

    for directory in SEARCH_DIRS:
        found = directory / name
        if found.is_dir():
            return found.resolve()

    raise ValueError(
        f"bot not found: {name}\n"
        f"searched: {', '.join(str(d.relative_to(PROJECT_ROOT)) for d in SEARCH_DIRS)}\n"
        f"run `python3 tools/botpath.py --list` to see what is available"
    )


def available() -> dict[str, list[str]]:
    """Bot names by the directory they live in, in search order."""
    out = {}
    for directory in SEARCH_DIRS:
        if not directory.is_dir():
            continue
        label = str(directory.relative_to(PROJECT_ROOT))
        out[label] = sorted(
            p.name for p in directory.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", nargs="?", help="bot name to resolve")
    parser.add_argument("--list", action="store_true", help="list every known bot")
    args = parser.parse_args()

    if args.list or not args.name:
        for label, names in available().items():
            print(f"{label}/")
            for name in names:
                print(f"    {name}")
        return 0

    try:
        print(resolve(args.name))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
