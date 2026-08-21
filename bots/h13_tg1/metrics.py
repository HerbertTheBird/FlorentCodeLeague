"""Structured per-turn metadata, emitted as one compact JSON line per unit-turn.

Replaces the old free-text `log()` spew (which produced ~750KB of prose per game
and could only be read by eye). Local oarena replays capture bot stdout, so
anything emitted here is recoverable afterwards with
`tools/replay_state.py --meta` -- analysis becomes a query over a finished game
instead of another instrument-and-rerun cycle.

Ladder replays have stdout STRIPPED (measured: 0 lines from a match where the bot
was printing constantly), so this is a LOCAL forensics tool only. Ladder games
still need external reconstruction.

## Log rejections, not decisions

Every bug found on 2026-08-19 was "why didn't it do the obvious thing", and the
answer was always in a reject path, never in the chosen action:
  * harvest bailed "nothing reachable this turn" 7538 times against 19 accepts
  * 22 of 23 missed core heals had ZERO titanium
  * route candidates were silently dropped by `cost > ti`
A log of chosen actions shows none of that. So `rej()` is the important call
here, not `turn()`.

## Cost
Disabled it is one module-global truth test per call site. Enabled it costs
~21us/turn (measured: 906us vs 885us mean with the old prose logging), i.e. 0.2%
of the platform's 10ms budget -- so verbosity is not the constraint.
"""

import os

# Off unless explicitly asked for. The submitted bot ships with this False, so
# the emitter is a single global check per call site.
ENABLED = os.environ.get("HB_META", "") not in ("", "0", "false")

_rc = None
_round = 0
_uid = 0


def init(rc):
    global _rc
    _rc = rc


def begin(round_num: int, uid: int) -> None:
    """Called once per unit-turn before anything else is recorded."""
    global _round, _uid
    _round = round_num
    _uid = uid


def _emit(kind: str, fields: dict) -> None:
    parts = ['{"k":"', kind, '","r":', str(_round), ',"id":', str(_uid)]
    for key, value in fields.items():
        parts.append(',"')
        parts.append(key)
        parts.append('":')
        if isinstance(value, str):
            parts.append('"')
            parts.append(value)
            parts.append('"')
        elif isinstance(value, bool):
            parts.append("true" if value else "false")
        elif isinstance(value, tuple):
            parts.append("[%d,%d]" % value)
        elif isinstance(value, float):
            parts.append("%.2f" % value)
        else:
            parts.append(str(value))
    parts.append("}")
    print("".join(parts))


def turn(pos, ti: int, chosen: str, scores) -> None:
    """The per-turn decision: what every state offered and which one won."""
    if not ENABLED:
        return
    _emit("turn", {
        "pos": (pos.x, pos.y),
        "ti": ti,
        "chose": chosen,
        "sc": "|".join("%s=%s" % (n, s) for n, s in scores),
    })


def rej(state: str, reason: str, **extra) -> None:
    """A candidate this state WANTED but could not take, and why.

    This is the call that finds bugs. `reason` should be a short stable slug --
    unreachable, unroutable, too_expensive, blacklisted, no_candidates,
    broke, blocked -- so occurrences can be counted across a game.
    """
    if not ENABLED:
        return
    f = {"st": state, "why": reason}
    f.update(extra)
    _emit("rej", f)


def act(state: str, what: str, **extra) -> None:
    """An action actually taken, for pairing against rejections."""
    if not ENABLED:
        return
    f = {"st": state, "do": what}
    f.update(extra)
    _emit("act", f)
