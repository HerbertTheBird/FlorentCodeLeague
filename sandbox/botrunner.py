"""Per-unit bot isolation + match orchestration.

The competitor bots keep PER-UNIT state in module globals (map_info._my_pos,
comms._my_slot, ...), so every unit needs its own copy of the bot's module graph.
We get that by fresh-importing the bot for each unit and detaching those modules
from sys.modules, so the next unit imports a clean graph. `fcode` is our shim,
injected once and shared (it's stateless types).
"""
import importlib
import os
import sys
import traceback

# make the shim importable as `fcode` before any bot loads
import fcode_shim
sys.modules.setdefault("fcode", fcode_shim)

from engine import Engine, UnitController          # noqa: E402
from fcode_shim import EntityType, Team            # noqa: E402
from replaywriter import Recorder                  # noqa: E402


def _under(mod, root):
    """Is `mod` a module living under `root`? Robust to namespace packages whose
    __path__ recalculates lazily (must only be called while parents still exist)."""
    try:
        f = getattr(mod, "__file__", None)
        if f and os.path.abspath(f).startswith(root):
            return True
        paths = getattr(mod, "__path__", None)
        if paths:
            for p in list(paths):
                if os.path.abspath(str(p)).startswith(root):
                    return True
    except Exception:
        return False
    return False


def _detach(names):
    for n in names:
        sys.modules.pop(n, None)


def load_player(bot_dir):
    """Fresh-import the bot at `bot_dir` and return (Player instance, module graph).
    The graph is removed from sys.modules so the next call is fully isolated."""
    root = os.path.abspath(bot_dir)
    # purge any bot modules left from a prior load (compute the list fully, THEN
    # delete -- deleting a namespace-package parent mid-scan breaks child __path__).
    _detach([n for n, m in list(sys.modules.items()) if m is not None and _under(m, root)])
    before = set(sys.modules)
    sys.path.insert(0, root)
    try:
        main = importlib.import_module("main")
        player = main.Player()
    finally:
        try:
            sys.path.remove(root)
        except ValueError:
            pass
    names = [n for n in (set(sys.modules) - before) if _under(sys.modules[n], root)]
    if "main" not in names and "main" in sys.modules:
        names.append("main")
    graph = {n: sys.modules[n] for n in names}
    _detach(names)
    return player, graph


class Match:
    def __init__(self, width, height, terrain, cores, bot_a_dir, bot_b_dir, seed=1):
        self.engine = Engine(width, height, terrain, cores, seed=seed)
        self.bot_dir = {Team.A: os.path.abspath(bot_a_dir),
                        Team.B: os.path.abspath(bot_b_dir)}
        self.players = {}         # uid -> (player, graph)
        self.controllers = {}     # uid -> UnitController
        self.errors = []          # (round, uid, msg)
        self.recorder = Recorder(self.engine)

    def save_replay(self, path):
        return self.recorder.save(path)

    def _ensure_player(self, uid):
        if uid not in self.players:
            team = self.engine.entities[uid].team
            self.players[uid] = load_player(self.bot_dir[team])
        return self.players[uid]

    def _ctrl(self, uid):
        c = self.controllers.get(uid)
        if c is None:
            c = UnitController(self.engine, uid)
            self.controllers[uid] = c
        return c

    def _act(self, uid):
        if uid not in self.engine.entities:
            return
        player, _ = self._ensure_player(uid)
        ctrl = self._ctrl(uid)
        try:
            player.run(ctrl)
        except Exception as exc:      # bots also catch internally; this is a backstop
            self.errors.append((self.engine.round, uid,
                                f"{exc}\n{traceback.format_exc()}"))

    # -- stepping -----------------------------------------------------------
    def step_unit(self):
        """Advance one unit's turn. Returns the acted uid, 'ENDROUND' when the
        round's units are all done (resolves resources/cooldowns), or None if the
        game is over."""
        e = self.engine
        if e.winner:
            return None
        if not e._round_started:
            e._begin_round()
        uid = e.current_unit_id()
        if uid is None:
            e.end_round()
            self.recorder.record()
            return "ENDROUND"
        self._act(uid)
        e.advance_cursor()
        return uid

    def step_round(self):
        """Advance a whole round (all units + end-of-round resolution)."""
        guard = 0
        while not self.engine.winner and guard < 5000:
            r = self.step_unit()
            guard += 1
            if r == "ENDROUND":
                break

    def current_uid(self):
        e = self.engine
        if not e._round_started:
            return None
        return e.current_unit_id()
