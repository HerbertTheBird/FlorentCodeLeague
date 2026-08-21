#!/usr/bin/env python3
"""Import cost of the chip tables: every format, cold and warm.

Cold = no __pycache__, so the source is compiled. Warm = .pyc present.
The distinction is the whole argument. A decode loop pays its cost on EVERY
import; a source literal pays it ONCE, at first compile, and then the .pyc makes
it nearly free. Which format wins therefore depends entirely on whether the
platform reuses __pycache__ between games -- so measure both and say so.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
BUILD = BOT / "build"

OLD = f"""
import sys, time, zlib
sys.path[:0] = [{str(BUILD / 'precompute')!r}]
start = time.perf_counter()
import chip_precompute, chip_barrier_data, chip_bestmove_data
chip_precompute._dec(zlib.decompress(chip_barrier_data.ZDATA), 0)
chip_precompute._dec(zlib.decompress(chip_bestmove_data.ZDATA), 0)
print(round(1000 * (time.perf_counter() - start), 1))
"""

NEW = f"""
import sys, time
sys.path.insert(0, {str(BOT)!r})
start = time.perf_counter()
import chip_tables
chip_tables.BFILTER, chip_tables.BESTMOVE
print(round(1000 * (time.perf_counter() - start), 1))
"""

CACHES = [BOT / "__pycache__", BUILD / "precompute" / "__pycache__"]


def run(script: str) -> float:
    out = subprocess.run([sys.executable, "-c", script],
                         capture_output=True, text=True, cwd=BOT)
    if out.returncode:
        raise SystemExit(out.stderr[-2000:])
    return float(out.stdout.strip().splitlines()[-1])


def measure(script: str, runs: int = 3) -> tuple[float, float]:
    for cache in CACHES:
        shutil.rmtree(cache, ignore_errors=True)
    cold = run(script)                       # first run compiles and writes .pyc
    warm = min(run(script) for _ in range(runs))
    return cold, warm


label = sys.argv[1] if len(sys.argv) > 1 else "generated"
old_cold, old_warm = measure(OLD)
new_cold, new_warm = measure(NEW)
size = (BOT / "chip_tables.py").stat().st_size / 1e6
print(f"{'format':28s} {'source':>9s} {'cold':>9s} {'warm':>9s}")
print(f"{'zlib + recursive codec':28s} {1.35:>7.2f} MB {old_cold:>7.1f} ms {old_warm:>7.1f} ms")
print(f"{label:28s} {size:>7.2f} MB {new_cold:>7.1f} ms {new_warm:>7.1f} ms")
