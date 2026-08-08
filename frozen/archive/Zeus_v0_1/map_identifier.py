"""Identify the competition map from its size + our core position, then load
the whole board (walls, ore, both cores, symmetry) into map_info in one shot.

The map pool is fixed and baked into `map_data.py` (generated from the .map26
files). We verified across the whole pool that (width, height, core-origin)
uniquely names a map — for *either* of a map's two cores — so a unit that knows
its own core origin can name the exact map, and from the name reconstruct every
tile. The core identifies the map once and publishes a tiny index over comms;
every other unit reads that index and loads the same board.

Data is baked in (not read from disk) because the competition sandbox provides
neither filesystem access nor `__file__` at runtime.
"""
from __future__ import annotations

from fcode import Position
import map_info
from map_data import MAPS

# Sorted at generation time; this order defines the published index.
MAP_NAMES: list[str] = [m["name"] for m in MAPS]
NUM_MAPS: int = len(MAPS)


def _bits_for_max(maxval: int) -> int:
    """Bits needed to hold any value in 0..maxval (at least 1)."""
    b = 1
    while (1 << b) <= maxval:
        b += 1
    return b


# We publish (index + 1) so 0 reads as "unknown"; the field spans 0..NUM_MAPS.
# ceil(log2(NUM_MAPS)) is the classic answer; _bits_for_max keeps room for the
# +1 offset (identical for every N that isn't an exact power of two).
ID_BITS: int = _bits_for_max(NUM_MAPS)


def identify(width: int, height: int, core_origin: Position):
    """Return (index, side) for the pool map matching this size + own core
    origin, else None. `side` (0/1) is which of the map's two stored cores is
    ours — published alongside the index so a unit that never sees its own core
    still knows which core is friendly."""
    key = (core_origin.x, core_origin.y)
    for idx, m in enumerate(MAPS):
        if m["width"] != width or m["height"] != height:
            continue
        for side, c in enumerate(m["cores"]):
            if c == key:
                return idx, side
    return None


def load(idx: int, side: int) -> bool:
    """Load pool map `idx` into map_info wholesale. `side` says which stored
    core is ours. Returns False if the index/size doesn't match the live map."""
    if not (0 <= idx < NUM_MAPS):
        return False
    m = MAPS[idx]
    width, height = m["width"], m["height"]
    if width != map_info._width or height != map_info._height:
        return False
    cores = m["cores"]
    my_core = them = None
    if len(cores) >= 2:
        mine = cores[side] if side < len(cores) else cores[0]
        other = cores[1 - side] if side < 2 else cores[0]
        my_core = Position(mine[0], mine[1])
        them = Position(other[0], other[1])
    elif cores:
        my_core = Position(cores[0][0], cores[0][1])
    map_info.load_full_map(width, height, m["walls"], m["ore"], my_core, them)
    return True
