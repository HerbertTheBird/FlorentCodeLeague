"""Recognise the map we are playing, and stop exploring it.

Every competition map is a published file, so `mapdata.py` can carry the walls
and the ore for all of them. This module is the part that decides whether that
table is allowed to speak: it narrows the candidates to one, checks the table
against everything the unit has actually looked at, and only then writes the
terrain into `map_info` as if we had explored the whole board on turn 0.

Three states, and the transitions only ever go forward:

    SEARCHING  candidates still being narrowed; map_info behaves exactly as it
               does without this module
    ACTIVE     one candidate left, terrain injected, still being checked
    OFF        no candidate can be right (unknown map, or an ACTIVE table was
               contradicted); we are back to ordinary exploration for good

Why the checking outlives identification. Adopting a table entry is a claim
about ~600 tiles made from the ~40 the unit could see on turn 0, and the pool
rotates -- a new map that happens to share a size and a core position with an
old one would be adopted on that evidence and then be wrong everywhere else.
So `verify_tile` re-reads the terrain of every tile the first time it genuinely
comes into view, compares it against what we injected, and one disagreement
sends us to OFF with all un-observed knowledge thrown away. The cost is one
`get_tile_env` per newly-seen tile, and it is paid only while ACTIVE.

Being wrong is therefore bounded: at worst we play some rounds on a false map
and then fall back to the behaviour we would have had anyway. Being right skips
the entire exploration phase.

`map_info` owns the state this writes; everything here reaches into it directly
rather than going through accessors, because it is doing the same job
`update_at` does and has to keep the same invariants -- most importantly that
core-footprint tiles are never marked seen (`build_core_areas` owns those).
"""

from __future__ import annotations   # module-level `X | None` annotations are
                                     # evaluated at import on older runtimes;
                                     # map_info and comms take the same care.

from fcode import Position

import map_info
import mapdata

# Stderr trace of every state transition. Off in shipped builds; the local
# harness (tools/hc_check.py) turns it on to assert that every map is
# recognised, on both sides, and never reverts.
DEBUG = False

SEARCHING, ACTIVE, OFF = 0, 1, 2

state = SEARCHING
name: str | None = None          # identified map, for logging/telemetry
_candidates: list | None = None  # [(name, core_a, core_b, sym, wall, ore)] decoded
_injected_core_n = -1            # tile index of the enemy core we synthesised


def init() -> None:
    """Pick the candidate maps matching this board's dimensions.

    Called from `map_info.init`, so `_width`/`_height` are set but nothing has
    been observed yet. Decoding is deferred to exactly the entries that share
    our dimensions -- at most four across the whole table.
    """
    global state, name, _candidates, _injected_core_n
    state = SEARCHING
    name = None
    _injected_core_n = -1
    entries = mapdata.MAPS.get((map_info._width, map_info._height))
    if not entries:
        _candidates = None
        state = OFF
        return
    _candidates = [
        (nm, core_a, core_b, sym, int(wall_hex, 16), int(ore_hex, 16))
        for (nm, core_a, core_b, sym, wall_hex, ore_hex) in entries
    ]


def consider() -> bool:
    """Narrow the candidates and inject when exactly one survives.

    Called once a turn from `map_info.update`, after the per-tile scan and
    before the symmetry solver, so it sees this turn's observations and can
    settle symmetry itself rather than racing the solver for it.

    Returns True when it changed map state (the caller must recompute derived
    masks).
    """
    global state, name, _candidates

    if state != SEARCHING:
        return False
    if map_info._my_core is None:
        # Nothing to key on yet. Every unit spawns in sight of our core, so this
        # only holds for the turn a unit is thrown somewhere blind by a launcher.
        return False

    mine = (map_info._my_core.x, map_info._my_core.y)
    observed = map_info._bm_seen_observed
    obs_wall = map_info._bm_env[map_info._IDX_ENV_WALL] & observed
    obs_ore = map_info._bm_env[map_info._IDX_ENV_ORE_TI] & observed

    # A candidate survives only if it agrees with every tile this unit has read
    # for itself. Comparing whole bitmasks makes that a couple of big-int ANDs
    # rather than a loop over the board.
    survivors = [
        c for c in _candidates
        if (mine == c[1] or mine == c[2])
        and c[4] & observed == obs_wall
        and c[5] & observed == obs_ore
    ]
    _candidates = survivors

    if not survivors:
        state = OFF
        _trace("no candidate matches observation")
        return False
    if len(survivors) > 1:
        return False

    entry = survivors[0]
    if not entry[3]:
        # No flip both preserves the terrain and swaps the cores, so we cannot
        # hand map_info a symmetry, and half of what injection buys is the enemy
        # core. Not worth a special case for a map that does not exist yet.
        state = OFF
        _trace(f"{entry[0]}: no usable symmetry")
        return False

    _inject(entry)
    name = entry[0]
    state = ACTIVE
    _trace(f"ACTIVE {name} obs={observed.bit_count()}")
    return True


def verify_tile(n: int, env_idx: int) -> None:
    """Check a directly-observed tile against what we injected for it.

    `update_at` calls this the first time a tile is genuinely read while ACTIVE.
    A mismatch means the table is not this map, so throw the table away.
    """
    bit = 1 << n
    if map_info._bm_env[env_idx] & bit:
        return
    # Correct this tile before reverting, not after: it is now an observed tile
    # still carrying the table's answer, and `revert` both keeps observed tiles
    # and re-derives the symmetry from them.
    for i in range(map_info._NUM_ENV):
        map_info._bm_env[i] &= ~bit
    map_info._bm_env[env_idx] |= bit
    revert()


