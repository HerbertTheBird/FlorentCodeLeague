#!/usr/bin/env python3
"""Step through one game and say, round by round, how it was actually lost.

`compose` gives end-of-game totals and `curve` gives a coarse economy trace, but
neither answers "what killed us and when did it start". This walks the damage
events, attributes each one to the entity that dealt it, and reconstructs the
timeline that matters:

  - the first round our core took damage, and from what
  - sustained damage rate on our core, and the projected death round from it
  - what each side had built by the time the bleeding started
  - what we did in response, if anything

Damage magnitude identifies the source unambiguously, because every unit type
does a distinct amount: 7 = gunner, 18 = sentinel, 2 = builder attack, 4 = heal
(recorded as negative damage / healing events are separate).

    python3 tools/postmortem.py <replay> [--side A|B]

`--side` is which side we were; default A.
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay import Game, fields, sub, get, PAYLOAD_KIND, _pos, _signed  # noqa: E402

# Damage per shot is a fingerprint: no two attackers share a value.
DAMAGE_SOURCE = {7: "gunner", 18: "sentinel", 2: "builder"}


def walk(game: Game, us: int):
    """Yield (round, kind, payload) for spawns and damage, in order."""
    # A turn holds repeated events under field 1; the EVENT TYPE is the inner
    # field number (1 spawn, 3 death, 5 damage), not the outer one.
    seen_spawn = set()
    for rnd, turn in enumerate(game.turns):
        for ef, _w, ev in fields(turn):
            if ef != 1:
                continue
            for kind, _kw, body in fields(ev):
                if kind == 1:
                    ent = sub(body, 1)
                    if ent is None:
                        continue
                    payload = [f for f, _w2, _v in fields(ent) if f >= 6]
                    name = PAYLOAD_KIND.get(payload[0] if payload else -1)
                    eid = get(ent, 1)
                    if name is None or eid in seen_spawn:
                        continue
                    seen_spawn.add(eid)
                    yield rnd, "spawn", (get(ent, 2, 0), name, _pos(sub(ent, 3)), eid)
                elif kind == 5:
                    yield rnd, "damage", (get(body, 1), _signed(get(body, 2, 0)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("replay")
    ap.add_argument("--side", default="A", choices=["A", "B"])
    ap.add_argument("--until", type=int, default=0,
                    help="only report builds up to this round (default: the round the core started dying)")
    args = ap.parse_args()

    game = Game(Path(args.replay))
    us = 0 if args.side == "A" else 1
    print(f"{Path(args.replay).name}  {game.width}x{game.height}  {len(game.turns)} turns  (we are {args.side})")

    builds = collections.defaultdict(list)     # round -> [(team, kind, pos)]
    dmg_by_round = collections.defaultdict(lambda: collections.Counter())
    for rnd, kind, payload in walk(game, us):
        if kind == "spawn":
            team, name, pos, _eid = payload
            builds[rnd].append((team, name, pos))
        else:
            _target, delta = payload
            src = DAMAGE_SOURCE.get(-delta if delta < 0 else delta)
            if src:
                dmg_by_round[rnd][src] += abs(delta)

    # Composition at any round, per side.
    def comp_at(limit):
        out = [collections.Counter(), collections.Counter()]
        for rnd, entries in builds.items():
            if rnd > limit:
                continue
            for team, name, _pos in entries:
                out[team][name] += 1
        return out

    rounds = sorted(dmg_by_round)
    if not rounds:
        print("no attributable damage in this replay")
        return 0

    first = rounds[0]
    print(f"\nfirst attributable damage: round {first}  ({dict(dmg_by_round[first])})")

    # Sustained damage: the window where damage is continuous to the end.
    total = collections.Counter()
    for r in rounds:
        total.update(dmg_by_round[r])
    print(f"total damage dealt in the game, by source: {dict(total)}")

    cut = args.until or first
    ours, theirs = comp_at(cut)[us], comp_at(cut)[1 - us]
    print(f"\nwhat was on the board at round {cut}:")
    kinds = ["builder", "harvester", "conveyor", "barrier", "gunner", "sentinel", "launcher"]
    print(f"  {'':10s} " + " ".join(f"{k[:5]:>6s}" for k in kinds))
    print(f"  {'us':10s} " + " ".join(f"{ours.get(k,0):6d}" for k in kinds))
    print(f"  {'them':10s} " + " ".join(f"{theirs.get(k,0):6d}" for k in kinds))

    print(f"\nbuild order to round {cut}:")
    for rnd in sorted(builds):
        if rnd > cut:
            break
        for team, name, pos in builds[rnd]:
            who = "US  " if team == us else "THEM"
            print(f"  t{rnd:4d} {who} {name:9s} {pos}")

    # Projected core death from the enemy's turret mix at the cut.
    rate = theirs.get("sentinel", 0) * 9 + theirs.get("gunner", 0) * 3.5
    if rate:
        print(f"\nat round {cut} their turrets project {rate:.1f} damage/round at a 500 HP core")
        print(f"  -> death around round {cut + 500 / rate:.0f}; the game ended at {len(game.turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
