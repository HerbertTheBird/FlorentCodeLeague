#!/usr/bin/env python3
"""Prove a hardcoded opener is legal before a game is ever played.

`bots/<bot>/openers.py` names exact tiles -- spawn here, build a launcher there,
throw that builder to this tile, walk to that one and put a barrier beside it. Every
one of those is a claim about terrain, and a wrong tile does not raise: the bot's
can_*() gate just returns False and the unit sits there doing nothing until the
opener times out, which in a replay looks like the bot being idle for no reason.
Reading the coordinates off a rendered map by eye is exactly the process that
produces those, so this replays the whole script against the real .map26 instead.

What it checks, per map and for BOTH sides (the mirrored side is a different set
of tiles and gets the same scrutiny):

  * every spawn tile is in the core's spawn ring and empty
  * every build target is orthogonally adjacent to where the builder will actually
    be standing when it gets there, in bounds, and not wall/ore/occupied
  * every launch is in range (r^2 <= 26), picks up from a tile a builder is
    really on, and lands on a passable tile
  * every GOTO is reachable, and the walk does not run through a tile the opener
    has already barriered shut
  * the whole thing is affordable from 500 titanium with cost scaling applied,
    and the barrier reserve is never violated
  * each builder claims the role the script meant it to, under the same
    entry-tile rule `units/states/opening.py` uses at runtime

and then reports two things legality alone cannot catch:

  * whether the barriers actually seal -- are the two cores still connected
    afterwards, and how big a region does each end up in
  * whether the map can be TOLD APART from the others of its size that share our
    core position. yulerune and frostgate are both 20x20 with cores at (2,9), so
    running an opener on "20x20, core (2,9)" alone would throw builders into
    walls on the wrong one. This reports how many tiles inside the core's opening
    vision differ, and of what kind, for every colliding pair.

    python3 tools/check_openers.py [--bot Tyr_v1] [--map valkyrie] [--verbose]

Exit status is 1 if any map reports an error, so it can gate a submission.
"""

from __future__ import annotations

import argparse
import importlib.util
from collections import deque
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import gen_mapdata as G  # noqa: E402

EMPTY, WALL, ORE = 0, 1, 2

# Engine constants that the opener has to respect (fcode._types.GameConstants).
CORE_SPAWNING_RADIUS_SQ = 2
LAUNCHER_RANGE_SQ = 26
STARTING_TITANIUM = 500
PASSIVE_TITANIUM_AMOUNT = 10
PASSIVE_TITANIUM_INTERVAL = 4

BASE_COST = {"builder": 30, "launcher": 20, "barrier": 3, "gunner": 20,
             "sentinel": 30, "harvester": 20, "conveyor": 3, "splitter": 6}
# Fraction the cost scale gains each time one is built, by category.
SCALE_STEP = {"builder": 0.20, "launcher": 0.10, "barrier": 0.01, "gunner": 0.20,
              "sentinel": 0.20, "harvester": 0.05, "conveyor": 0.01, "splitter": 0.01}

CARDINALS = ((0, -1), (1, 0), (0, 1), (-1, 0))


