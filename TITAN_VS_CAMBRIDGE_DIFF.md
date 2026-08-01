# Titan (Florent Code League) vs. Cambridge Battlecode — Full Engine Diff

> ⚠️ **VERSION WARNING (2026-07-24).** Everything below was verified against the **local pip
> engine `fcode 2.2.0`** (latest on public PyPI). The **official game site**
> (`game.code.florent.vc`) — the same host the CLI submits bots to — publishes tutorials that
> describe a **DIFFERENT, incompatible version** of the game. Confirmed contradictions (verbatim
> from the site's tutorial code):
> - **Movement:** site says builders move **cardinal-only** (diagonal → `GameError`); the 2.2.0
>   engine allows **8-directional** movement (probe-verified).
> - **Ammo:** site says ammo is a **team-wide pool** filled at the Core via
>   `ct.convert_ammo()` / `ct.can_convert_ammo()` / `ct.get_global_ammo()` (1:1, once/turn);
>   the 2.2.0 engine has **per-turret, conveyor-fed** ammo and **none of those three methods
>   exist** (absent from the binary).
>
> The pip package (CLI + engine `.so` + bundled starter bot) is fully self-consistent with the
> "2.2.0" rules below, so this is a real fork, not a doc typo. **This doc treats local 2.2.0 as
> authoritative** (per project owner). The site's differing movement/ammo are catalogued in
> **`THREE_WAY_DIFF.md`** (Cambridge ↔ Site ↔ Local). If you ever submit to the ranked server, the
> one thing worth confirming is that the server runs these 2.2.0 rules and not the site's.

Titan is a stripped-down, rebalanced descendant of the game in the *Cambridge Battlecode
Postmortem* (Team Pantheon / bot "Khaos"). **Khaos is a Cambridge Battlecode bot**, so its
code and that postmortem describe CamBC's rules — not this engine. This document is the
verified spec of the **local `fcode 2.2.0` engine** (see version warning above), plus the diff
vs. CamBC.

## Provenance & method
- **Engine:** `fcode` **v2.2.0**; simulator is compiled Rust crate **`battlecode_titan`**
  (`fcode/fcode_engine.cpython-312-darwin.so`, arm64). The `fcode` Python package is a CLI +
  ladder client. Matches run fully locally (`fcode run`).
- **Source origin:** embedded panic paths reveal it builds from a **private repo
  `battlecode-platform`** at `engine/rust/src/` (`game.rs`, `game/{build,distribute,turret}.rs`,
  `game_map.rs`, `map_loader.rs`, `runner{,/watchdog}.rs`, `bindings/controller.rs`). The wheel
  ships **compiled machine code only — no Rust source** (release build, no DWARF). PyPI would
  not change this; only the private repo "opens the code."
- **How each fact was obtained** (tagged below):
  - `[const]` = `GameConstants` in `_types.py` (Python, authoritative).
  - `[probe]` = measured live (a bot inspects `Controller`, exfiltrates via `resign()` / the store).
  - `[asm]` = read from the binary (symbols via `nm`, logic via `objdump` disassembly, `strings`).
  - `[doc]` = stub docstring — **treated as suspect** (some are stale CamBC docs; see ⚠️).
- **API surface:** the runtime `Controller` **exactly matches** the `_types.py` stub `[probe]`
  (0 extra methods, 0 missing) — signatures are trustworthy; only *docstring behavior* can be stale.

---

## TL;DR diff

| Area | Cambridge Battlecode | Titan |
|---|---|---|
| Resources | Titanium + raw + refined axionite | **Titanium only** |
| Axionite layer (ore, foundry, Ax→Ti core conv, breach) | Yes | **Removed** `[asm]` |
| Removed buildings | — | **road, bridge, armoured conveyor, foundry, breach** `[asm]` |
| Communication | Tile **markers** (1/turn) | **16-slot global store** `[const][probe]` |
| Core | 3×3, walkable by allies | **2×2, non-walkable** `[probe]` |
| Rounds | 2000 | **1000** `[const]` |
| Turn budget | 2000 µs | **10 ms** server TLE `[probe]` |
| Map size | 20×20–50×50 | **10×10–28×20** `[probe]` |
| Tiebreaks | ax → ti → harvesters → ax stored → ti stored | ti-based **+ coinflip** `[asm]` |

---

## 1. Removed vs. Cambridge Battlecode
Confirmed with **0 occurrences** in the binary `[asm]` and absent from the enums `[const]`:
- **Axionite** (raw + refined): `ResourceType` = `{TITANIUM}` only; `Environment` = `{EMPTY,
  WALL, ORE_TITANIUM}`. No axionite ore.
- **Foundry**, **core Ax→Ti conversion**, **Breach turret**, **Bridge**, **Armoured conveyor**,
  **Road**, **Markers**.
- ⚠️ **Road was removed because it is unnecessary:** in Titan **empty tiles are directly walkable**
  (see Movement below), so no road building is needed to traverse open ground. (This corrects an
  earlier claim in this doc that walkability required conveyors/splitters — it does not.)

Titan entity set `[const]`: `CORE, BUILDER_BOT, GUNNER, SENTINEL, LAUNCHER, CONVEYOR, SPLITTER,
HARVESTER, BARRIER`.

---

## 2. Titan spec (engine-verified)

### Map
- Binary protobuf `Map { width, height, cores[] }`; "map must have a core for each team" `[asm]`.
- Shipped sizes `[probe]`: sprint 10², duel 12², crossfire 16², atoll 18², pinch 14×18,
  fjord 20², twins 21², skerry 22², quarry/runestone/vault 24², hive 25², aurora 26²,
  strait 20×26, longship 28×20.
- **Symmetry is guaranteed** (H, V, or rotational) — verified by parsing every map's two core
  positions `[probe]`: 10 rotational (atoll, aurora, crossfire, duel, fjord, hive, quarry, skerry,
  sprint, vault), 1 horizontal (longship), 3 vertical (pinch, strait, twins), 1 both H+ROT
  (runestone). Same guarantee as CamBC.

### Core
- **2×2 footprint** (4 tiles) `[probe]` — *not* 3×3. `get_position()` returns the min-corner
  (top-left) tile `[probe]`.
- **Not walkable/passable by anyone** `[probe]`: from the core, `is_tile_passable=False` on all
  4 tiles; from an adjacent builder, `passable=False, empty=False, can_move=False`.
  ⚠️ The `is_tile_passable` **docstring is wrong** — it claims the allied core is passable `[doc]`.
- Immobile; spawns 1 builder/turn on an adjacent tile (`CORE_SPAWNING_RADIUS_SQ = 2` `[const]`).
  HP 500 `[const]`; vision² 36 measured **from the nearest of its 4 footprint cells** (footprint-relative,
  not from the top-left anchor) `[probe]`.
- **Fog of war** `[probe]`: reading `get_tile_env`/`get_tile_building_id`/`get_hp` for a tile/entity
  outside a unit's vision raises `GameError`; `get_nearby_tiles(dist_sq)` with `dist_sq` > the unit's
  vision radius also raises. Builder vision² = 20 (64 tiles), core 36 (82 tiles) — match constants.

### Units vs. buildings (matters for cap + scaling)
- **Units** = core + builder bots + turrets (gunner/sentinel/launcher). These drive cost scaling
  `[probe]` and are capped at **50** (`MAX_TEAM_UNITS` `[const]` — the cap itself was *not* reached in
  probes, always titanium-limited first, so the 50 boundary is `[const]`-asserted, not `[probe]`-observed).
- **Buildings** = conveyor, splitter, harvester, barrier — **not** counted toward units, **not** capped `[probe]`.

### Cost scaling `[probe]`
```
scale_percent = 100 + 20 * (unit_count - 1)
cost          = floor(base_cost * scale_percent / 100)
```
Verified for **all 8 building types** across unit_count 1–11 `[probe]`, exact match every step
(`get_scale_percent` == 100+20·(units−1)). E.g. at units 1/3/6/11 → scale 100/140/200/300%; gunner
10/14/20/30, sentinel 30/42/60/90, harvester 20/28/40/60, splitter 6/8/12/18, conveyor & barrier
3/4/6/9, builder 30/42/60/90.

### Economy `[probe]` unless noted
- Starting titanium **500** `[const]`.
- **Passive income: +10 every 4 rounds** (lands on rounds ≡0 mod 4).
- **Harvester: 1 stack every 4 rounds**, per harvester (verified on quarry/aurora/vault:
  production rounds 5,9,13,… intervals all 4). Outputs to a cardinally adjacent building.
- **A resource stack = 10 titanium** `[asm]` (`receive_resource` sets the stack field to `#0xa`;
  `STACK_SIZE = 10` `[const]`).
- **Conveyors advance a stack exactly 1 tile/round** toward their facing tile; **accept from the 3
  non-facing cardinal sides and reject from the facing side** `[probe]` (fed from each side: facing
  side never loaded; other three did). Hop isolated: a stack lands on A at round R, reaches B at R+1.
  A **terminal conveyor (no downstream receiver) holds its stack and stalls** — the upstream
  harvester then can't push, so it caps `[probe]`.
- **Splitter**: accepts from the **back** (side opposite its facing) **only** — feeding any other side
  is rejected `[probe]`; outputs to the other **3 cardinal sides** (front + both perpendiculars), one
  stack per side in **round-robin** `[probe]`.
- **Core accepts a conveyor feeding into any of its 4 sides** → team titanium rises `[probe]`.
- **Multi-upstream arbitration** `[probe]`: when two loaded conveyors point into one tile with a
  single receiver, the target takes **one stack per round**, **never loses** the other (it stays on
  its conveyor as back-pressure and is delivered later), and the choice is **deterministic /
  priority-based, not fair round-robin** (one upstream is consistently favored, the other starved).
- Conveyors/splitters face **cardinal directions only** `[doc][probe]`.
- Resources become globally spendable only after reaching the **core** `[doc]`.

### Movement & passability `[probe]` ⚠️ (corrects earlier draft)
- Builders move **8-directionally** — diagonals allowed. Verified: a builder stepped NE/etc. and its
  position changed by exactly (±1, ±1). "Cardinal-only" applies to **conveyor/splitter facing and
  I/O**, *not* to movement.
- **Passable (can move onto):** **empty tiles** (no building, not wall), **conveyors**, **splitters**.
  Empty tiles being walkable is why roads were removed.
- **Blocked (cannot move onto):** walls, barriers, harvesters, turrets (gunner/sentinel/launcher),
  the **core** (allied or enemy — the `is_tile_passable` docstring wrongly says allied core is
  passable; measured False), and any tile already holding another builder bot.

### Cooldowns
- Builder **move and action are independent**, **1 each per turn** `[probe]` (a builder moved *and*
  built a barrier in the same turn; both cooldowns went 0→1, back to 0 next round).
- Core spawn: 1/turn (`spawn_builder` costs an action cooldown `[doc]`).
- Gunner: fire cooldown 1, ammo 2/shot, **dmg 10** — all `[probe]` (fired at a barrier: ammo 10→8,
  target HP 30→20); rotate costs **10 Ti**, cooldown 1, must be a *different* compass dir `[const][asm]`.
- Sentinel: fire cooldown 3, ammo 10/shot, **dmg 18** `[probe]` (fired: ammo 10→0, target HP 30→12).
- Builder: attack (own tile only) dmg 2, cost 2 Ti `[probe]` (conveyor 20→18, titanium −2);
  heal **+4 HP**, cost 1 `[const]`. **Self-destruct dmg = 0** `[probe]` (adjacent barrier HP 30→30).

### Ammo `[probe]` (answers a common question)
- **Ammo is NOT global and is NOT converted at the core.** Each turret holds its **own** ammo,
  physically **fed as titanium stacks via a conveyor** from a **non-facing side** (turrets obey the
  same accept-side rule as conveyors — feeding the facing side is rejected). One stack = 10 ammo.
- **Firing draws only from that turret's ammo — global titanium is untouched.** Verified on 3 maps:
  gunner fire → `globalTi 440→440` (Δ0), `ammo 10→8` (Δ−2), target HP 30→20.
- **Launchers use no ammo** (`get_ammo_amount` stays 0; `build_launcher` + `launch` work with 0).

### Combat geometry `[probe]` (via `get_attackable_tiles_from`)
- **Gunner:** straight line, **range 3** (`(1,0),(2,0),(3,0)` facing E); hits only the **first** valid
  target (verified: near barrier hit, far one untouched). Walls block the line and are untargetable;
  builder bots and buildings block **and** are targetable.
- **Sentinel:** forward **3-wide × 5-long band** (17 tiles: `dx 0..5, dy −1..1`, minus own tile);
  can target **any** tile in the band (verified: hit the far tile while the near one was skipped).
- **Launcher:** **omnidirectional**, radius dist² ≤ 26 (~5.1 tiles), 88 tiles; direction ignored.
  Picks up a builder bot **of either team** (ally and enemy both verified) and throws it to a
  **walkable tile in the launcher's vision** — lands exactly on the target tile.

### Other constants `[const]`
- Vision²: core 36, sentinel 32, launcher 26, builder 20, gunner 13.
- HP: core 500; builder 40; gunner 40; harvester 30; sentinel 30; launcher 30; barrier 30;
  conveyor 20; splitter 20.
- Base costs: conveyor 3, barrier 3, splitter 6, gunner 10, harvester 20, launcher 20,
  sentinel 30, builder bot 30. `ACTION_RADIUS_SQ = 2`, `CORE_ACTION_RADIUS_SQ = 8`,
  `STORE_SIZE = 16`.

### Communication `[probe]`
- **16-slot per-team store** of u32 **integers** (`read_store`/`write_store`). Index 0–15 only —
  index 16 raises. Writes are **buffered** → visible to teammates at the **start of next round**
  (same-round read returns the start-of-turn snapshot). No tile markers exist.
- **Per-team isolation:** team B cannot read team A's store (verified: A wrote `0xDEADBEEF`, B always
  read 0).
