"""Interactive pygame viewer / sandbox for the Florent engine.

Controls
  Space ............ step ONE unit's turn
  Enter ............ step a whole round (all units + end-of-round resolution)
  1 conveyor  2 barrier  3 harvester  4 splitter   (team A)
  5 conveyor  6 barrier  7 harvester  8 splitter   (team B)
  9 delete    q attack (-2 hp)    e heal (+4 hp)
  W/A/S/D or arrows ... rotate the ghost's facing (conveyor/splitter)
  Left click ........... apply the selected tool at the tile (free, either team)
  Cmd/Ctrl+Z ........... undo the last edit
  Esc / close .......... quit

Renders the real visualiser2d sprites. A semi-transparent ghost of the selected
tool follows the cursor before you click. Edits are free and immediate; the bots
adapt on their next turn.
"""
import contextlib
import io
import os

import pygame

from fcode_shim import Direction, EntityType, Environment, Team

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "tools", "visualiser2d")

BG = (16, 13, 10)
GRID = (40, 32, 24)
C_EMPTY = (33, 26, 19)
C_WALL = (86, 78, 64)
C_ORE = (206, 170, 70)
TEAM = {Team.A: (240, 150, 70), Team.B: (80, 170, 240)}
TEAM_DK = {Team.A: (120, 70, 30), Team.B: (35, 80, 120)}
WHITE = (235, 228, 214)
DIM = (150, 138, 120)
HILITE = (250, 230, 120)

SUF = {Team.A: "gold", Team.B: "silver"}
DIRSUF = {Direction.NORTH: "n", Direction.NORTHEAST: "ne", Direction.EAST: "e",
          Direction.SOUTHEAST: "se", Direction.SOUTH: "s", Direction.SOUTHWEST: "sw",
          Direction.WEST: "w", Direction.NORTHWEST: "nw"}
LETTER = {EntityType.CONVEYOR: ">", EntityType.SPLITTER: "S", EntityType.HARVESTER: "H",
          EntityType.BARRIER: "B", EntityType.GUNNER: "G", EntityType.SENTINEL: "N",
          EntityType.LAUNCHER: "L", EntityType.BUILDER_BOT: "b", EntityType.CORE: "C"}

TOOLS = {
    pygame.K_1: ("build", EntityType.CONVEYOR, Team.A),
    pygame.K_2: ("build", EntityType.BARRIER, Team.A),
    pygame.K_3: ("build", EntityType.HARVESTER, Team.A),
    pygame.K_4: ("build", EntityType.SPLITTER, Team.A),
    pygame.K_5: ("build", EntityType.CONVEYOR, Team.B),
    pygame.K_6: ("build", EntityType.BARRIER, Team.B),
    pygame.K_7: ("build", EntityType.HARVESTER, Team.B),
    pygame.K_8: ("build", EntityType.SPLITTER, Team.B),
    pygame.K_9: ("delete", None, None),
    pygame.K_q: ("attack", None, None),
    pygame.K_e: ("heal", None, None),
}


def _entity_sprite_name(ent):
    t, suf = ent.type, SUF[ent.team]
    d = DIRSUF.get(ent.direction, "n")
    return {
        EntityType.CORE: f"base_{suf}.png",
        EntityType.BUILDER_BOT: f"builderbot_front_{suf}.png",
        EntityType.CONVEYOR: f"conveyor_{suf}.png",
        EntityType.SPLITTER: f"splitter_{d}_{suf}.png",
        EntityType.HARVESTER: f"harvester_{suf}.png",
        EntityType.BARRIER: f"barrier_{suf}.png",
        EntityType.GUNNER: f"gunner_{d}_{suf}.png",
        EntityType.SENTINEL: f"sentinel_{d}_{suf}.png",
        EntityType.LAUNCHER: f"launcher_{suf}.png",
    }[t]