def load_openers(bot: str):
    path = PROJECT_ROOT / "bots" / bot / "openers.py"
    if not path.is_file():
        raise SystemExit(f"no openers table at {path}")
    spec = importlib.util.spec_from_file_location(f"_openers_{bot}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sim:
    """A deliberately literal replay of the script against a known board.

    Not a reimplementation of the engine -- it models only what an opener can get
    wrong: whose turn it is, where each unit stands, what is occupied, and what
    everything costs.

    Two engine facts it does model, because openers live or die on them (both
    probed against the real engine rather than assumed):

      * Units act in ascending entity-id order, i.e. creation order. A launcher
        is built by a builder, so every builder spawned AFTER it has a higher id
        and acts after it -- but the builders spawned BEFORE it act first, which
        is what lets a bot step off a pickup tile and the launcher throw the next
        bot onto it in the same round.
      * A builder is on the board the instant the core spawns it and can be
        thrown that same round, but does not itself run until the next one. The
        five-turn shape of these openers depends entirely on this.
    """

    def __init__(self, m, ops, spec, mirror, max_turns=200):
        self.m = m
        self.ops = ops
        self.spec = spec
        self.mirror = mirror
        self.w, self.h = m["w"], m["h"]
        self.rows = m["rows"]
        self.max_turns = max_turns

        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.trace: list[str] = []

        self.my_core = self._core_for_side()
        self.their_core = m["core_b"] if self.my_core == m["core_a"] else m["core_a"]

        # occupancy: buildings we place, plus both core footprints
        self.buildings: dict[tuple[int, int], str] = {}
        for c in (m["core_a"], m["core_b"]):
            for t in G.core_tiles(c):
                self.buildings[t] = "core"
        self.bots: dict[int, tuple[int, int]] = {}      # role -> pos
        self.launchers: dict[tuple[int, int], list] = {}

        self.ti = STARTING_TITANIUM
        self.scale = {k: 1.0 for k in BASE_COST}
        self.barriers_built = 0
        self.barriers_total = ops.barrier_total(spec)

        self.core_q = list(spec["core_script"])
        self.launcher_q = {self._p(k): list(v) for k, v in spec["launchers"].items()}
        self.builder_q = [list(r) for r in spec["builders"]]
        self.next_role = 0                      # role handed to the next builder
        self.first_run: dict[int, int] = {}     # role -> first turn it may act
        self.claimed: dict[int, bool] = {}      # role -> claim rule checked
        self.done_at: dict[int, int] = {}
        # Actors in creation order, which is entity-id order and so turn order.
        self.order: list[tuple] = [("core",)]
        self.turn = 0

    # --- geometry helpers ---------------------------------------------------
    def _core_for_side(self):
        core = self.spec["core"]
        if self.mirror:
            w, h = self.spec["size"]
            core = self.ops._flip_core(core, self.spec["sym"], w, h)
        return core

    def _p(self, pos):
        """A table coordinate, mirrored onto the side we are simulating."""
        return self.ops.mirror_pos(pos, self.spec) if self.mirror else tuple(pos)

    def env(self, p):
        x, y = p
        return self.rows[y][x]

    def in_bounds(self, p):
        return 0 <= p[0] < self.w and 0 <= p[1] < self.h

    def free(self, p):
        """A builder could stand here: in bounds, not wall, no building, no bot."""
        return (self.in_bounds(p) and self.env(p) != WALL
                and p not in self.buildings and p not in self.bots.values())

    def buildable(self, p):
        return (self.in_bounds(p) and self.env(p) == EMPTY
                and p not in self.buildings and p not in self.bots.values())

    def cost(self, kind):
        return int(self.scale[kind] * BASE_COST[kind])

    def reserve(self):
        """Titanium the opener must not spend: what the barriers it still owes
        will cost. Same formula as `units/opener.barrier_reserve`, deliberately --
        a checker that reserves differently from the bot proves nothing."""
        return (self.barriers_total - self.barriers_built) * self.cost("barrier")

    def afford(self, kind, reserved=True):
        """Mirrors `units/opener.can_spend`: the reserve binds the CORE's spawns
        and nothing else. A builder placing a scripted building spends against
        the bare balance, because it is what the reserve is being held for."""
        c = self.cost(kind)
        r = self.reserve() if reserved else 0
        return self.ti - c > r, c, r

    def pay(self, kind):
        self.ti -= self.cost(kind)
        self.scale[kind] += SCALE_STEP[kind]

    def err(self, who, msg):
        self.errors.append(f"t{self.turn:<3} {who:<10} {msg}")

    def warn(self, who, msg):
        self.warnings.append(f"t{self.turn:<3} {who:<10} {msg}")

    # --- pathing ------------------------------------------------------------
    def path(self, src, dst, ignore_bots=True):
        """Cardinal BFS path src->dst over currently-open tiles, or None.

        Other builders are ignored by default: they move too, so treating them as
        walls would report phantom blockages. Buildings the opener itself has put
        down are NOT ignored -- walking into your own barrier is a real bug and is
        the one this is looking for.
        """
        if src == dst:
            return [src]
        blocked = set(self.buildings)
        if not ignore_bots:
            blocked |= set(self.bots.values())
        prev = {src: None}
        q = deque([src])
        while q:
            cur = q.popleft()
            for dx, dy in CARDINALS:
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt in prev or not self.in_bounds(nxt):
                    continue
                if self.env(nxt) == WALL or nxt in blocked:
                    continue
                prev[nxt] = cur
                if nxt == dst:
                    out = [nxt]
                    while prev[out[-1]] is not None:
                        out.append(prev[out[-1]])
                    return out[::-1]
                q.append(nxt)
        return None

    # --- per-actor turns ----------------------------------------------------
    def core_turn(self):
        if not self.core_q:
            return
        op = self.core_q[0]
        assert op[0] == self.ops.SPAWN, f"core op {op[0]} not supported"
        tile = self._p(op[1])
        if not self.in_bounds(tile):
            self.err("core", f"spawn {tile} out of bounds")
            self.core_q.pop(0)
            return
        near = any(max(abs(tile[0] - t[0]), abs(tile[1] - t[1])) <= 1
                   and (tile[0] - t[0]) ** 2 + (tile[1] - t[1]) ** 2 <= CORE_SPAWNING_RADIUS_SQ
                   for t in G.core_tiles(self.my_core))
        if not near:
            self.err("core", f"spawn {tile} is not in the core's spawn ring "
                             f"(core {self.my_core})")
            self.core_q.pop(0)
            return
        if self.env(tile) == WALL:
            self.err("core", f"spawn {tile} is a wall")
            self.core_q.pop(0)
            return
        if not self.free(tile):
            return  # occupied this turn; wait, exactly as the bot will
        ok, c, r = self.afford("builder")
        if not ok:
            self.warn("core", f"spawn {tile} blocked by reserve "
                              f"(ti={self.ti} cost={c} reserve={r})")
            return
        self.pay("builder")
        role = self.next_role
        self.next_role += 1
        self.core_q.pop(0)
        if role >= len(self.builder_q):
            self.warn("core", f"spawned builder #{role} but only "
                              f"{len(self.builder_q)} roles are scripted")
            return
        # On the board at once (a launcher can throw it this very turn) but it
        # does not run until next turn.
        self.bots[role] = tile
        self.first_run[role] = self.turn + 1
        self.order.append(("bot", role))
        self.trace.append(f"t{self.turn:<3} core       spawn role {role} at {tile}")

    def launcher_turn(self, lpos):
        q = self.launcher_q.get(lpos)
        if not q or lpos not in self.buildings:
            return
        src, dst = self._p(q[0][1]), self._p(q[0][2])
        role = next((r for r, p in self.bots.items() if p == src), None)
        if role is None:
            return                       # nobody standing on the pickup tile yet
        d2 = (dst[0] - lpos[0]) ** 2 + (dst[1] - lpos[1]) ** 2
        if d2 > LAUNCHER_RANGE_SQ:
            self.err("launcher", f"{lpos} -> {dst} is out of range "
                                 f"(d^2={d2} > {LAUNCHER_RANGE_SQ})")
            q.pop(0)
            return
        pick2 = (src[0] - lpos[0]) ** 2 + (src[1] - lpos[1]) ** 2
        if pick2 > 2:
            self.err("launcher", f"{lpos} cannot reach a bot at {src} (d^2={pick2})")
            q.pop(0)
            return
        if pick2 == 2:
            self.warn("launcher", f"{lpos} picks up diagonally from {src}; "
                                  f"orthogonal pickup is the verified case")
        if not self.free(dst):
            self.err("launcher", f"{lpos} -> {dst} is not a passable landing tile "
                                 f"({'wall' if self.env(dst) == WALL else 'occupied'})")
            q.pop(0)
            return
        self.bots[role] = dst
        q.pop(0)
        self.trace.append(f"t{self.turn:<3} launcher   {lpos} throws role {role} "
                          f"{src} -> {dst} (d^2={d2})")

    def check_claim(self, role):
        """Would this builder claim the role the script means it to?

        Replays `units/states/opening._claim` exactly: walk the roles in order
        and take the first whose spawn tile matches where we are standing, or
        whose program OPENS with a WAIT naming this tile. Getting a different
        answer here means two roles are confusable and one builder will run the
        other's program.
        """
        self.claimed[role] = True
        me = self.bots[role]
        script = self.spec["core_script"]
        for r, prog in enumerate(self.spec["builders"]):
            by_spawn = r < len(script) and self._p(script[r][1]) == me
            by_wait = (prog and prog[0][0] == self.ops.WAIT
                       and self._p(prog[0][1]) == me)
            if not (by_spawn or by_wait):
                continue
            # The runtime also skips a role whose first build is already up.
            first_build = next((o for o in prog if o[0] == self.ops.BUILD), None)
            if first_build is not None and self._p(first_build[2]) in self.buildings:
                continue
            if r != role:
                self.err(f"role {role}", f"standing on {me} at its first turn it would "
                                         f"claim role {r}, not {role} -- the two entry "
                                         f"tiles are confusable")
            return
        self.err(f"role {role}", f"standing on {me} at its first turn it matches no "
                                 f"role's entry tile, so it would never claim {role}")

    def builder_turn(self, role):
        q = self.builder_q[role]
        while q:
            op = q[0]
            kind = op[0]
            pos = self.bots[role]
            if kind == self.ops.WAIT:
                if pos == self._p(op[1]):
                    q.pop(0)
                    continue          # landed: start the next op the same turn
                return                # still waiting to be thrown
            if kind == self.ops.GOTO:
                dst = self._p(op[1])
                if pos == dst:
                    q.pop(0)
                    continue
                p = self.path(pos, dst)
                if p is None:
                    self.err(f"role {role}", f"no path {pos} -> {dst}"
                             + (" (the opener's own barriers close it)"
                                if self.path(pos, dst) is None else ""))
                    q.clear()
                    return
                self.bots[role] = p[1]
                return
            if kind == self.ops.SKIP_UNLESS_ENEMY:
                # The enemy's buildings are not simulated, so this models the
                # branch that always happens in an empty simulation: nothing is
                # there, so the guarded ops are skipped. The other branch's ops
                # are still checked, because they are checked from the table.
                for _ in range(1 + op[2]):
                    if q:
                        q.pop(0)
                continue

            if kind == self.ops.STRIKE:
                tile = self._p(op[1])
                d = abs(tile[0] - pos[0]) + abs(tile[1] - pos[1])
                if d != 1:
                    p = self.path(pos, tile)
                    if p is None or len(p) < 2:
                        self.err(f"role {role}", f"cannot reach a tile beside {tile} "
                                                 f"from {pos} to strike it")
                        q.clear()
                        return
                    self.bots[role] = p[1]
                    return
                # Nothing is known about what the enemy will have built there, so
                # the check is of the fallback: the tile must at least be legal to
                # take and hold if it turns out to be empty.
                if self.env(tile) == WALL:
                    self.err(f"role {role}", f"strike target {tile} is wall")
                    q.clear()
                    return
                ok, c, r = self.afford("barrier", reserved=False)
                if not ok:
                    return
                self.pay("barrier")
                self.buildings[tile] = op[2]
                if op[2] == "barrier":
                    self.barriers_built += 1
                    self.barriers_total += 1
                self.trace.append(f"t{self.turn:<3} role {role}     took {tile} "
                                  f"with {op[2]} (ti now {self.ti})")
                q.pop(0)
                return

            if kind == self.ops.ASSAULT:
                stand, target = self._p(op[1]), self._p(op[2])
                d = abs(target[0] - stand[0]) + abs(target[1] - stand[1])
                if d != 1:
                    self.err(f"role {role}", f"assault target {target} is not "
                                             f"orthogonally adjacent to {stand}")
                    q.clear()
                    return
                if target not in self.buildings or self.buildings[target] != "core":
                    self.err(f"role {role}", f"assault target {target} is not an "
                                             f"enemy core tile")
                    q.clear()
                    return
                if pos != stand:
                    p = self.path(pos, stand)
                    if p is None:
                        self.err(f"role {role}", f"no path {pos} -> {stand} to assault from")
                        q.clear()
                        return
                    self.bots[role] = p[1]
                    return
                q.pop(0)     # in position; the real op never ends, the sim stops here
                continue

            if kind == self.ops.BUILD:
                what, tile = op[1], self._p(op[2])
                d = abs(tile[0] - pos[0]) + abs(tile[1] - pos[1])
                if d != 1:
                    self.err(f"role {role}", f"cannot build {what} at {tile} from {pos}: "
                                             f"not orthogonally adjacent (manhattan {d})")
                    q.clear()
                    return
                if not self.in_bounds(tile):
                    self.err(f"role {role}", f"build {what} at {tile} out of bounds")
                    q.clear()
                    return
                if self.env(tile) == WALL:
                    self.err(f"role {role}", f"build {what} at {tile}: tile is wall")
                    q.clear()
                    return
                if self.env(tile) == ORE and what != "harvester":
                    self.err(f"role {role}", f"build {what} at {tile}: tile is ore "
                                             f"(only a harvester may go there)")
                    q.clear()
                    return
                if not self.buildable(tile):
                    return            # transiently occupied; retry next turn
                is_barrier = what == "barrier"
                ok, c, r = self.afford(what, reserved=False)
                if not ok:
                    self.warn(f"role {role}", f"build {what} at {tile} blocked by reserve "
                                              f"(ti={self.ti} cost={c} reserve={r})")
                    return
                self.pay(what)
                self.buildings[tile] = what
                if is_barrier:
                    self.barriers_built += 1
                if what == "launcher":
                    self.order.append(("launcher", tile))
                q.pop(0)
                self.trace.append(f"t{self.turn:<3} role {role}     build {what} at {tile} "
                                  f"(ti now {self.ti})")
                return
            self.err(f"role {role}", f"unknown op {kind!r}")
            q.clear()
            return

    def run(self):
        for self.turn in range(self.max_turns):
            if self.turn and self.turn % PASSIVE_TITANIUM_INTERVAL == 0:
                self.ti += PASSIVE_TITANIUM_AMOUNT
            # Ascending entity id == creation order == the order the engine runs
            # them. Iterate a copy: a spawn or a build appends to self.order, and
            # the new unit does not act until the next turn anyway.
            for actor in list(self.order):
                if actor[0] == "core":
                    self.core_turn()
                elif actor[0] == "launcher":
                    self.launcher_turn(actor[1])
                else:
                    role = actor[1]
                    if self.turn < self.first_run.get(role, 0):
                        continue          # spawned this turn; does not run yet
                    if not self.claimed.get(role):
                        self.check_claim(role)
                    if self.builder_q[role]:
                        self.builder_turn(role)
                        if not self.builder_q[role] and role not in self.done_at:
                            self.done_at[role] = self.turn
            if not self.core_q and not any(self.builder_q) \
                    and not any(self.launcher_q.values()):
                return True
        for role, q in enumerate(self.builder_q):
            if q:
                self.err(f"role {role}", f"did not finish in {self.max_turns} turns; "
                                         f"{len(q)} op(s) left, first is {q[0]}")
        if self.core_q:
            self.err("core", f"{len(self.core_q)} spawn(s) never happened")
        return False

    # --- what the barriers achieved ----------------------------------------
    def connectivity(self):
        blocked = set(self.buildings)
        seen = set()
        start = None
        for t in G.core_tiles(self.my_core):
            for dx, dy in CARDINALS:
                p = (t[0] + dx, t[1] + dy)
                if self.in_bounds(p) and self.env(p) != WALL and p not in blocked:
                    start = p
                    break
            if start:
                break
        if start is None:
            return None
        q = deque([start])
        seen.add(start)
        while q:
            cur = q.popleft()
            for dx, dy in CARDINALS:
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt in seen or not self.in_bounds(nxt):
                    continue
                if self.env(nxt) == WALL or nxt in blocked:
                    continue
                seen.add(nxt)
                q.append(nxt)
        their_adj = [(t[0] + dx, t[1] + dy)
                     for t in G.core_tiles(self.their_core) for dx, dy in CARDINALS]
        reaches = any(p in seen for p in their_adj)
        open_tiles = sum(1 for y in range(self.h) for x in range(self.w)
                         if self.rows[y][x] != WALL and (x, y) not in blocked)
        # Where the ore ended up, and who is stuck where. A seal that works is
        # still a bad seal if it walls the ore -- or our own builders -- into a
        # pocket neither core can reach, which is not visible from tile counts.
        ore = [(x, y) for y in range(self.h) for x in range(self.w)
               if self.rows[y][x] == ORE and (x, y) not in blocked]
        mine_ore = sum(1 for p in ore if p in seen)
        stranded = [r for r, p in self.bots.items() if p not in seen]
        return {"reaches_enemy": reaches, "my_region": len(seen), "open": open_tiles,
                "ore_total": len(ore), "ore_mine": mine_ore,
                "ore_walled_off": sum(1 for p in ore if p not in seen
                                      and not self._enemy_reaches(p, blocked)),
                "stranded": stranded}

    def _enemy_reaches(self, target, blocked):
        seen = set()
        q = deque()
        for t in G.core_tiles(self.their_core):
            for dx, dy in CARDINALS:
                p = (t[0] + dx, t[1] + dy)
                if (self.in_bounds(p) and self.env(p) != WALL and p not in blocked
                        and p not in seen):
                    seen.add(p)
                    q.append(p)
        while q:
            cur = q.popleft()
            if cur == target:
                return True
            for dx, dy in CARDINALS:
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt in seen or not self.in_bounds(nxt):
                    continue
                if self.env(nxt) == WALL or nxt in blocked:
                    continue
                seen.add(nxt)
                q.append(nxt)
        return target in seen


CORE_VISION_RADIUS_SQ = 36
SENTINEL_RANGE_SQ = 32
KIND = {EMPTY: "empty", WALL: "WALL", ORE: "ORE"}

DELTAS = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0),
          "northeast": (1, -1), "northwest": (-1, -1),
          "southeast": (1, 1), "southwest": (-1, 1)}