- **Values must be a u32 int:** `write_store` accepts a Python **int** only (writing a string raises).
  Out-of-range (`2**32` or negative) raises `OverflowError` — it does **not** wrap, despite the
  docstring saying "treated as u32". Max valid value `2**32−1` round-trips.

### Win conditions & results `[asm][probe]` (+ `run.py`)
- Immediate: `core_destroyed`, `resigned`. A one-team `resign()` → the **other team wins**,
  `win_condition='resigned'`; a simultaneous double-resign → `coinflip` `[probe]`.
- Tiebreaks in order: `resources` → `titanium_collected` → `harvesters` (alive) →
  `titanium_stored` → **`coinflip`**; `timeout` = draw. A 1000-round game with a titanium lead
  ended `win_condition='titanium_collected'`, turns≈1000 `[probe]`. `MAX_TURNS = 1000` `[const]`.

### Runtime / platform
- **Rounds are 0-indexed at runtime** `[probe]` (first `run()` sees round 0). ⚠️ `get_current_round`
  docstring says "starts at 1" `[doc]`.
- Turn limit: the **local `run_game` harness does NOT enforce a per-turn time limit** `[probe]` — a
  ~2.5 s/turn busy loop survived at both `tle=0` and `tle=1`, and `get_cpu_time_elapsed()` returns 0
  in this build. The **10 ms** budget applies on the **server** (`--tle`); rely on it there, not locally.
