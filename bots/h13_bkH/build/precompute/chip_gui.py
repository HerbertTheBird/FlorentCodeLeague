"""Interactive sanity-checker for the chip mini-game solver.

Run:  python3 chip_gui.py     (from bots/herbert/)

Set up a situation with the mouse, then use Left/Right arrows to step through
optimal play (A drives toward the fastest kill, B toward survival / longest delay).

Controls
  Left-click a CARDINAL tile ........ cycle type: empty -> wall -> conveyor -> harvester
  Right-click a conv/harv tile ...... lower its HP by 2 (wraps to full at the bottom)
  Left-click an OUTER tile .......... toggle empty <-> wall
  Shift-Left-click a passable tile .. move B (the healer) there
  t ................................. toggle whose turn it is
  Right / Left arrow ................ advance / rewind one optimal ply
  r ................................. reset HP full, B to default, A to move
  q ................................. quit

Colors: A = blue (origin). conveyor = green (passable, 20). harvester = orange
(impassible, 30). wall = dark. empty = light. heal spots outlined gold. B = red dot.
"""

import tkinter as tk

import chip_precompute as C

CELL = 96
MARGIN = 44
CARD_CYCLE = ["empty", "wall", "conv", "harv"]
COLORS = {
    "empty": "#e8e8e8", "wall": "#3a3a3a",
    "conv": "#7fc97f", "harv": "#f0a860", "origin": "#6fa8dc",
}


