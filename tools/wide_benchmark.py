#!/usr/bin/env python3
"""Wide passive benchmark: current bot against every reference opponent across
all 33 maps, both sides. Reports per-opponent and, for the whole pool, which
maps we lose on and how those games end.

    python3 tools/wide_benchmark.py Heimdall_v6 loki,Khaos,Hermod,Heimdall_v3,Champion_v49 out.json

benchmark_bots.py answers "does this beat that". This answers "what kind of
games are we losing", which is the question you want before writing any code --
it is what turned up that 78 of 90 losses are core destruction and 59 of those
land before turn 250.

Pick opponents for what you are testing: only Khaos fields sentinels, so a
turret-matchup change is invisible in a pool of loki/Hermod/Heimdall_v3."""
import json, subprocess, sys, pathlib, collections, concurrent.futures as cf
bot = sys.argv[1]; opps = sys.argv[2].split(","); outp = sys.argv[3]
maps = sorted(p.stem for p in pathlib.Path("maps").glob("*.map26"))
def one(a):
    opp, mp, side = a
    x, y = (f"bots/{bot}", f"bots/{opp}") if side=="A" else (f"bots/{opp}", f"bots/{bot}")
    r = subprocess.run(["fcode","run",x,y,f"maps/{mp}.map26","--seed","1","--tle","5000",
                        "--replay","/dev/null","--json"], capture_output=True, text=True)
    ln = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("{")), None)
    if not ln: return None
    d = json.loads(ln); us = side
    return dict(opp=opp, map=mp, side=side, won=d["winner"]==us, cond=d["win_condition"],
                turns=d["turns"], ti=d["a_titanium_collected"] if side=="A" else d["b_titanium_collected"],
                oti=d["b_titanium_collected"] if side=="A" else d["a_titanium_collected"])
jobs=[(o,m,s) for o in opps for m in maps for s in ("A","B")]
with cf.ThreadPoolExecutor(max_workers=3) as ex:
    rows=[r for r in ex.map(one, jobs) if r]
json.dump(rows, open(outp,"w"))
print(f"{'opponent':16s} {'record':>10s}  {'rate':>6s}")
for o in opps:
    rs=[r for r in rows if r["opp"]==o]; w=sum(r["won"] for r in rs)
    print(f"  {o:14s} {w:3d}-{len(rs)-w:<3d}  {100*w/max(len(rs),1):5.1f}%")
w=sum(r["won"] for r in rows)
print(f"  {'POOL':14s} {w:3d}-{len(rows)-w:<3d}  {100*w/max(len(rows),1):5.1f}%  ({len(rows)} matches)")
losses=[r for r in rows if not r["won"]]
print(f"\nlosses by condition: {collections.Counter(r['cond'] for r in losses).most_common()}")
bym=collections.Counter(r["map"] for r in losses)
print("worst maps:", ", ".join(f"{m}({c})" for m,c in bym.most_common(10)))
early=[r for r in losses if r["cond"]=="core_destroyed" and r["turns"]<250]
print(f"early core deaths (<t250): {len(early)} of {len(losses)} losses")