def sentinel_ray(src, facing, w, h):
    """Tiles a sentinel at `src` facing `facing` can hit.

    A single-tile-wide line that ignores obstacles, so terrain does not truncate
    it -- only the r^2=32 range does.
    """
    dx, dy = DELTAS[facing]
    out = []
    k = 1
    while True:
        x, y = src[0] + dx * k, src[1] + dy * k
        if not (0 <= x < w and 0 <= y < h):
            break
        if (x - src[0]) ** 2 + (y - src[1]) ** 2 > SENTINEL_RANGE_SQ:
            break
        out.append((x, y))
        k += 1
    return out


def check_sentinels(ops, name, spec, m, mirror):
    """Every scripted sentinel must actually be able to hit what it is told to.

    This is the check worth having. A sentinel's facing is chosen off a rendered
    map by eye, its ray is diagonal, and being one tile out means it silently
    shoots past the enemy core forever -- there is no error, no failed action,
    just a turret that never does anything. So: find the BUILD that places each
    scripted sentinel, walk its ray, and confirm every listed target is on it.
    """
    problems = 0
    w, h = m["w"], m["h"]

    def p(t):
        return ops.mirror_pos(t, spec) if mirror else tuple(t)

    def d(name_):
        return ops.mirror_dir(name_, spec) if mirror else name_

    placed = {}
    for role, prog in enumerate(spec["builders"]):
        for op in prog:
            if op[0] == ops.BUILD and op[1] == "sentinel":
                placed[tuple(op[2])] = (role, op[3] if len(op) > 3 else None)

    for tile, targets in spec.get("sentinels", {}).items():
        tile = tuple(tile)
        if tile not in placed:
            print(f"         ERROR sentinel {tile} is scripted to fire but no "
                  f"builder program ever builds it")
            problems += 1
            continue
        role, face = placed[tile]
        if face is None:
            print(f"         ERROR sentinel {tile} is built without a facing")
            problems += 1
            continue
        src, facing = p(tile), d(face)
        ray = set(sentinel_ray(src, facing, w, h))
        core_tiles = set(G.core_tiles(m["core_a"])) | set(G.core_tiles(m["core_b"]))
        for t in targets:
            tp = p(t)
            if tp not in ray:
                print(f"         ERROR sentinel {src} facing {facing} cannot hit "
                      f"{tp}: its ray is {sorted(ray)}")
                problems += 1
        last = p(targets[-1])
        if last not in core_tiles:
            print(f"         note  sentinel {src} last-resort target {last} is not "
                  f"a core tile; it will hold fire unless a builder stands beside it")
        if not problems:
            print(f"         sentinel {src} (role {role}) facing {facing}: ray "
                  f"{sentinel_ray(src, facing, w, h)}")
    return problems