class Viewer:
    def __init__(self, match, hud_w=300):
        self.m = match
        e = match.engine
        pygame.init()
        pygame.display.set_caption("Florent sandbox")
        info = pygame.display.Info()
        maxw, maxh = info.current_w - 80, info.current_h - 120
        self.cell = max(10, min((maxw - hud_w) // e.width, maxh // e.height, 44))
        self.gw, self.gh = e.width * self.cell, e.height * self.cell
        self.screen = pygame.display.set_mode((self.gw + hud_w, max(self.gh, 540)))
        self.font = pygame.font.SysFont("menlo,monospace", 13)
        self.big = pygame.font.SysFont("menlo,monospace", 16, bold=True)
        self.tool = ("build", EntityType.CONVEYOR, Team.A)
        self.facing = Direction.NORTH
        self.hover = None
        self.undo_stack = []
        self.save_btn = None
        self.flash = ""
        self.flash_until = 0
        self.clock = pygame.time.Clock()
        self._raw = {}          # filename -> base Surface (or None if missing)
        self._scaled = {}       # (filename, w, h) -> Surface

    # ---- sprites ----
    def _base(self, name):
        if name not in self._raw:
            try:
                self._raw[name] = pygame.image.load(os.path.join(ASSETS, name)).convert_alpha()
            except Exception:
                self._raw[name] = None
        return self._raw[name]

    def _sprite(self, name, w, h):
        key = (name, w, h)
        s = self._scaled.get(key)
        if s is None:
            base = self._base(name)
            s = pygame.transform.smoothscale(base, (w, h)) if base else False
            self._scaled[key] = s
        return s or None

    def _conveyor_sprite(self, team, direction, w, h):
        """The single conveyor sprite, rotated to its facing (default sprite = WEST)."""
        key = ("conv", team, direction, w, h)
        s = self._scaled.get(key)
        if s is None:
            base = self._sprite(f"conveyor_{SUF[team]}.png", w, h)
            if base:
                deg = {Direction.EAST: 180, Direction.NORTH: 270,
                       Direction.WEST: 0, Direction.SOUTH: 90}.get(direction, 0)
                s = pygame.transform.rotate(base, deg) if deg else base
            else:
                s = False
            self._scaled[key] = s
        return s or None

    def _wall_sprite(self):
        """Wall texture at ~10% opacity, over the dark empty backdrop."""
        key = ("__wall__", self.cell)
        s = self._scaled.get(key)
        if s is None:
            base = self._sprite("natural_wall.jpg", self.cell, self.cell)
            if base:
                s = base.copy()
                s.fill((255, 255, 255, 26), None, pygame.BLEND_RGBA_MULT)
            else:
                s = False
            self._scaled[key] = s
        return s or None

    # ---- geometry ----
    def _rect(self, x, y, w=1, h=1, pad=0):
        return pygame.Rect(x * self.cell + pad, y * self.cell + pad,
                           w * self.cell - 2 * pad, h * self.cell - 2 * pad)

    def _cell_at(self, mx, my):
        if mx >= self.gw:
            return None
        x, y = mx // self.cell, my // self.cell
        if 0 <= x < self.m.engine.width and 0 <= y < self.m.engine.height:
            return x, y
        return None

    # ---- actions ----
    def _step(self, whole_round):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.m.step_round() if whole_round else self.m.step_unit()

    def _apply_tool(self, x, y):
        e = self.m.engine
        kind, etype, team = self.tool
        undo = None
        if kind == "build":
            undo = e.god_place(etype, team, x, y, self.facing if etype in (
                EntityType.CONVEYOR, EntityType.SPLITTER) else None)
        elif kind == "delete":
            undo = e.god_delete(x, y)
        elif kind == "attack":
            undo = e.god_damage(x, y, 2)
        elif kind == "heal":
            undo = e.god_heal(x, y, 4)
        if undo:
            self.undo_stack.append(undo)

    def _undo(self):
        if self.undo_stack:
            self.undo_stack.pop()()

    def _save_replay(self):
        path = os.path.abspath(f"sandbox_r{self.m.engine.round}.replay26")
        try:
            self.m.save_replay(path)
            self.flash = "saved " + os.path.basename(path)
        except Exception as exc:
            self.flash = "save failed: " + str(exc)
        self.flash_until = pygame.time.get_ticks() + 3500

    # ---- drawing ----
    def _blit_entity(self, surf, ent, ghost=False):
        c = self.cell
        if ent.type == EntityType.CORE:
            name = _entity_sprite_name(ent)
            spr = self._sprite2x(name, 2 * c)
            r = self._rect(ent.x, ent.y, 2, 2)
            if spr:
                s = spr.copy(); s.set_alpha(120) if ghost else None
                surf.blit(s, (r.left, r.bottom - s.get_height()))
            else:
                self._flat(surf, self._rect(ent.x, ent.y, 2, 2, 1), ent, ghost)
            return
        r = self._rect(ent.x, ent.y, pad=1)
        if ent.type == EntityType.CONVEYOR:
            spr = self._conveyor_sprite(ent.team, ent.direction, r.width, r.height)
        else:
            spr = self._sprite(_entity_sprite_name(ent), r.width, r.height)
        if spr:
            if ghost:
                spr = spr.copy(); spr.set_alpha(120)
            surf.blit(spr, spr.get_rect(center=r.center))   # centered (rotation may pad)
        else:
            self._flat(surf, r, ent, ghost)
        if not ghost and getattr(ent, "holding", False):
            ico = self._sprite("titanium.png", c // 2, c // 2)
            if ico:
                surf.blit(ico, ico.get_rect(center=r.center))
            else:
                pygame.draw.circle(surf, C_ORE, r.center, max(2, c // 7))
        if not ghost and ent.hp < ent.max_hp:
            frac = max(0, ent.hp / ent.max_hp)
            pygame.draw.rect(surf, (90, 200, 90) if frac > 0.4 else (210, 90, 70),
                             pygame.Rect(r.left, r.bottom - 3, int(r.width * frac), 3))

    def _sprite2x(self, name, w):
        base = self._base(name)
        if not base:
            return None
        h = int(w * base.get_height() / base.get_width())
        return self._sprite(name, w, h)

    def _flat(self, surf, r, ent, ghost):
        col = TEAM[ent.team]
        s = pygame.Surface((r.width, r.height), pygame.SRCALPHA)
        pygame.draw.rect(s, (*col, 120 if ghost else 255), s.get_rect(), border_radius=3)
        surf.blit(s, r.topleft)
        t = self.font.render(LETTER.get(ent.type, "?"), True, BG)
        surf.blit(t, t.get_rect(center=r.center))

    def _arrow(self, surf, center, d, color):
        dx, dy = d.delta()
        r = self.cell * 0.34
        cx, cy = center
        pygame.draw.line(surf, color, (cx, cy), (cx + dx * r, cy + dy * r), max(2, self.cell // 9))
        pygame.draw.circle(surf, color, (int(cx + dx * r), int(cy + dy * r)), max(2, self.cell // 8))

    def draw(self):
        e = self.m.engine
        self.screen.fill(BG)
        for y in range(e.height):
            for x in range(e.width):
                env = e.terrain[y][x]
                r = self._rect(x, y)
                if env == Environment.WALL:
                    self.screen.fill(C_EMPTY, r)
                    spr = self._wall_sprite()
                    self.screen.blit(spr, r.topleft) if spr else self.screen.fill(C_WALL, r)
                elif env == Environment.ORE_TITANIUM:
                    self.screen.fill(C_EMPTY, r)
                    spr = self._sprite("titanium_ore.png", self.cell, self.cell)
                    self.screen.blit(spr, r.topleft) if spr else self.screen.fill(C_ORE, r)
                else:
                    self.screen.fill(C_EMPTY, r)
        if self.cell >= 12:
            for x in range(e.width + 1):
                pygame.draw.line(self.screen, GRID, (x * self.cell, 0), (x * self.cell, self.gh))
            for y in range(e.height + 1):
                pygame.draw.line(self.screen, GRID, (0, y * self.cell), (self.gw, y * self.cell))
        for ent in sorted(e.entities.values(), key=lambda en: en.type == EntityType.BUILDER_BOT):
            self._blit_entity(self.screen, ent)
        cu = self.m.current_uid()
        if cu and cu in e.entities:
            ce = e.entities[cu]
            n = 2 if ce.type == EntityType.CORE else 1
            pygame.draw.rect(self.screen, HILITE, self._rect(ce.x, ce.y, n, n), 3)
        if self.hover:
            self._draw_ghost(*self.hover)
        self._draw_hud()
        pygame.display.flip()

    def _draw_ghost(self, x, y):
        kind, etype, team = self.tool
        r = self._rect(x, y, pad=1)
        if kind == "build":
            from types import SimpleNamespace
            self._blit_entity(self.screen, SimpleNamespace(
                type=etype, team=team, x=x, y=y, direction=self.facing,
                holding=False, hp=1, max_hp=1), ghost=True)
        else:
            col = {"delete": (220, 70, 60), "attack": (230, 120, 60), "heal": (90, 210, 110)}[kind]
            s = pygame.Surface((r.width, r.height), pygame.SRCALPHA)
            pygame.draw.rect(s, (*col, 90), s.get_rect())
            pygame.draw.rect(s, (*col, 200), s.get_rect(), 2)
            self.screen.blit(s, r.topleft)

    def _draw_hud(self):
        e = self.m.engine
        x0, y = self.gw + 14, 12

        def line(txt, color=WHITE, f=None):
            nonlocal y
            self.screen.blit((f or self.font).render(txt, True, color), (x0, y))
            y += (f or self.font).get_height() + 3

        line("FLORENT SANDBOX", HILITE, self.big)
        line(f"round {e.round}" + ("  GAME OVER" if e.winner else ""), WHITE, self.big)
        if e.winner:
            line(f"winner: {e.winner.value}  ({e.win_reason})", HILITE)
        cu = self.m.current_uid()
        if cu and cu in e.entities:
            ce = e.entities[cu]
            line(f"next: #{cu} {ce.type.value} ({ce.team.value})", DIM)
        else:
            line("next: end-of-round", DIM)
        y += 6
        kind, etype, team = self.tool
        tt = {"build": f"build {etype.value if etype else ''} [{team.value if team else ''}]",
              "delete": "delete", "attack": "attack -2", "heal": "heal +4"}[kind]
        line("tool: " + tt, TEAM.get(team, WHITE))
        if kind == "build" and etype in (EntityType.CONVEYOR, EntityType.SPLITTER):
            line(f"  facing: {self.facing.value}", DIM)
        line(f"undo stack: {len(self.undo_stack)}", DIM)
        # --- save-replay button ---
        y += 8
        hud_w = self.screen.get_width() - self.gw
        btn = pygame.Rect(x0, y, hud_w - 28, 28)
        hot = btn.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(self.screen, (70, 120, 80) if hot else (52, 92, 62), btn, border_radius=5)
        pygame.draw.rect(self.screen, (120, 190, 130), btn, 1, border_radius=5)
        bt = self.font.render("Save replay  (R)", True, WHITE)
        self.screen.blit(bt, bt.get_rect(center=btn.center))
        self.save_btn = btn
        y += btn.height + 4
        if self.flash and pygame.time.get_ticks() < self.flash_until:
            line(self.flash, HILITE)
        else:
            y += self.font.get_height() + 3
        y += 4
        for t in (Team.A, Team.B):
            ts = e.teams[t]
            line(f"team {t.value.upper()}", TEAM[t], self.big)
            line(f"  ti {ts.titanium}  ammo {ts.ammo}", WHITE)
            line(f"  units {e.unit_count(t)}  scale {ts.scale_pct/100:.2f}", WHITE)
            line(f"  collected {ts.titanium_collected}", DIM)
            y += 4
        y = self.screen.get_height() - 158
        for h in ["SPACE step unit  ENTER step round",
                  "1-4 build A   5-8 build B",
                  "9 delete  q -2hp  e +4hp",
                  "WASD/arrows facing",
                  "click = apply (free)",
                  "Cmd/Ctrl+Z = undo edit"]:
            line(h, DIM)

    def run(self):
        running = True
        while running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.KEYDOWN:
                    mod = ev.mod & (pygame.KMOD_META | pygame.KMOD_GUI | pygame.KMOD_CTRL)
                    if ev.key == pygame.K_ESCAPE:
                        running = False
                    elif ev.key == pygame.K_z and mod:
                        self._undo()
                    elif ev.key == pygame.K_r:
                        self._save_replay()
                    elif ev.key == pygame.K_SPACE:
                        self._step(False)
                    elif ev.key == pygame.K_RETURN:
                        self._step(True)
                    elif ev.key in TOOLS:
                        self.tool = TOOLS[ev.key]
                    elif ev.key in (pygame.K_w, pygame.K_UP):
                        self.facing = Direction.NORTH
                    elif ev.key in (pygame.K_s, pygame.K_DOWN):
                        self.facing = Direction.SOUTH
                    elif ev.key in (pygame.K_a, pygame.K_LEFT):
                        self.facing = Direction.WEST
                    elif ev.key in (pygame.K_d, pygame.K_RIGHT):
                        self.facing = Direction.EAST
                elif ev.type == pygame.MOUSEMOTION:
                    self.hover = self._cell_at(*ev.pos)
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    if self.save_btn and self.save_btn.collidepoint(ev.pos):
                        self._save_replay()
                    else:
                        cell = self._cell_at(*ev.pos)
                        if cell:
                            self._apply_tool(*cell)
            self.draw()
            self.clock.tick(60)
        pygame.quit()
