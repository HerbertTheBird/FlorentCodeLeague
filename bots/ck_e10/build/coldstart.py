#!/usr/bin/env python3
"""Cold-start import cost of the chip tables, old path vs generated.

Measured the way the platform sees it: a fresh interpreter with no __pycache__,
so the cost of COMPILING the source literal is counted, not just executing it.
Timing the two inside one process (as an earlier version of verify.py did)
flatters whichever module was imported first, because its compile had already
happened before the clock started.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

BUILD = Path(__file__).resolve().parent
BOT = BUILD.parent

OLD = """
import sys, time, zlib
sys.path[:0] = [%r, %r]
start = time.perf_counter()
import chip_precompute, chip_barrier_data, chip_bestmove_data
b = chip_precompute._dec(zlib.decompress(chip_barrier_data.ZDATA), 0)[0]
m = chip_precompute._dec(zlib.decompress(chip_bestmove_data.ZDATA), 0)[0]
print(round(1000 * (time.perf_counter() - start), 1))
""" % (str(BUILD / "precompute"), str(BOT))

NEW = """
import sys, time
sys.path.insert(0, %r)
start = time.perf_counter()
import chip_tables, chip_lookup
b, m = chip_tables.BFILTER, chip_tables.BESTMOVE
print(round(1000 * (time.perf_counter() - start), 1))
""" % str(BOT)


def cold(script: str, runs: int = 3) -> float:
    times = []
    for _ in range(runs):
        for cache in (BOT / "__pycache__", BUILD / "precompute" / "__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        out = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, cwd=BOT)
        if out.returncode:
            raise SystemExit(out.stderr[-2000:])
        times.append(float(out.stdout.strip().splitlines()[-1]))
    return min(times)


old, new = cold(OLD), cold(NEW)
print(f"cold import, best of 3, no __pycache__:")
print(f"  old  zlib + recursive codec + 796-line solver : {old:7.1f} ms")
print(f"  new  generated literals + one zlib call       : {new:7.1f} ms")
print(f"  {'faster' if new < old else 'SLOWER'} by {abs(old - new):.1f} ms "
      f"({100 * (old - new) / old:+.0f}%)")