def check_discrimination(name, m):
    """Can this map be told apart from the others that share its identity key?

    `(width, height, our core)` is not an identity -- yulerune and frostgate are
    both 20x20 with cores at (2,9) -- so `units/opener.py` narrows candidates by
    terrain before it will run anything. That only helps if the tiles that differ
    are ones a unit can actually SEE, and it has to be true on turn 0, before the
    core makes its first scripted spawn. So: for each colliding map, count the
    differing tiles inside the core's opening vision, and say what kind they are.

    Vision is measured from the core's anchor tile alone, the pessimistic reading
    (the engine may well measure from the whole footprint, which sees strictly
    more). Reports both sides -- the two cores see different halves of the board
    and a pair can be separable from one and not the other.
    """
    problems = 0
    same_size = []
    for path in sorted((PROJECT_ROOT / "maps").glob("*.map26")):
        if path.stem == name:
            continue
        o = G.parse_map(path)
        if (o["w"], o["h"]) == (m["w"], m["h"]):
            same_size.append(o)

    for core, label in ((m["core_a"], "A"), (m["core_b"], "B")):
        rivals = [o for o in same_size if core in (o["core_a"], o["core_b"])]
        if not rivals:
            print(f"         id: side {label} core {core} is unique among "
                  f"{m['w']}x{m['h']} maps")
            continue
        for o in rivals:
            diffs = [(x, y, m["rows"][y][x], o["rows"][y][x])
                     for y in range(m["h"]) for x in range(m["w"])
                     if m["rows"][y][x] != o["rows"][y][x]
                     and (x - core[0]) ** 2 + (y - core[1]) ** 2 <= CORE_VISION_RADIUS_SQ]
            walls = sum(1 for d in diffs if WALL in (d[2], d[3]))
            if not diffs:
                print(f"         id: ERROR side {label} cannot be told from "
                      f"{o['name']} on turn 0 -- identical inside core vision")
                problems += 1
                continue
            first = diffs[0]
            print(f"         id: side {label} vs {o['name']}: {len(diffs)} tile(s) "
                  f"differ in core vision ({walls} wall, {len(diffs) - walls} ore); "
                  f"e.g. ({first[0]},{first[1]}) {KIND[first[2]]} vs {KIND[first[3]]}")
    return problems


