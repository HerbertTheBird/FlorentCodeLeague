"""Parse .map26 files into (width, height, terrain, cores)."""
from fcode_shim import Environment, Team

_TILE = {0: Environment.EMPTY, 1: Environment.WALL, 2: Environment.ORE_TITANIUM}


def _rv(b, i):
    r = s = 0
    while True:
        x = b[i]; i += 1; r |= (x & 0x7f) << s; s += 7
        if not x & 0x80:
            return r, i


def _row(p):
    tiles = []; j = 0
    while j < len(p):
        k, j = _rv(p, j); f = k >> 3; wt = k & 7
        if f == 1 and wt == 2:
            ln, j = _rv(p, j); end = j + ln
            while j < end:
                v, j = _rv(p, j); tiles.append(v)
        elif f == 1 and wt == 0:
            v, j = _rv(p, j); tiles.append(v)
        elif wt == 0:
            _, j = _rv(p, j)
        elif wt == 2:
            ln, j = _rv(p, j); j += ln
    return tiles


def load_map(path):
    b = open(path, "rb").read(); i = 0
    W = H = None; rows = []; cores = []
    while i < len(b):
        key, i = _rv(b, i); fn = key >> 3; wt = key & 7
        if wt == 0:
            v, i = _rv(b, i)
            if fn == 1: W = v
            elif fn == 2: H = v
        elif wt == 2:
            ln, i = _rv(b, i); p = b[i:i + ln]; i += ln
            if fn == 3:
                rows.append(_row(p))
            elif fn == 4:
                j = 0; team = None; x = y = 0
                while j < len(p):
                    k, j = _rv(p, j); f = k >> 3; w2 = k & 7
                    if w2 == 0:
                        v, j = _rv(p, j)
                        if f == 1: team = v
                    elif w2 == 2:
                        sl, j = _rv(p, j); sub = p[j:j + sl]; j += sl
                        if f == 3:
                            m = 0; sx = sy = 0
                            while m < len(sub):
                                kk, m = _rv(sub, m); ff = kk >> 3
                                vv, m = _rv(sub, m)
                                if ff == 1: sx = vv
                                elif ff == 2: sy = vv
                            x, y = sx, sy
                cores.append((Team.A if team == 1 else Team.B, x, y))
    terrain = [[_TILE.get(rows[y][x] if x < len(rows[y]) else 0, Environment.EMPTY)
                for x in range(W)] for y in range(H)]
    return W, H, terrain, cores
