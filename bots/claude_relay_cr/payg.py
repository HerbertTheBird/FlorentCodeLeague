"""Pay-as-you-go cost bookkeeping shared by the harvest and route states.

Both states price a candidate project, record the quote against the tile, and
skip that tile on later turns while the quote is still above our balance. The
two cost maps stay separate -- harvest quotes harvester + chain, route quotes
chain alone -- but the bookkeeping over them is identical.
"""
from _config import COST_MAP_TTL


def too_expensive(cost_map: dict[int, tuple[int, int]], ti: int, current: int) -> int:
    """Bitmask of tiles in `cost_map` we know we can't afford right now.

    Evicts quotes older than COST_MAP_TTL as it goes, so callers that skip this
    read on a turn only defer the eviction -- staleness is decided by the
    recorded round, not by how often we sweep.
    """
    result = 0
    stale = []
    for n, (cost, turn) in cost_map.items():
        if turn + COST_MAP_TTL < current:
            stale.append(n)
            continue
        if cost > ti:
            result |= 1 << n
    for n in stale:
        del cost_map[n]
    return result