def _inject(entry) -> None:
    """Write the whole board into map_info as known terrain.

    Deliberately a *replacement* of the environment masks rather than a merge:
    tiles filled in earlier by the symmetry solver are inferences, and the table
    -- which every observed tile has just agreed with -- is better than any of
    them. Observed tiles are unaffected either way, since they are what the
    table was checked against.
    """
    global _injected_core_n
    _, core_a, core_b, sym, wall, ore = entry
    mi = map_info

    mine = (mi._my_core.x, mi._my_core.y)
    theirs = core_b if mine == core_a else core_a
    mi._their_core = Position(theirs[0], theirs[1])

    # Tell map_info the symmetry outright. `flip()` picks the first live flag in
    # hor/ver/rot order, so exactly one flag must be set, and it must be the one
    # the generator verified maps core A onto core B.
    mi._hor_sym = sym == "h"
    mi._ver_sym = sym == "v"
    mi._rot_sym = sym == "r"
    mi._solved_sym = True

    # Synthesise the enemy core the same way update()'s symmetry solve does: one
    # anchor tile with the sentinel id -1, then build_core_areas() spreads it
    # over the 2x2 and claims those tiles.
    #
    # Unless we have actually seen it. A unit can be looking at the enemy core
    # before it has identified the map (a launcher throw, or simply a small
    # board), and then update_at has already recorded the real entity id and the
    # real HP. Overwriting those with a placeholder and full health would throw
    # away the only live reading of the thing we are trying to kill.
    n = mi._their_core.x + mi._their_core.y * mi._width
    if mi._building_et_idx[n] != mi._IDX_CORE:
        mi._building_id[n] = -1
        mi._building_et_idx[n] = mi._IDX_CORE
        mi._building_hp[n] = mi.GameConstants.CORE_MAX_HP
        mi._bm_et[mi._IDX_CORE] |= 1 << n
        mi._bm_any_building |= 1 << n
        mi._bm_team[1 - mi._my_team_idx] |= 1 << n
        _injected_core_n = n
    mi.build_core_areas()
    mi._predicted_enemy_core = mi._their_core

    # Core footprints stay unseen: update_at returns before touching their
    # env/seen state and every reader relies on that, so filling them in here
    # would brand eight tiles of core as permanently-seen floor.
    protect = mi._bm_my_core_area | mi._bm_their_core_area
    keep = mi._board_mask & ~protect
    wall &= keep
    ore &= keep
    mi._bm_env[mi._IDX_ENV_WALL] = wall
    mi._bm_env[mi._IDX_ENV_ORE_TI] = ore
    mi._bm_env[mi._IDX_ENV_EMPTY] = keep & ~wall & ~ore
    mi._bm_seen = keep

    # Walls feed _bm_blocked and both gunner-ray masks, all memoised on the
    # structural version; without the bump the caller's recompute is a no-op.
    mi._struct_version += 1


def revert() -> None:
    """Throw the table away and go back to what we had actually seen.

    Only reachable from `verify_tile`, i.e. the board contradicted an ACTIVE
    table. Everything injected has to go, including the symmetry -- it came from
    the same source -- leaving map_info in the state ordinary exploration would
    have produced, minus any tiles a teammate relayed while we were ACTIVE
    (those arrived as no-ops, because the tiles already counted as seen).
    """
    global state, name, _injected_core_n
    if state != ACTIVE:
        return
    mi = map_info

    observed = mi._bm_seen_observed
    for i in range(len(mi._bm_env)):
        mi._bm_env[i] &= observed
    mi._bm_seen = observed

    if _injected_core_n >= 0 and mi._building_id[_injected_core_n] == -1:
        for x in range(mi._their_core.x, mi._their_core.x + 2):
            for y in range(mi._their_core.y, mi._their_core.y + 2):
                m = x + y * mi._width
                bit = 1 << m
                for i in range(mi._NUM_ET):
                    mi._bm_et[i] &= ~bit
                for i in range(mi._NUM_TEAM):
                    mi._bm_team[i] &= ~bit
                mi._bm_any_building &= ~bit
                mi._building_id[m] = 0
                mi._building_et_idx[m] = -1
                mi._building_hp[m] = -1
        mi._their_core = None
        mi._bm_their_core_area = 0
        mi.build_core_areas()
    _injected_core_n = -1

    # Re-derive symmetry from observations alone. Cheaper to redo the whole
    # board once than to have tracked which eliminations came from the table.
    mi._hor_sym = mi._ver_sym = mi._rot_sym = True
    mi._solved_sym = False
    mask = observed
    while mask:
        lsb = mask & -mask
        mask ^= lsb
        n = lsb.bit_length() - 1
        for env_idx in range(mi._NUM_ENV):
            if (mi._bm_env[env_idx] >> n) & 1:
                mi.note_symmetry_conflict(n, env_idx)
                break
    mi._predicted_enemy_core = mi._compute_predicted_enemy_core()

    mi._struct_version += 1
    _trace(f"REVERT from {name}")
    state = OFF
    name = None


def _trace(msg: str) -> None:
    if DEBUG:
        import sys
        print(f"HC r{map_info._rc.get_current_round()} "
              f"u{map_info._rc.get_id()} {map_info._my_team}: {msg}",
              file=sys.stderr)
