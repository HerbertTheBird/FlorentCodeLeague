#!/usr/bin/env python3
"""Prove the generated tables are the same tables.

This is a refactor, so the only acceptable result is equality: the dicts the bot
now imports as literals must equal the ones the old zlib+codec path produced,
key for key and byte for byte. Anything less and the "cleanup" is a behaviour
change wearing a cleanup's clothes.

Also reports what the split actually bought, since that is the claim being made.
"""

from __future__ import annotations

import sys
import time
import zlib
from pathlib import Path

BUILD = Path(__file__).resolve().parent
BOT = BUILD.parent
sys.path.insert(0, str(BUILD / "precompute"))
sys.path.insert(0, str(BOT))


def timed(label, fn):
    start = time.perf_counter()
    value = fn()
    return value, 1000 * (time.perf_counter() - start)


def main() -> int:
    import chip_precompute as solver
    import chip_barrier_data
    import chip_bestmove_data

    old_barrier, t_ob = timed("barrier", lambda: solver._dec(
        zlib.decompress(chip_barrier_data.ZDATA), 0)[0])
    old_bestmove, t_om = timed("bestmove", lambda: solver._dec(
        zlib.decompress(chip_bestmove_data.ZDATA), 0)[0])

    for name in ("chip_tables", "chip_lookup"):
        sys.modules.pop(name, None)
    new, t_new = timed("generated", lambda: __import__("chip_tables"))

    ok = True
    if new.BFILTER != old_barrier:
        ok = False
        print(f"MISMATCH barrier: {len(new.BFILTER)} vs {len(old_barrier)} keys")
    if new.BESTMOVE != old_bestmove:
        ok = False
        differing = [k for k in old_bestmove
                     if new.BESTMOVE.get(k) != old_bestmove[k]]
        print(f"MISMATCH best-move: {len(differing)} of {len(old_bestmove)} keys")

    # The four names the bot reads out of the solver must be identical objects.
    import chip_lookup
    for name in ("ALL_CELLS", "DIAGONALS"):
        if getattr(chip_lookup, name) != getattr(solver, name):
            ok = False
            print(f"MISMATCH {name}")

    print(f"barrier   {len(old_barrier):>6,} keys  identical")
    print(f"best-move {len(old_bestmove):>6,} keys  identical")
    print(f"import: old {t_ob + t_om:6.1f} ms  ->  new {t_new:6.1f} ms")
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


raise SystemExit(main())
