"""Live exact min-turns solver for the chip endgame (single healer).

Game: A (to move first) deals 2 dmg to one adjacent target per turn; the single
healer B either moves to an adjacent passable tile or heals +4 to an adjacent
target. A wins by taking any target to 0. We compute the minimum number of plies
for A to force a kill against an optimal B, and the optimal first target to hit.

Exact (no heuristics): forward-reachability from the start state, then a
retrograde AND-OR shortest-path (A-nodes minimise, B-nodes maximise, kill states
= 0). Integer-encoded states for speed. Returns None if A cannot force a kill.
"""

import heapq
from collections import deque


def solve(maxhalf, hp0, adj, heal_by_tile, b0, state_cap=60000):
    """maxhalf/hp0: per-target half-HP caps and current values.
    adj[t]: passable-neighbour tile indices of healer-tile t (movement graph).
    heal_by_tile[t]: target indices the healer can heal while standing on tile t.
    b0: healer start tile index.
    Returns (min_plies, best_target_index) or (None, None) if A can't force a kill
    (or the reachable set exceeds state_cap)."""
    k = len(maxhalf)
    P = [1] * k                         # mixed-radix place values for hp encoding
    for i in range(k - 2, -1, -1):
        P[i] = P[i + 1] * (maxhalf[i + 1] + 1)
    nt = len(adj)

    def enc(hp_idx, t, turn):
        return (hp_idx * nt + t) * 2 + turn

    hp0_idx = sum(hp0[i] * P[i] for i in range(k))
    start = enc(hp0_idx, b0, 0)

    succ = {}
    preds = {}
    kills = []
    seen = {start}
    dq = deque([start])
    while dq:
        s = dq.popleft()
        turn = s & 1
        rest = s >> 1
        t = rest % nt
        hp_idx = rest // nt
        # decode hp digits + detect a dead target
        hp = [0] * k
        r = hp_idx
        dead = False
        for i in range(k):
            hp[i] = r // P[i]
            r -= hp[i] * P[i]
            if hp[i] == 0:
                dead = True
        if dead:
            succ[s] = ()
            kills.append(s)
            continue
        outs = []
        if turn == 0:                   # A attacks: one target -1 half (=2 dmg)
            for i in range(k):
                outs.append(enc(hp_idx - P[i], t, 1))
        else:                           # B moves or heals
            for nb in adj[t]:
                outs.append(enc(hp_idx, nb, 0))
            for i in heal_by_tile[t]:
                if hp[i] < maxhalf[i]:
                    add = maxhalf[i] - hp[i]
                    if add > 2:
                        add = 2
                    outs.append(enc(hp_idx + add * P[i], t, 0))
        succ[s] = outs
        for o in outs:
            preds.setdefault(o, []).append(s)
            if o not in seen:
                seen.add(o)
                dq.append(o)
        if len(seen) > state_cap:
            return None, None

    # retrograde AND-OR min-plies
    val = {}
    bcount = {}
    pq = []
    for s in kills:
        val[s] = 0
        pq.append((0, s))
    heapq.heapify(pq)
    for s in seen:
        if (s & 1) == 1 and s not in val:
            bcount[s] = len(succ[s])
            if bcount[s] == 0:          # B has no legal move: A already won
                val[s] = 0
                heapq.heappush(pq, (0, s))
    while pq:
        d, s = heapq.heappop(pq)
        if val.get(s) != d:
            continue
        for p in preds.get(s, ()):
            if p in val:
                continue
            if (p & 1) == 0:            # A-node: first finalized successor is the min
                val[p] = d + 1
                heapq.heappush(pq, (d + 1, p))
            else:                       # B-node: finalized when the last (max) successor is
                bcount[p] -= 1
                if bcount[p] == 0:
                    val[p] = d + 1
                    heapq.heappush(pq, (d + 1, p))

    if start not in val:
        return None, None
    # optimal first move
    best_i, best_v = None, 1 << 30
    for i in range(k):
        ns = enc(hp0_idx - P[i], b0, 1)
        v = 0 if hp0[i] == 1 else val.get(ns, 1 << 30)
        if v < best_v:
            best_v, best_i = v, i
    return val[start], best_i