- **`resign()` messages truncate to 500 chars** `[probe]`.
- **Sandbox** `[asm]`: `_thread` import blocked; frame introspection blocked; `datetime`/`time`
  frozen (no wall-clock); no filesystem. `print()` is **suppressed**; **uncaught exceptions print a
  traceback to stderr** (usable for debugging).
- Execution model `[doc][probe]`: each unit gets its own `Player` instance; the engine calls
  `run(ct)` once per unit per round. Entities are stored in SwissTable hashmaps; cooldowns
  decrement once per turn `[asm]`.

---

## 3. Unchanged from Cambridge Battlecode
Core spawns builders; builder bot is the only mover/constructor; titanium economy via
harvesters → conveyors/splitters → core; turrets need ammo fed from non-facing sides; gunner
(first-target line) / sentinel (area) / launcher (throw) roles; 50-unit cap; build-cost scaling
with unit count; win by destroying the enemy core; squared-Euclidean vision; `can_*`/`build_*`/
`get_*_cost` API shape.

---

## 4. Porting Khaos (CamBC → Titan) — required changes
- **Delete** all axionite / foundry / refined-ammo / breach logic and Ax tiebreak plans.
- **Delete** road/bridge/armoured-conveyor placement. Passability is simpler: **empty tiles are
  walkable** (no roads needed); only walls/barriers/harvesters/turrets/cores/other-builders block.
