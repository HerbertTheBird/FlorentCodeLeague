# herbert3 build

Everything in this directory is **build-time only**. The platform bundles a bot by
following top-level imports from `main.py`, so nothing here is uploaded — which is
the point of the split. Before it, `chip_precompute.py` (796 lines of offline
solver) shipped with every submission so that the bot could call four functions
out of it.

    make          regenerate chip_lookup.py and chip_tables.py
    make check    prove the generated tables equal the ones the old codec built
    make report   print the table sizes the templates quote
    python3 coldstart.py    old vs new cold import cost, fresh interpreter

## What is generated, and why it looks like that

`chip_lookup.py` — the four names `units/states/chip.py` calls, plus their
transitive closure (18 definitions), extracted from `precompute/chip_precompute.py`
by `closure.py`. The solver stays the single source of truth; nothing is
hand-copied, so nothing can drift.

`chip_tables.py` — the barrier win-table and the best-move table.

`make literal` does the obvious thing: every table becomes Python source and there
is no decode step at runtime at all. It is not the default, and the reason is a
measurement rather than a preference:

| format | source | cold import | warm import |
|---|---|---|---|
| zlib + recursive codec (what herbert ships) | 1.35 MB | 122.4 ms | 100.1 ms |
| `make packed` — dense literals + one zlib call | 1.62 MB | 123.7 ms | ~40 ms |
| `make literal` — pure source, no zlib | 99.62 MB | 908.7 ms | **32.0 ms** |

Cold means no `__pycache__`, so the source is compiled; warm means the `.pyc`
exists. A decode loop pays on every import; a source literal pays once and is
nearly free afterwards -- so the literal build is three times faster warm and
seven times slower cold, and everything turns on whether bytecode is reused
between games.

**It is not.** The same 86-game suite, identical turn counts, went from a median
22.3 s per game to 24.8 s when the literal build replaced the packed one: about a
second per side per game, which is the cold compile, paid every single game. The
warm column is therefore unreachable in play, and `packed` is the default because
it dominates under that fact -- far better cold, barely worse warm. Flip it with
`make literal` if the platform ever starts caching.

One trap, measured, worth not rediscovering: writing the barrier table as its
natural 3,840 dict-entry literals costs **77 ms to compile and 0.8 ms to run** --
worse than the 39 ms codec it replaced, because the compiler charges per literal.
The same table as one dense 4,096-byte string plus a loop is ~2 ms. Size is not
the thing that matters; the NUMBER of literals is. Both builds use the dense form
for it, and they differ only in the best-move payloads.

## Layout

    build/
      Makefile
      render.py          renders the templates
      closure.py         ast-based "what does the runtime actually reference"
      verify.py          generated tables == originals, byte for byte
      coldstart.py       import cost, old vs new, fresh interpreter
      templates/         chip_lookup.py.j2, chip_tables.py.j2
      precompute/        the offline solver and its dev GUI -- never shipped
