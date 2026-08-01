# Three-Way Rules Diff: Cambridge Battlecode ↔ Official Site ↔ Local Engine

Three sources describe (variants of) this game. This doc diffs all three pairwise.

| Source | What it is | How captured |
|---|---|---|
| **CamBC** | Cambridge Battlecode (the game "Khaos" was written for) | the postmortem PDF `[pm]` |
| **Site** | Official Florent Code League tutorials at `game.code.florent.vc` (the host the `fcode` CLI submits bots to) | fetched tutorial pages `[web]` |
| **Local** | Installed engine `fcode 2.2.0` (latest on public PyPI) — **treated as authoritative/up-to-date** | runtime probes + binary `[probe]/[asm]` |

**Headline:** Site and Local are the *same game* except for **two mechanics — movement and ammo** — where the **Site is the outlier** (it also diverges from CamBC on those two). Everything else the Site quantifies matches Local. CamBC is the older, larger ancestor of both.

---

## Master table (— = not specified by that source)

| Mechanic | CamBC | Site | Local 2.2.0 |
|---|---|---|---|
| Resources | Titanium + raw/refined **axionite** | Titanium only | Titanium only |
| Foundry / Bridge / Armoured conveyor / Breach / **Road** | present | absent | absent |
| Comms | **Tile markers** (1/turn, u32) | 16-slot per-team store | 16-slot per-team store |
| **Core footprint** | **3×3**, walkable by allies | **2×2** | **2×2**, non-walkable |
| Map size | 20×20–50×50 | — | 10×10–28×20 |
| Rounds | 2000 | 1000 | 1000 |
| **① MOVEMENT** | **8-directional** (diagonal; Chebyshev pathing) | **Cardinal only** (diagonal → `GameError`) | **8-directional** (diagonal verified) |
| Passable tiles | empty needs a **road**; conveyor/splitter/core | empty walkable | empty/conveyor/splitter walkable; core & rest blocked |
| **② AMMO model** | **per-turret, conveyor-fed** from non-facing side | **team-wide pool**, converted at Core | **per-turret, conveyor-fed** from non-facing side |
| **② AMMO api** | (feed via conveyor) | `convert_ammo`/`can_convert_ammo`/`get_global_ammo` (1:1, once/turn) | `get_ammo_amount`/`get_ammo_type`; **no convert_ammo** |
| Gunner | high-DPS short line | HP40, cost10, dmg10, ammo2, reload1 | HP40, cost10, dmg10, ammo2, cd1, **range-3 line, first target** |
| Sentinel | low-DPS long/wide | HP30, cost30, dmg18, ammo10, reload3 | HP30, cost30, dmg18, ammo10, cd3, **3×5 band, any tile** |
| Launcher | throws bots, no ammo | HP30, cost20, no damage, reload1 | no ammo; throws either team's bot to walkable in-vision tile, r²≤26 |
| Builder attack | attack a building | dmg2, own tile only, cost2 | dmg2, own tile only, cost2 |
| Heal | heal adjacent | +4 HP, cost1, action radius, bot+building same call | +4 HP, cost1 |
| Self-destruct | (explosion, later removed) | **0 damage** | **0 damage** |
| Harvester | stack every 4 turns | **stack every 4 rounds**, ore only, feeds adjacent | **10 Ti every 4 rounds**, ore only |
| Resource stack | — | **10 Ti** | **10 Ti** |
| Conveyor | 1 tile/turn, 3 non-facing sides | (mechanics only, no #s) | **1 tile/round**, accepts 3 non-facing sides |
| Splitter | back in, 3 sides out | back in, **round-robin** to other 3 | back in, **round-robin** to other 3 |
| Passive income | every 4 turns | **10 / 4 rounds** | **10 / 4 rounds** |
| Cost scaling | scales with build (factor `s`) | ×% up per build, down on destroy (no # given) | **`100 + 20·(units−1)`%**, units=core+builders+turrets |
| Store | (n/a — markers) | 16 slots, per-team, int, buffered next round | 16 slots, per-team, int (u32; out-of-range raises), buffered |
| Starting titanium | — | — (shared pool via `get_global_resources`) | **500** |
| Time limit | 2000 µs/turn | — | server 10 ms; **local harness doesn't enforce** |
| Win / tiebreak | core / ax→ti→harv→ax→ti stored | 1000-round coinflip fallback | core/resign; ti-based tiebreaks + coinflip |

---

## A. Local 2.2.0 ↔ CamBC  (detailed in `TITAN_VS_CAMBRIDGE_DIFF.md`)
Local is a **stripped-down CamBC**: removed axionite economy (foundry, refined ammo, breach),
bridge, armoured conveyor, road, and marker comms; core 3×3→2×2 and no longer walkable; 2000→1000
rounds; smaller maps; markers→16-slot store; +coinflip tiebreak. **Same** on the two axes the Site
changes: 8-directional movement and per-turret conveyor-fed ammo both carry over from CamBC to Local.

## B. Local 2.2.0 ↔ Site  ← the important one
Nearly identical games. **Only two real differences, both on the Site side:**
1. **Movement.** Site = cardinal-only (diagonal raises `GameError`, `can_move` False). Local =
   8-directional (diagonal move verified, exact (±1,±1) displacement).
2. **Ammo.** Site = one **team-wide ammo pool**, filled by the Core with `ct.convert_ammo(amount)`
   (1:1, ≤once/turn), every turret firing from the shared pool. Local = **per-turret ammo physically
   fed by a conveyor** from a non-facing side; `convert_ammo`/`can_convert_ammo`/`get_global_ammo`
   **do not exist** in the engine (absent from the binary), and firing spends turret ammo, not
   global titanium.
- Minor: Site says `print()` is captured and shown in the visualiser; Local's `run_game` suppresses
  it to stdout. Everything else the Site quantifies (turret stats, harvester 10/4, splitter
  round-robin, builder attack/heal/self-destruct, store, passive income, environment, core 2×2)
  **matches Local exactly**.
- ⚠️ Because the CLI submits to the Site's host, if the *server* runs the Site rules, a Local-tuned
  bot would break there (diagonal moves rejected; turrets never armed without `convert_ammo`). Per
  your call this doc treats Local as authoritative — but that mismatch is the one thing to confirm
  before relying on local testing for ranked play.

## C. Site ↔ CamBC
Site is a **further-simplified CamBC**, and on the two mechanics above it diverges from CamBC in the
*opposite* direction from Local:
- Same removals as Local (no axionite/foundry/bridge/road/breach; 2×2 core; store instead of markers;
  1000 rounds).
- **Movement:** CamBC was 8-directional (diagonal Chebyshev pathfinding, "prefer diagonals"); Site
  restricts to **cardinal-only** — a Site-only change.
- **Ammo:** CamBC fed turrets via conveyors from a non-facing side (per-turret); Site replaces this
  with a **global convert-at-Core pool** — also a Site-only change.
So the Site simplified *two* CamBC systems (movement + ammo) that the Local engine kept faithful.

---

*Sources: `[pm]` = Cambridge postmortem PDF; `[web]` = `game.code.florent.vc` tutorials (fetched
2026-07-24; several pages give mechanics but not exact numbers — marked "—"); `[probe]/[asm]` =
measured from the running `fcode 2.2.0` engine / its binary. See `TITAN_VS_CAMBRIDGE_DIFF.md` for the
full engine-verified Local spec.*