- **Rewrite comms** from marker broadcast (incl. the XOR "encryption") to the 16-slot store
  (per-team, int-only, buffered next-turn). Note store rejects out-of-range/negative values.
- **Fix the core model:** 2×2, non-walkable — any pathing/adjacency/spawn-ring code assuming a
  3×3 walkable core is wrong.
- **Ammo is per-turret and physically fed** (conveyor from a non-facing side), not a global pool —
  turret placement must guarantee a titanium feed line, and firing doesn't spend global titanium.
- **Retune** to: 1000 rounds, 10 ms/turn (server-enforced only), 10×10–28×20 maps,
  `scale% = 100 + 20·(units−1)`, harvester = 10 Ti / 4 rounds, gunner range 3 / sentinel 3×5 band /
  launcher r²≤26.

---

## 5. Confidence notes
Full verification sweep complete. Items closed this pass (all `[probe]`):
- ✅ Movement is 8-directional; empty tiles walkable; exact passable/blocked set.
- ✅ Ammo is per-turret, physically fed, no global cost on fire; launchers use no ammo.
- ✅ Combat numbers (gunner 10/2/cd1/first-target; sentinel 18/10/cd3/any-tile; builder 2 own-tile;
  self-destruct 0) all measured, not just from constants.
- ✅ Conveyor accept sides (3 non-facing); splitter back-only; core accepts on any side; multi-upstream
  = one/round, no loss, deterministic priority.
- ✅ Cost formula for all 8 building types (units 1–11); conveyor 1 tile/round; harvester 10 Ti/4 rounds.
- ✅ Store: 16 slots, per-team isolated, buffered, int-only, out-of-range raises (no wrap).
- ✅ Fog of war (out-of-vision reads raise); vision radii match; core vision footprint-relative.
- ✅ Map symmetry guaranteed (all 15 maps); win/tiebreak labels; local harness ignores TLE.

Genuinely still **not** directly observed (low stakes):
- The **50-unit cap** boundary (`[const]`-asserted; probes were always titanium-limited before 50).
- **Core-destruction** win in practice (`core_destroyed` label exists; not force-triggered).
- Harvester adjacency/output arbitration when it borders multiple accepting buildings.

*Facts tagged `[probe]`/`[asm]`/`[const]` were taken from the running v2.2.0 engine or its binary;
`[doc]` items are from stub docstrings and may be stale (several were found wrong — noted with ⚠️).*
