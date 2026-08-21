#!/usr/bin/env python3
"""Render herbert3's generated runtime modules.

    python3 build/render.py            regenerate chip_lookup.py and chip_tables.py
    python3 build/render.py --report   print the sizes the templates quote

Everything under build/ is build-time only. The bot is bundled by following
top-level imports from main.py, so nothing in here reaches the platform; that is
the whole point of the split.
"""

from __future__ import annotations

import argparse
import ast
import sys
import time
import zlib
from pathlib import Path

BUILD = Path(__file__).resolve().parent
BOT = BUILD.parent
sys.path.insert(0, str(BUILD))
sys.path.insert(0, str(BUILD / "precompute"))

from closure import closure                                    # noqa: E402
from jinja2 import Environment, FileSystemLoader, StrictUndefined   # noqa: E402

# The five names units/states/chip.py actually calls.
ROOTS = {"ALL_CELLS", "DIAGONALS", "bestmove_lookup", "wins_barrier"}


def load_tables():
    """The two tables, decoded once here at BUILD time using the solver's codec."""
    import chip_precompute as solver
    import chip_barrier_data
    import chip_bestmove_data
    barrier, _ = solver._dec(zlib.decompress(chip_barrier_data.ZDATA), 0)
    bestmove, _ = solver._dec(zlib.decompress(chip_bestmove_data.ZDATA), 0)
    return barrier, bestmove


def render_lookup(env) -> str:
    source = (BUILD / "precompute" / "chip_precompute.py").read_text()
    keep, imports = closure(source, set(ROOTS))
    return env.get_template("chip_lookup.py.j2").render(
        solver_lines=len(source.splitlines()),
        roots=sorted(ROOTS),
        kept=len(keep),
        imports=[ast.unparse(node) for node in imports],
        blocks=[ast.unparse(node) for node in keep],
    )


KEY_SPACE = 4 ** 4 * 16      # 4 cardinal codes of 2 bits, plus a 4-bit mask
ABSENT = 255


def pack_key(key) -> int:
    """((c0, c1, c2, c3), diagonal_mask) -> one int below KEY_SPACE."""
    (c0, c1, c2, c3), dmask = key
    assert 0 <= min(c0, c1, c2, c3) and max(c0, c1, c2, c3) <= 3
    assert 0 <= dmask <= 15
    return c0 << 10 | c1 << 8 | c2 << 6 | c3 << 4 | dmask


def intern(seen: dict, values: list, item) -> int:
    """Index of `item` in `values`, appending it the first time it is seen."""
    text = repr(item)
    if text not in seen:
        seen[text] = len(values)
        values.append(text)
    return seen[text]


def render_tables(env, barrier, bestmove, literal: bool = False) -> str:
    # Barrier: one dense byte per possible key. The table covers 3,840 of the
    # 4,096 keys, so a dense index wastes 256 bytes and buys a format the
    # compiler reads as a single literal.
    seen: dict = {}
    bvalues: list[str] = []
    index = bytearray([ABSENT]) * KEY_SPACE
    for key, value in barrier.items():
        index[pack_key(key)] = intern(seen, bvalues, value)
    assert len(bvalues) < ABSENT, "value index no longer fits in a byte"

    # Best-move: dedupe the metadata, concatenate the payloads.
    mh_seen: dict = {}
    maxhalfs: list[str] = []
    tl_seen: dict = {}
    tlists: list[str] = []
    blob = bytearray()
    mmeta: list[str] = []
    for key, (halfs, tlist, buf) in bestmove.items():
        start = len(blob)
        blob += buf
        mmeta.append(repr((pack_key(key),
                           intern(mh_seen, maxhalfs, halfs),
                           intern(tl_seen, tlists, tlist),
                           start, len(blob))))

    return env.get_template("chip_tables.py.j2").render(
        b_keys=f"{len(barrier):,}",
        b_space=KEY_SPACE,
        b_distinct=len(bvalues),
        absent=ABSENT,
        m_keys=f"{len(bestmove):,}",
        m_raw_mb=f"{len(blob) / 1e6:.1f}",
        m_literal_mb=f"{len(repr(bytes(blob))) / 1e6:.0f}",
        bvalues=bvalues,
        bindex=repr(bytes(index)),
        maxhalfs=maxhalfs,
        tlists=tlists,
        needs_zlib=not literal,
        moves_expr=(repr(bytes(blob)) if literal
                    else f"zlib.decompress({zlib.compress(bytes(blob), 9)!r})"),
        mmeta=mmeta,
    )


def _time_ms(fn) -> float:
    start = time.perf_counter()
    fn()
    return 1000 * (time.perf_counter() - start)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--literal", action="store_true",
                        help="emit the best-move payloads as one raw bytes\n                              literal instead of a packed blob -- no zlib at\n                              all, at the cost of a ~93 MB source file")
    args = parser.parse_args()

    env = Environment(
        loader=FileSystemLoader(BUILD / "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    barrier, bestmove = load_tables()

    if args.report:
        blob = b"".join(buf for _mh, _tl, buf in bestmove.values())
        print(f"barrier   {len(barrier):>7,} keys  "
              f"{len(set(map(repr, barrier.values()))):>4} distinct  "
              f"repr {len(repr(barrier)) / 1e6:>7.1f} MB")
        print(f"best-move {len(bestmove):>7,} keys  "
              f"payloads {len(blob) / 1e6:>6.1f} MB  "
              f"as a literal {len(repr(blob)) / 1e6:>6.1f} MB  "
              f"zlib {len(zlib.compress(blob, 9)) / 1e3:>7.1f} KB")
        return

    for name, text in (("chip_lookup.py", render_lookup(env)),
                       ("chip_tables.py", render_tables(env, barrier, bestmove,
                                                        literal=args.literal))):
        path = BOT / name
        path.write_text(text)
        print(f"wrote {path.relative_to(BOT.parent)}  {len(text) / 1e6:.2f} MB")


main()