class ChipGUI:
    def __init__(self, root):
        self.root = root
        root.title("chip solver sanity check")
        self.cards = {t: "empty" for t in C.TARGET_TILES}
        self.outer = {t: "empty" for t in C.OUTER_TILES}
        self.cache = {}                       # terrain key -> analysis tuple
        self.history = []
        self.last_move = ""

        w = MARGIN * 2 + CELL * 5
        self.canvas = tk.Canvas(root, width=w, height=w, bg="white",
                                highlightthickness=0)
        self.canvas.pack()
        self.status = tk.Label(root, text="", font=("Menlo", 13), justify="left",
                               anchor="w")
        self.status.pack(fill="x", padx=8, pady=4)
        self.help = tk.Label(
            root, justify="left", anchor="w", fg="#555", font=("Menlo", 10),
            text=("L-click cardinal: cycle type   R-click target: -2 HP   "
                  "L-click outer: wall   Shift-click: place B\n"
                  "t: flip turn    <- / ->: rewind / step optimal    r: reset    q: quit"))
        self.help.pack(fill="x", padx=8, pady=(0, 6))

        self.canvas.bind("<Button-1>", self.on_left)
        self.canvas.bind("<Shift-Button-1>", self.on_shift_left)
        self.canvas.bind("<Button-2>", self.on_right)
        self.canvas.bind("<Button-3>", self.on_right)
        root.bind("<Left>", lambda e: self.step(-1))
        root.bind("<Right>", lambda e: self.step(+1))
        root.bind("t", lambda e: self.flip_turn())
        root.bind("r", lambda e: self.reset_state())
        root.bind("q", lambda e: root.destroy())

        self.resolve()

    # ---- terrain / analysis ----
    def terrain(self):
        return {"cardinals": dict(self.cards), "outer": dict(self.outer)}

    def key(self):
        return (tuple(sorted(self.cards.items())), tuple(sorted(self.outer.items())))

    def resolve(self):
        k = self.key()
        if k not in self.cache:
            self.status.config(text="solving...")
            self.root.update_idletasks()
            self.cache[k] = C.analyze(self.terrain())
        self.board, self.targets, self.tiles, self.a_win, self.rank = self.cache[k]
        self.reset_state()

    def default_b(self):
        spots = [t for t in C.HEAL_SPOTS if t in self.board.passable]
        if spots:
            return spots[0]
        return self.tiles[0] if self.tiles else None

    def reset_state(self):
        hp = tuple(self.board.maxhalf[t] for t in self.targets)
        self.b = self.default_b()
        self.turn = 0
        self.hp = hp
        self.history = []
        self.last_move = ""
        self.draw()

    def state(self):
        return (self.hp, self.b, self.turn)

    # ---- geometry ----
    def cell_rect(self, x, y):
        col, row = x + 2, 2 - y
        px, py = MARGIN + col * CELL, MARGIN + row * CELL
        return px, py, px + CELL, py + CELL

    def tile_at(self, mx, my):
        col = (mx - MARGIN) // CELL
        row = (my - MARGIN) // CELL
        if not (0 <= col < 5 and 0 <= row < 5):
            return None
        t = (col - 2, 2 - row)
        return t if t in C.ALL_CELLS else None

    # ---- mouse ----
    def on_left(self, e):
        t = self.tile_at(e.x, e.y)
        if t is None:
            return
        if t in self.cards:
            cur = self.cards[t]
            self.cards[t] = CARD_CYCLE[(CARD_CYCLE.index(cur) + 1) % 4]
            self.resolve()
        elif t in self.outer:
            self.outer[t] = "wall" if self.outer[t] == "empty" else "empty"
            self.resolve()

    def on_shift_left(self, e):
        t = self.tile_at(e.x, e.y)
        if t is not None and t in self.board.passable:
            self.b = t
            self.turn = self.turn
            self.history = []
            self.last_move = ""
            self.draw()

    def on_right(self, e):
        t = self.tile_at(e.x, e.y)
        if t is not None and t in self.cards and self.cards[t] in ("conv", "harv"):
            i = self.targets.index(t)
            mh = self.board.maxhalf[t]
            h = self.hp[i] - 1
            if h < 1:
                h = mh
            hp = list(self.hp); hp[i] = h; self.hp = tuple(hp)
            self.history = []
            self.last_move = ""
            self.draw()

    # ---- play ----
    def flip_turn(self):
        self.turn ^= 1
        self.history = []
        self.last_move = ""
        self.draw()

    def terminal(self):
        return any(h == 0 for h in self.hp)

    def step(self, direction):
        if direction < 0:
            if self.history:
                self.hp, self.b, self.turn, self.last_move = self.history.pop()
                self.draw()
            return
        if not self.targets or self.b is None or self.terminal():
            return
        s = self.state()
        nxt = C.optimal_move(self.board, self.targets, self.a_win, self.rank, s)
        if nxt is None:
            return
        desc = C.describe_move(self.targets, s, nxt)
        self.history.append((self.hp, self.b, self.turn, self.last_move))
        self.hp, self.b, self.turn = nxt
        self.last_move = desc
        self.draw()

    # ---- render ----
    def draw(self):
        c = self.canvas
        c.delete("all")
        tgt_hp = {t: self.hp[i] for i, t in enumerate(self.targets)}
        for (x, y) in C.ALL_CELLS:
            px, py, qx, qy = self.cell_rect(x, y)
            if (x, y) == C.ORIGIN:
                fill = COLORS["origin"]
            elif (x, y) in self.cards:
                fill = COLORS[self.cards[(x, y)]]
            else:
                fill = COLORS[self.outer[(x, y)]]
            outline = "#d4af37" if (x, y) in C.HEAL_SPOTS else "#999"
            width = 3 if (x, y) in C.HEAL_SPOTS else 1
            c.create_rectangle(px, py, qx, qy, fill=fill, outline=outline, width=width)
            label = None
            if (x, y) == C.ORIGIN:
                label = "A"
            elif (x, y) in tgt_hp:
                mh = self.board.maxhalf[(x, y)] * 2
                label = f"{tgt_hp[(x, y)] * 2}/{mh}"
            if label:
                fg = "white" if fill in (COLORS["wall"],) else "black"
                c.create_text((px + qx) // 2, (py + qy) // 2, text=label,
                              font=("Menlo", 14, "bold"), fill=fg)
        # B token
        if self.b is not None:
            px, py, qx, qy = self.cell_rect(*self.b)
            r = 16
            cx, cy = (px + qx) // 2, py + 20
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#e05050", outline="black")
            c.create_text(cx, cy, text="B", font=("Menlo", 12, "bold"), fill="white")

        # status line
        if not self.targets:
            verdict = "no targets"
        elif self.b is None:
            verdict = "no B position (A wins by default)"
        elif self.terminal():
            verdict = "A WINS -- a target destroyed"
        else:
            s = self.state()
            if self.a_win.get(s):
                verdict = f"A WINS  (forced kill in {self.rank.get(s, '?')} plies)"
            else:
                verdict = "B WINS  (survives forever)"
        mover = "A" if self.turn == 0 else "B"
        nxt_preview = ""
        if self.targets and self.b is not None and not self.terminal():
            nm = C.optimal_move(self.board, self.targets, self.a_win, self.rank,
                                self.state())
            if nm is not None:
                nxt_preview = "   next-> " + C.describe_move(self.targets,
                                                             self.state(), nm)
        last = ("   last: " + self.last_move) if self.last_move else ""
        self.status.config(
            text=f"turn: {mover}    {verdict}{nxt_preview}{last}")


def main():
    root = tk.Tk()
    ChipGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
