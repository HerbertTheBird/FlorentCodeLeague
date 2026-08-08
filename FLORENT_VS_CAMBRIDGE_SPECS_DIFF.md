# Florent Code League ("Titan") vs. Cambridge Battlecode — Spec Diff

Authoritative diff of the two games. As of **2026-08-01** the local engine, the public docs, and
runtime behavior all agree for Florent (see "Engine vs. rules" below), so this is a clean two-way
comparison.

## Sources
- **Cambridge** `[cam]` — `CAMBRIDGE_SPECS.md`, a full export of `docs.battlecode.cam` (`from cambc
  import`, `MAX_TURNS = 2000`). Complete & authoritative.
- **Florent** `[flo]` — the public docs at **`game.code.florent.vc/docs`** (`/game-rules-reference`,
  `/api-types`, `/game-rules-overview`), cross-checked against the **`fcode 2.3.3` engine** (probed
  at runtime). These now match.

## Engine vs. rules (consistency check)
**They agree.** `fcode 2.3.3` (rebuilt 2026-08-01) matches the published Florent rules on every axis
I checked — 9 entities, titanium-only, cardinal movement, global ammo pool, 2×2 core, 1000 rounds,
16-slot store, and the exact per-type scaling weights. This resolves the earlier mismatch: engine
**2.2.0** had *per-turret conveyor-fed ammo* and *8-directional movement* (Cambridge-style); **2.3.3
switched to a global ammo pool and cardinal-only movement**, catching up to the spec. Verified at
runtime: `convert_ammo(20)` → global ammo 0→20, titanium −20 (1:1); diagonal `can_move`=False and
`move(diagonal)` raises `GameError`.

**Headline:** Florent is a **stripped-down, rebalanced Cambridge** — the whole axionite economy and
6 building types are removed, the core shrinks 3×3→2×2, and comms change — **plus two mechanics
Florent deliberately redesigned**: movement is now **cardinal-only** and turret ammo is a **global
titanium-converted pool** (Cambridge is 8-directional with per-turret conveyor-fed ammo).

---

## TL;DR

| Area | Cambridge `[cam]` | Florent `[flo]` |
|---|---|---|
| Resources | Titanium + raw + refined **axionite** | **Titanium only** |
| Entity types | 15 | **9** (removed: breach, bridge, armoured conveyor, foundry, road, marker) |
| **Movement** | **8-directional** | **Cardinal only** (diagonal → `GameError`) |
| Empty tiles | need a **road** to walk on | **directly walkable** |
| **Turret ammo** | **per-turret**, conveyor-fed (Ti *or* refined Ax) | **global pool**, `convert_ammo()` Ti→ammo 1:1 at core |
| **Core** | **3×3**, `get_position()`=centre, spawn on 9 tiles | **2×2**, spawn radius² 2 |
| Comms | tile **markers** (`place_marker`, u32, 1/round) | **16-slot per-team store** (`read/write_store`) |
| Rounds / game | 2000 | **1000** |
| Map size | 20×20 – 50×50 | **8×8 – 30×30** |
| Match format | best-of-5 | best-of-5 |
| Tiebreakers | ax collected → ti collected → harvesters → ax stored → ti stored → coinflip | ti collected → harvesters → ti stored → coinflip |

---

## 1. Removed in Florent

**Axionite economy** — raw & refined axionite gone (`ResourceType` = titanium only; `Environment`
drops `ORE_AXIONITE`). With it: `convert` (Ax→Ti), the gunner's **25-dmg axionite shot**, the
sentinel **refined-Ax stun (+5 cd)**, and the whole refining chain.

**6 entity types** (Cambridge 15 → Florent 9):

| Removed | Cambridge role `[cam]` |
|---|---|
| **BREACH** | Splash turret — dmg 40 / splash 20, refined-Ax ammo (5), HP 60. Florent turrets = gunner/sentinel/launcher only |
| **FOUNDRY** | Ti + raw Ax → refined Ax; HP 50, 40 Ti, **+50% scaling** |
| **ARMOURED_CONVEYOR** | Conveyor, HP 50, immune to builder attacks; 5 Ti + 5 Ax |
| **BRIDGE** | Long-range conveyor, `build_bridge(pos, target)`, output within dist² 9; HP 20, 20 Ti |
| **ROAD** | Cheapest building (1 Ti, HP 4) — **unnecessary in Florent since empty tiles are walkable** |
| **MARKER** | Comms primitive — replaced by the 16-slot store |

---

## 2. Changed mechanics (Florent diverges from Cambridge)

- **Movement — 8-dir → cardinal-only.** Cambridge `move()` takes any of 8 compass directions.
  Florent: builders move only N/S/E/W; a diagonal raises `GameError` / `can_move`→False. Diagonals
  remain valid for **turret facing**. *(Verified in engine 2.3.3.)*
- **Passability — roads → empty walkable.** Cambridge: empty isn't walkable; place a road (1 Ti).
  Florent: `EMPTY` is *"traversable by builder bots and buildings."*