def check_map(ops, name, spec, verbose=False):
    m = G.parse_map(PROJECT_ROOT / "maps" / f"{name}.map26")
    sym = G.symmetry(m)
    problems = 0
    print(f"\n=== {name}  {m['w']}x{m['h']}  sym={sym or 'NONE'}  "
          f"A={m['core_a']} B={m['core_b']}")
    if tuple(spec["size"]) != (m["w"], m["h"]):
        print(f"  ERROR  table says size {tuple(spec['size'])}, map is {(m['w'], m['h'])}")
        problems += 1
    if spec["sym"] != sym:
        print(f"  ERROR  table says sym={spec['sym']!r}, map is {sym!r}")
        problems += 1
    if tuple(spec["core"]) not in (m["core_a"], m["core_b"]):
        print(f"  ERROR  table's core {tuple(spec['core'])} is neither core "
              f"({m['core_a']} / {m['core_b']})")
        problems += 1
    if problems:
        return problems

    # The bot decides which side it is on by looking its own core up in this
    # table, so check that lookup answers correctly for BOTH real cores. A table
    # authored from core_b is the case that breaks: derive the flip from
    # anything other than the table's own `core` and every coordinate silently
    # inverts, the core spawns onto a tile nowhere near itself, and -- with no
    # stall timeout -- the opener waits there for the rest of the game.
    for real_core, label in ((m["core_a"], "A"), (m["core_b"], "B")):
        hit = ops.lookup(m["w"], m["h"], real_core)
        if hit is None:
            print(f"  ERROR  lookup({m['w']},{m['h']},{real_core}) found nothing; "
                  f"side {label} would never run this opener")
            problems += 1
            continue
        found, _, mirrored = hit
        if found != name:
            print(f"  ERROR  lookup for side {label} returned {found!r}, not {name!r}")
            problems += 1
            continue
        expect = tuple(spec["core"]) != tuple(real_core)
        if mirrored != expect:
            print(f"  ERROR  lookup says mirror={mirrored} for side {label} core "
                  f"{real_core}, but the table is authored for {tuple(spec['core'])}")
            problems += 1

    problems += check_discrimination(name, m)

    for mirror in (False, True):
        sim = Sim(m, ops, spec, mirror)
        side = "B (mirrored)" if mirror else "A (as authored)"
        finished = sim.run()
        sim_problems = len(sim.errors) + check_sentinels(ops, name, spec, m, mirror)
        tag = "ok " if (finished and not sim_problems) else "FAIL"
        last = max(sim.done_at.values()) if sim.done_at else -1
        print(f"  [{tag}] side {side:<14} core={sim.my_core}  "
              f"script done t{last}  ti left {sim.ti}  "
              f"barriers {sim.barriers_built}/{sim.barriers_total}")
        for e in sim.errors:
            print(f"         ERROR  {e}")
        for w in sim.warnings:
            print(f"         warn   {w}")
        problems += sim_problems
        c = sim.connectivity()
        if c:
            verdict = ("enemy core STILL REACHABLE" if c["reaches_enemy"]
                       else "enemy core CUT OFF")
            print(f"         seal: {verdict}; our region {c['my_region']} of "
                  f"{c['open']} open tiles")
            print(f"         ore:  {c['ore_mine']}/{c['ore_total']} reachable from "
                  f"our core, {c['ore_walled_off']} walled into a pocket neither "
                  f"core can reach")
            if c["stranded"]:
                print(f"         WARNING role(s) {c['stranded']} finish outside our "
                      f"core's region -- they can never come home")
        if verbose:
            for line in sim.trace:
                print(f"         {line}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bot", default="Tyr_v1")
    ap.add_argument("--map", help="only this map")
    ap.add_argument("--verbose", action="store_true", help="print the whole simulated script")
    a = ap.parse_args()

    ops = load_openers(a.bot)
    names = [a.map] if a.map else sorted(ops.OPENERS)
    bad = 0
    for name in names:
        if name not in ops.OPENERS:
            print(f"no opener for {name!r}; have {sorted(ops.OPENERS)}")
            return 1
        bad += check_map(ops, name, ops.OPENERS[name], a.verbose)
    print(f"\n{len(names)} opener(s) checked, {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
