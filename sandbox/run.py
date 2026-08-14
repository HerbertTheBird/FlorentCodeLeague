"""Launch the interactive Florent sandbox.

    python sandbox/run.py [--map maps/nordkap.map26]
                          [--a bots/Heimdall_opening] [--b bots/Heimdall_opening]
                          [--seed 1]

Bot names resolve against ./bots (a bare name like Heimdall_v6 works too).
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from mapio import load_map          # noqa: E402
from botrunner import Match         # noqa: E402


def resolve_bot(name):
    for cand in (name, os.path.join(ROOT, name), os.path.join(ROOT, "bots", name)):
        if os.path.isdir(cand) and os.path.exists(os.path.join(cand, "main.py")):
            return os.path.abspath(cand)
    raise SystemExit(f"bot not found (need a dir with main.py): {name}")


def resolve_map(name):
    for cand in (name, os.path.join(ROOT, name), os.path.join(ROOT, "maps", name),
                 os.path.join(ROOT, "maps", name + ".map26")):
        if os.path.isfile(cand):
            return cand
    raise SystemExit(f"map not found: {name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default="nordkap")
    ap.add_argument("--a", default="Heimdall_opening")
    ap.add_argument("--b", default="Heimdall_opening")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    W, H, terrain, cores = load_map(resolve_map(args.map))
    match = Match(W, H, terrain, cores, resolve_bot(args.a), resolve_bot(args.b), seed=args.seed)

    from viewer import Viewer
    Viewer(match).run()


if __name__ == "__main__":
    main()