- **Turret ammo — per-turret → global pool.** Cambridge: turrets are fed a resource (Ti or refined
  Ax) via conveyor and *"only accept resources when completely empty."* Florent: one **team-wide
  ammo balance**, filled only by the **core** via `convert_ammo(amount)` (Ti→ammo **1:1, ≤once per
  turn**); `get_global_ammo`, `can_convert_ammo`. Per-shot costs unchanged (gunner 2, sentinel 10),
  drawn from the pool. *(Verified in engine 2.3.3.)*
- **Core — 3×3 → 2×2.** Cambridge: 9 tiles, `get_position()`=centre. Florent: 2×2, spawn radius² 2.
  Core `convert` changes meaning: Cambridge refined-**Ax→Ti** (1:4); Florent **Ti→ammo** (1:1).
- **Comms — markers → store.** Cambridge: one u32 marker/round (a MARKER entity on the map).
  Florent: **16 int slots (0–15)**, per-team, buffered one round. NOTE: comms are *not visible until 
  next round, even to the writer*. If a bot writes to comms, no other bot will see until the following
  turn.
- **Length / maps / results:** rounds 2000 → **1000**; maps 20–50 → **8–30**; tiebreakers drop the
  two axionite tiers (ti collected → harvesters → ti stored → coinflip). Both end on core
  destroyed / resign first, best-of-5.
- **Cooldown:** Even though we retain separate action and move cooldowns, both must be 0 for a builder
  bot to move or act. Hence, we cannot do both on the same turn.

---

## 3. Unchanged (identical in both)

- **Constants:** `MAX_TEAM_UNITS 50`, `STACK_SIZE 10`, `STARTING_TITANIUM 500`, passive **10 Ti / 4
  rounds**, `ACTION_RADIUS_SQ 2`, `CORE_ACTION_RADIUS_SQ 8`, core HP 500 / vision² 36, best-of-5.
- **Cost scaling — same model *and* same weights.** `effective_cost = base × scale`, scale starts
  1.0 and rises additively per build (falls on destroy). Weights are identical for every shared
  entity: **conveyor/splitter/barrier +1%, harvester +5%, gunner/launcher +10%, builder/sentinel
  +20%.** *(Engine-measured values match the docs exactly.)*
- **Turret stats (Ti ammo):** Gunner HP40 / 10 Ti / **dmg10** / ammo2 / cd1 / vision² 13 (forward
  first-obstruction ray). Sentinel HP30 / 30 Ti / **dmg18** / ammo10 / cd3 / vision² 32. Launcher
  HP30 / 20 Ti / **no damage** / cd1 / throw dist²≤26, pickup² 2.
- **Economy:** Harvester on-ore-only, **10 Ti / 4 rounds**, 20 Ti base, HP 30, feeds adjacent
  (first output immediate). Splitter input-from-behind, **round-robin** to the other 3 sides.
  Conveyor 3 Ti / HP 20, stacks of 10, distributed end-of-round, can feed enemy buildings.
- **Builder:** own-tile attack **2 dmg / 2 Ti**; heal **+4 HP / 1 Ti** in action radius; **self-
  destruct deals 0 damage**. Barrier 3 Ti / HP 30, blocks movement + line-of-sight.
- **Framework:** `run(ct)` per unit per round; 8+centre `Direction` enum; symmetric maps;
  `resign` (≤500-char msg); 50-unit cap; CPU budget **10 ms/unit/round** (Florent doc; +5% banked).

---

## 4. Per-number comparison

| Item | Cambridge `[cam]` | Florent `[flo]` |
|---|---|---|
| MAX_TURNS | 2000 | **1000** |
| Core footprint | **3×3** (get_position=centre) | **2×2** |
| Movement dirs | 8 | **4 (cardinal)** |
| Empty tile | needs road | walkable |
| Ammo model | per-turret (Ti / refined Ax) | **global pool, Ti→ammo 1:1 at core** |
| Map size | 20×20–50×50 | **8×8–30×30** |
| Passive Ti | 10 / 4 rounds | 10 / 4 rounds |
| Harvester | 10 Ti/4 rnd, 20 Ti, +5% scale | 10 Ti/4 rnd, 20 Ti, +5% scale |
| Gunner | HP40/10 Ti/dmg10 (25 w/Ax)/ammo2/cd1/+10% | HP40/10 Ti/dmg10/ammo2/cd1/+10% |
| Sentinel | HP30/30 Ti/dmg18/ammo10/cd3 (+5 stun w/Ax)/+20% | HP30/30 Ti/dmg18/ammo10/cd3/+20% |
| Launcher | HP30/20 Ti/no dmg/cd1/throw²26 | HP30/20 Ti/no dmg/cd1/throw²26/+10% |
| Breach | HP60/15 Ti+10 Ax/dmg40+splash20/ammo5 | **removed** |
| Comms | markers (u32, 1/round) | **16-slot store (0–15)** |
| Tiebreak tiers | ax coll, ti coll, harv, ax stored, ti stored | ti coll, harv, ti stored |

---

*`[cam]` from `CAMBRIDGE_SPECS.md`; `[flo]` from `game.code.florent.vc/docs` cross-checked against the
`fcode 2.3.3` engine (runtime-probed). Supersedes the earlier three-way split — the engine now
tracks the published rules, so "site" and "engine" are one and the same for Florent.*
