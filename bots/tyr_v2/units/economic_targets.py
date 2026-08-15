"""Cheap bitboard queries for enemy economic targets.

The map is sticky: directions, buildings, and the last directly observed loaded
state remain known after leaving vision.  The queries here deliberately use
that remembered information, but invalidate when either structure or loaded
state changes.
"""

import map_info


_CACHE_KEY = None
_CACHE_VALUE = (0, 0)


def _receiver_accepts(source_n: int, receiver_n: int) -> bool:
    """Whether a resource output by ``source_n`` enters ``receiver_n``.

    ``_conv_reverse`` only says that the source outputs onto the receiver tile;
    it does not encode the receiver's input-side rules.  Conveyors reject input
    from their front (the tile they face), while splitters accept only from the
    back.  Core tiles accept from every cardinal side.
    """
    if (map_info._bm_their_core_area >> receiver_n) & 1:
        return True

    et_idx = map_info._building_et_idx[receiver_n]
    if et_idx == map_info._IDX_CONVEYOR:
        return map_info._building_conv_target[receiver_n] != source_n
    if et_idx != map_info._IDX_SPLITTER:
        return False

    direction_idx = map_info._building_dir[receiver_n]
    if direction_idx < 0:
        return False
    dx, dy = map_info._DIRECTION_DELTAS_I[direction_idx]
    rx = receiver_n % map_info._width
    ry = receiver_n // map_info._width
    return source_n == (rx - dx) + (ry - dy) * map_info._width


def _compute() -> tuple[int, int]:
    """Return ``(harvester_adjacent_conveyors, loaded_core_feeders)``.

    Both masks contain enemy CONVEYOR tiles only (not harvesters/splitters).
    The second is a strict subset whose remembered downstream graph reaches the
    enemy core and which has itself been observed holding titanium.
    """
    enemy = map_info._bm_team[1 - map_info._my_team_idx]
    enemy_conveyors = map_info._bm_et[map_info._IDX_CONVEYOR] & enemy
    enemy_harvesters = map_info._bm_et[map_info._IDX_HARVESTER] & enemy
    beside_harvesters = (
        map_info.expand_manhattan(enemy_harvesters)
        & ~enemy_harvesters
        & enemy_conveyors
    )

    if not map_info._bm_their_core_area or not enemy_conveyors:
        return beside_harvesters, 0

    conveyor_like = (
        map_info._bm_et[map_info._IDX_CONVEYOR]
        | map_info._bm_et[map_info._IDX_SPLITTER]
    )
    reverse = map_info._conv_reverse
    reaches_core = 0
    frontier = map_info._bm_their_core_area
    # At most one new conveyor tile per iteration is needed to make progress;
    # this board-sized bound also makes cycles harmless.
    for _ in range(map_info._width * map_info._height):
        upstream = 0
        receivers = frontier
        while receivers:
            receiver_bit = receivers & -receivers
            receiver_n = receiver_bit.bit_length() - 1
            receivers ^= receiver_bit
            sources = reverse[receiver_n] & conveyor_like & ~reaches_core
            while sources:
                source_bit = sources & -sources
                source_n = source_bit.bit_length() - 1
                sources ^= source_bit
                if _receiver_accepts(source_n, receiver_n):
                    upstream |= source_bit
        if not upstream:
            break
        reaches_core |= upstream
        frontier = upstream

    loaded_feeders = (
        reaches_core
        & enemy_conveyors
        & map_info._bm_conv_ti
    )
    return beside_harvesters, loaded_feeders


def masks() -> tuple[int, int]:
    global _CACHE_KEY, _CACHE_VALUE
    key = (
        map_info._struct_version,
        map_info._my_team_idx,
        map_info._bm_conv_ti,
        map_info._bm_their_core_area,
    )
    if key != _CACHE_KEY:
        _CACHE_VALUE = _compute()
        _CACHE_KEY = key
    return _CACHE_VALUE


def harvester_adjacent_conveyors() -> int:
    return masks()[0]


def loaded_enemy_core_feeders() -> int:
    return masks()[1]
