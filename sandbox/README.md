# Florent sandbox

A **self-contained, steppable, editable re-implementation** of the Florent
("Titan") game engine, plus an interactive pygame viewer. It runs the *real*
competitor bots turn-by-turn against a live board you can edit for free, and the
bots adapt to your edits on their next turn.

Why it exists: the packaged `fcode_engine.so` only exposes `run_game(...)` — a
whole match, atomic, no stepping or mid-game mutation. This engine is a
ground-up rewrite so tooling can step and edit.

## Run

```bash
python sandbox/run.py                      # Heimdall_opening vs itself on nordkap
python sandbox/run.py --map valkyrie --a Heimdall_opening --b Heimdall_v6 --seed 3
```
`--a` / `--b` take a bot dir or a bare name resolved against `./bots`.
`--map` takes a map name (resolved against `./maps`, `.map26` optional).

Requires `pygame` and (for the map image tool) `pillow` — both installed in `.venv`.

## Controls

| key | action |
|-----|--------|
| **Space** | step ONE unit's turn |
| **Enter** | step a whole round (all units + end-of-round resource resolution) |
| **1 / 2 / 3 / 4** | build conveyor / barrier / harvester / splitter — **team A** |
| **5 / 6 / 7 / 8** | build conveyor / barrier / harvester / splitter — **team B** |
| **9** | delete (building or bot) |
| **q** | attack tool — click does −2 HP |
| **e** | heal tool — click does +4 HP |
| **W A S D** / arrows | rotate the ghost's facing (conveyor/splitter) |
| **left click** | apply the selected tool at the tile (free, ignores cost/cooldown/adjacency) |
| **Cmd/Ctrl+Z** | undo the last edit |
| **R** or the **Save replay** button | write the session so far to `sandbox_r<round>.replay26` |
| **Esc** | quit |

The saved replay is the real `.replay26` wire format, so watch it in the 2D viewer:

```bash
./run2d sandbox_r40.replay26        # or: python3 tools/watch2d.py sandbox_r40.replay26
```
It records your edits too. (Belt titanium-on-conveyor animation isn't emitted, but
entities/positions/types/facings/hp/economy all are.)

A semi-transparent ghost of the selected tool follows the cursor before you
click. The next unit to act (on Space) is outlined in yellow. The right-hand HUD
shows round, both teams' titanium / ammo / units / scale / collected, and the
current tool.

## Files

- `fcode_shim.py` — API-compatible `fcode` types (Direction/Position math, enums,
  `GameConstants`). Injected as `sys.modules['fcode']` so unmodified bots import it.
- `engine.py` — `Engine` (state + all mechanics) and `UnitController` (the 65
  Controller methods the bots call). Includes `god_place/god_delete/god_damage/
  god_heal` for the free edits.
- `botrunner.py` — per-unit bot isolation. Bots keep per-unit state in *module
  globals*, so each unit gets its own module graph via a fresh import that's
  detached from `sys.modules`. `Match` owns the engine + per-unit players and
  exposes `step_unit()` / `step_round()`.
- `mapio.py` — `.map26` parser → (width, height, terrain, cores).
- `viewer.py` — the pygame window.
- `run.py` — CLI entry.

## Fidelity — MEASURED against the real engine (`calibrate.py`)

This was validated by diffing the sandbox against the compiled engine round-by-round
(decode its replay, reconstruct per-turn state, compare entity positions/types +
titanium). Run it yourself:

```bash
python sandbox/calibrate.py nordkap:1 eider:1 duel:1     # first-divergence round per game
```

**What holds:** the sandbox reproduces the real engine's board **exactly for the
opening — the first ~9–30 rounds** (varies by map, avg ~16): identical entity
positions, types, and both titanium balances, every round. That validates the core
mechanics — terrain, **vision (incl. the 2×2 core's true 42.5-from-centre reach)**,
build/destroy/move/heal, cooldowns, **integer cost-scaling**, comms (one-round
buffer), spawning, passive titanium, ammo conversion, and the early resource flow.

**Where it diverges:** after the opening, two things drift and then cascade:
1. **Belt timing** — the harvester→conveyor→core shift is one-hop-per-round with
   single-tile occupancy, but the real engine's exact stack tie-breaks differ, so a
   stack (±10 ti) lands a round or two early/late. That ±10 changes a build decision,
   which snowballs.
2. From there the games become **different games**, so the mid/late phase (sieges,
   combat) doesn't match. Full-game **winner agreement is ~3/10** across maps —
   several sandbox games stall to a 1000-turn draw where the real engine ended by
   core-destroyed, because the diverged trajectory never mounts the same rush.

**Bottom line:** great for stepping through and editing the **opening / economy /
adaptation** behaviour (which is faithful); **not** a bit-exact match simulator —
don't use it to predict ladder outcomes. Closing the gap fully needs the compiled
engine's exact belt + combat algorithms (a closed binary), not something recoverable
by guessing. `calibrate.py` makes any future mechanic fix measurable (does the
first-divergence round move later?).

Other simplifications: combat (gunner ray / sentinel line / launcher) is modelled
but unverified; CPU-time limits are ignored (`get_cpu_time_elapsed` → 0). Bots that
raise inside `run()` are caught (as in the real engine); see `Match.errors`.
