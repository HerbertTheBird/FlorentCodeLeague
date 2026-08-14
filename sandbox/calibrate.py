"""Diff the real engine vs the sandbox round-by-round to find the first divergence."""
import sys, io, contextlib, pathlib, subprocess, tempfile, os
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT + "/tools"); sys.path.insert(0, ROOT + "/sandbox")
import replay as R
from mapio import load_map
from botrunner import Match
from fcode_shim import Team

BOT = ROOT + "/bots/Heimdall_opening"


def real_states(replay_path, cores):
    g = R.Game(pathlib.Path(replay_path))
    ent = {}
    for i, (team, x, y) in enumerate(cores, start=1):
        ent[i] = [0 if team == Team.A else 1, x, y, 500, 500]
    ti = [500, 500]; out = []
    for turn in g.turns:
        for ef, _w, ev in R.fields(turn):
            if ef != 1:
                continue
            for kind, _kw, body in R.fields(ev):
                if kind == 1:
                    e = R.sub(body, 1); eid = R.get(e, 1)
                    if eid is None:
                        continue
                    p = R._pos(R.get(e, 3)) or (0, 0)
                    ent[eid] = [R.get(e, 2, 0), p[0], p[1], R.get(e, 4, 0), R.get(e, 5, 0)]
                elif kind == 2:
                    eid = R.get(body, 1); p = R._pos(R.get(body, 2))
                    if eid in ent and p:
                        ent[eid][1], ent[eid][2] = p
                elif kind == 3:
                    ent.pop(R.get(body, 1), None)
                elif kind == 5:
                    eid = R.get(body, 1)
                    if eid in ent:
                        ent[eid][3] += R._signed(R.get(body, 2, 0))
                elif kind == 6:
                    a = R.sub(body, 1, 1); b = R.sub(body, 1, 2)
                    if a is not None: ti[0] = R.get(a, 1, ti[0])
                    if b is not None: ti[1] = R.get(b, 1, ti[1])
        ms = sorted((v[0], v[1], v[2], v[4]) for v in ent.values())
        out.append((ms, tuple(ti)))
    return out


def sand_states(mp, seed, n):
    W, H, t, cores = load_map(f"{ROOT}/maps/{mp}.map26")
    m = Match(W, H, t, cores, BOT, BOT, seed=seed)
    out = []
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        for _ in range(n):
            m.step_round(); e = m.engine
            ms = sorted((0 if en.team == Team.A else 1, en.x, en.y, en.max_hp) for en in e.entities.values())
            out.append((ms, (e.teams[Team.A].titanium, e.teams[Team.B].titanium)))
            if e.winner:
                break
    return out


def calibrate(mp, seed):
    rp = tempfile.mktemp(suffix=".replay26")
    subprocess.run([ROOT + "/.venv/bin/fcode", "run", BOT, BOT, f"{ROOT}/maps/{mp}.map26",
                    "--seed", str(seed), "--replay", rp], capture_output=True, cwd=ROOT)
    _, _, _, cores = load_map(f"{ROOT}/maps/{mp}.map26")
    real = real_states(rp, cores)
    sand = sand_states(mp, seed, len(real))
    os.unlink(rp)
    for i in range(min(len(real), len(sand))):
        rms, rti = real[i]; sms, sti = sand[i]
        if rms != sms or rti != sti:
            ent_diff = rms != sms
            ronly = [e for e in rms if e not in sms][:8]
            sonly = [e for e in sms if e not in rms][:8]
            return i, ("ENTITIES" if ent_diff else "titanium"), rti, sti, ronly, sonly, len(rms), len(sms)
    return min(len(real), len(sand)), "none", None, None, [], [], 0, 0


if __name__ == "__main__":
    cfgs = sys.argv[1:] or ["nordkap:1"]
    for cfg in cfgs:
        mp, seed = cfg.split(":"); seed = int(seed)
        i, what, rti, sti, ronly, sonly, rn, sn = calibrate(mp, seed)
        print(f"{mp}:{seed}  first divergence @ round {i}  ({what})")
        if what == "titanium":
            print(f"    real ti={rti}  sand ti={sti}")
        elif what == "ENTITIES":
            print(f"    real ti={rti} sand ti={sti}  counts real={rn} sand={sn}")
            print(f"    only-real (team,x,y,mhp): {ronly}")
            print(f"    only-sand (team,x,y,mhp): {sonly}")
