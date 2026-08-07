"""Heimdall v6 sentry-block defence — shared geometry, roles, and thresholds.

The idea exploits two Titan rules at once:

  * builder bots move **cardinally only**, so a single bot standing on the tile
    directly in front of an enemy along one axis makes progress along that axis
    *impossible*, not merely slow; and
  * the engine runs units in ascending entity id order (verified at runtime), so
    a freshly spawned defender — which always holds the highest id on the board —
    acts **after** every pre-existing enemy bot. It therefore sees the enemy's
    post-move position and can restore the block in the same round.

Put together: park a blocker on the enemy's front tile along the axis it has
furthest to travel, then mirror every perpendicular step it takes. The gap along
the blocked axis can never shrink, so that enemy never reaches our core again.

A blocker does not always owe that mirror step, though, and this is what makes
the whole thing affordable. The step is only owed when the enemy's forward tile
is somewhere it could actually go; if terrain, a building, or a barrier we laid
already seals it, the enemy cannot advance and the turn is ours (`denies_advance`).
So a blocker spends its free turns barriering the two tiles beside itself
(`flank_tiles`) — precisely the tiles that would become the enemy's forward tile
after a sideways step. Once both flanks are sealed the enemy gains nothing by
moving at all, the blocker never owes another step, and every subsequent turn is
free to finish walling it in. Each barrier buys the free turns that pay for the
next one.

Delivery is what makes this cheap. A `launcher` two tiles out from the core
(toward the map centre) both *sees* further up the approach than the core does
and can *throw* a builder up to dist^2 26 in one action. Because the core has the
lowest id on the team it always spawns before the launcher acts, so a defender
can be spawned and thrown to its blocking tile within a single round.

Role summary:
  sentry launcher  (turret_launcher)  detect -> raise alarm -> throw the defender
  core             (units/core)       answer the alarm by spawning the defender
  defender         (states/defend)    hold the block tile, seal flanks, wall in
  siege breaker    (states/defend)    screen or destroy turrets shooting the core
  supply cutter    (states/cut)       steal enemy line ends; wall their core feed

`map_info.ti_reserve()` holds one builder-bot's spawn cost unspent while a threat
is live, so the core can always answer an alarm.

Measured results
----------------
66 matches per pairing — every map in `maps/`, both sides — via
`tools/benchmark_bots.py`. Win rate for this bot:

                    was      now
    vs loki        34.8%    62.1%
    vs Khaos       54.5%    65.2%
    vs Hermod      34.8%    71.2%
    vs Heimdall v3 45.5%    48.5%
    vs Ladder_v36     -      72.7%   <- the column that matters

Measured against Ladder_v36, both sides, 66 matches each:

    tap only (defence on)                     57.6%
    aggressive turrets only                   60.6%
    defence off only                          62.1%
    defence off + turrets + gated tap         63.6%
    ... + rushing the enemy core to seal it    72.7%   <- shipped
    ... + also barriering enemy ore harder     66.7%   (worse: they compete)
    defence off + turrets + ungated tap       57.6%
    gated tap alone                           47.0%

Note the last two. The tap's gap gate loses 10 points on its own and gains 6 in
combination -- these interact, and the combination had to be measured rather
than reasoned about.

The four fixed opponents are old bots the field has long overtaken, and a change
can gain several points against them while losing unrated matches against real
opponents. Ladder_v36 is a snapshot of what is actually playing ranked; treat
that column as the signal.

Where the gains came from, each measured over the full suite:

    43.0 -> 50.4   siege breaker (see `core_besiegers`)
    50.4 -> 53.0   trapping, and the wider threat radius it justified
    53.0 -> 54.5   blockers stop owing mirror steps (see `denies_advance`)
    54.5 -> 57.2   cutting the enemy supply line (see `units/states/cut.py`)
    57.2 -> 56.4   fixing the core's spawn gate (a wash overall, but it lifted
                   three opponents of four and removed a dimensional bug)
    56.4 -> 61.4   the economy guard below
    61.4 -> 62.5   stealing enemy conveyor line ends (see `units/states/cut.py`)
    62.5 -> 64.4   unwedging `cut`: a bot parked on a feed tile made every action
                   illegal forever while score() re-picked the same tile
    64.4 -> 68.6   economy first (harvest 14 / route 13) -- REVERTED. It scored
                   68.6% here and lost unrated matches against real ladder
                   opponents. `select_best_state` breaks as soon as
                   best_score >= state.MAX_SCORE, so lifting harvest and route
                   above the denial family stopped cut/steal/tap/sentry being
                   evaluated at all whenever a builder had ore work. The gain
                   was against a suite the field has outgrown; the loss was real.
    v36 + tap      +7.6 head-to-head against the live ladder build (38W-28L)

Both of the big wins came from reading match data rather than tuning. The first:
losses were overwhelmingly `core_destroyed` at a median of ~99 rounds, and
instrumenting one showed an enemy gunner three tiles out taking the core from
500 HP to 0 by round 60 while our builders tried to heal through it. The second:
long losses showed us holding *more* buildings than the winner while collecting a
third of their titanium — because those buildings were 32 barriers, 2 conveyors
and no harvesters.
"""

from fcode import Controller, EntityType, GameConstants, Position

import map_info

# --- geometry -------------------------------------------------------------
# Chebyshev distance from the core's 2x2 footprint at which the sentry launcher
# sits. 2 keeps it off the core's spawn ring (so it never eats a spawn tile)
# while still leaving core-ring tiles inside its pickup radius.
SENTRY_RING = 2
PICKUP_R2 = 2                                         # launcher pickup reach (dist^2)
THROW_R2 = GameConstants.LAUNCHER_VISION_RADIUS_SQ    # 26 — launcher throw reach

# --- policy ---------------------------------------------------------------
# Radius (in tiles) around the core inside which an enemy bot earns a blocker.
# The most sensitive number in the design, and its best value flipped completely
# once trapping existed. While a block was an open-ended commitment — one builder
# shadowing one enemy forever — engaging wide was ruinous and 3 was optimal. Now
# that a pinned enemy gets walled in and permanently removed, a block is a
# terminal investment rather than a standing cost, and meeting them further out
# is better. Swept 66 matches per cell:
#
#     radius        3      5      7      9     11     14
#     vs loki    53.0   56.1   66.7   54.5   57.6   62.1
#     vs Hermod  50.0   57.6   51.5      -      -      -
#
# 7 looks best against loki but that is a single-opponent fit: over the whole
# suite it scored 52.7% against 5's 53.0%, and its profile is lopsided (loki
# 66.7% but Hermod 51.5% and Heimdall v3 50.0%, both ~9 points below what 5
# gets). 5 is the more consistent choice and is what ships.
THREAT_RADIUS = 5
# Cap on concurrent blockers, so a swarm can't drain us into spawning nothing but
# defenders.
MAX_BLOCKERS = 3

# Master switch for the sentry-block defence — the blocker state, the trapper,
# the core's dispatch, the sentry alarm, and the sentry build gate all funnel
# through `threatening_enemies()`, so this turns the whole thing on or off in one
# edit. It does NOT touch the siege breaker (`core_besiegers`), which is separate
# and always on.
#
# Worth re-measuring after any change to the trap logic: before trapping existed
# the block was a net negative over the full suite (50.4% on, 53.4% off), and it
# is trapping that pays for it.
ENABLED = False


# --- economy guard ---------------------------------------------------------
# Barriers are 3 Ti, and every barrier-laying job in this bot — trapping, flank
# sealing, supply cutting — outranks the harvest and route states. Left ungated
# that is a runaway: instrumented long games finished with 32 barriers, 2
# conveyors and *no harvesters*, collecting a third of the opponent's titanium
# while nominally holding more buildings than them. Denial is only worth doing
# from a working economy, so barrier work waits for one and then stays bounded.
MIN_HARVESTERS = 2
MAX_BARRIERS = 12


def my_count(idx: int) -> int:
    return (map_info._bm_et[idx] & map_info._bm_team[map_info._my_team_idx]).bit_count()


def economy_ok() -> bool:
    """True once we have enough harvesters to afford spending turns on denial."""
    return my_count(map_info._IDX_HARVESTER) >= MIN_HARVESTERS


def barrier_budget_ok() -> bool:
    """True while we are under the standing barrier cap."""
    return my_count(map_info._IDX_BARRIER) < MAX_BARRIERS


def may_wall() -> bool:
    """Gate for every optional barrier: economy first, and never unbounded.

    Deliberately not applied to the siege breaker's screen — a turret already
    shooting the core is an emergency, and 3 Ti to stop 10 damage a round is
    worth it whatever the economy looks like.
    """
    return economy_ok() and barrier_budget_ok()


def threat_r2() -> int:
    return THREAT_RADIUS * THREAT_RADIUS


def core_footprint() -> tuple[int, int, int, int] | None:
    """(x0, x1, y0, y1) inclusive bounds of my core's 2x2, or None if unknown."""
    core = map_info._my_core
    if core is None:
        return None
    return core.x, core.x + 1, core.y, core.y + 1


def core_offset(pos: Position) -> tuple[int, int] | None:
    """Signed (dx, dy) a bot at `pos` must still travel to stand on my core.

    Zero on an axis means the bot is already within the footprint's span there.
    """
    box = core_footprint()
    if box is None:
        return None
    x0, x1, y0, y1 = box
    dx = 0 if x0 <= pos.x <= x1 else (x0 - pos.x if pos.x < x0 else x1 - pos.x)
    dy = 0 if y0 <= pos.y <= y1 else (y0 - pos.y if pos.y < y0 else y1 - pos.y)
    return dx, dy


def _front_tile(enemy: Position, dx: int, dy: int, use_x: bool) -> Position | None:
    """The tile one cardinal step from `enemy` toward our core on the chosen axis."""
    if use_x:
        if dx == 0:
            return None
        tile = Position(enemy.x + (1 if dx > 0 else -1), enemy.y)
    else:
        if dy == 0:
            return None
        tile = Position(enemy.x, enemy.y + (1 if dy > 0 else -1))
    if not map_info.in_bounds(tile):
        return None
    return tile


def block_tile_axis(enemy: Position, axis: str | None = None):
    """(tile, axis) a defender must hold to stop `enemy` closing on our core.

    We block the axis the enemy has *furthest* left to travel: that axis is
    guaranteed to have a non-zero offset (so a "front" tile exists at all), and
    it is the axis the enemy is most committed to. Blocking it freezes that gap
    forever — the enemy may still slide along the other axis, but sliding never
    brings it closer.

    `axis` pins the choice to one already in force. A blocker latches the axis it
    started on, because re-deriving it every round makes the target tile jump
    between the two sides of the enemy whenever terrain changes which one is
    passable — and a blocker chasing a jumping tile blocks nothing. Falls back to
    the other axis only when the pinned one has no usable front tile.
    """
    off = core_offset(enemy)
    if off is None:
        return None, None
    dx, dy = off
    if axis is not None:
        first = (axis == "x")
    else:
        first = abs(dx) >= abs(dy)
    for use_x in (first, not first):
        tile = _front_tile(enemy, dx, dy, use_x)
        if tile is not None and map_info.is_passable(tile):
            return tile, ("x" if use_x else "y")
    return None, None


def block_tile(enemy: Position, axis: str | None = None) -> Position | None:
    return block_tile_axis(enemy, axis)[0]


def forward_tile(enemy: Position, axis: str) -> Position | None:
    """The tile `enemy` must step onto to close `axis` toward our core.

    Unlike `block_tile_axis` this never falls back to the other axis. A blocker
    that has pinned an axis wants to know about *that* axis specifically: if the
    forward tile there is already sealed the enemy cannot advance, which is the
    whole point, and silently switching axes would send the blocker chasing a
    tile it never needed.
    """
    off = core_offset(enemy)
    if off is None:
        return None
    dx, dy = off
    if axis == "x":
        if dx == 0:
            return None
        tile = Position(enemy.x + (1 if dx > 0 else -1), enemy.y)
    else:
        if dy == 0:
            return None
        tile = Position(enemy.x, enemy.y + (1 if dy > 0 else -1))
    return tile if map_info.in_bounds(tile) else None


def denies_advance(tile: Position | None, my_pos: Position | None = None) -> bool:
    """True if an enemy builder cannot step onto `tile` this turn.

    Terrain, any building, any bot, the map edge, and the asking blocker itself
    all count. This is what lets a blocker know it does *not* owe a mirror step:
    if the enemy's forward tile is denied by something other than us having to
    stand on it, we are free to spend the turn building instead of shadowing.
    """
    if tile is None:
        return True
    if not map_info.in_bounds(tile):
        return True
    if my_pos is not None and tile == my_pos:
        return True
    if not map_info.is_passable(tile):
        return True
    bit = 1 << (tile.x + tile.y * map_info._width)
    return bool((map_info._bm_friendly_bots | map_info._bm_enemy_bots) & bit)


def flank_tiles(block: Position, axis: str) -> list[Position]:
    """The two tiles beside `block`, perpendicular to the blocked axis.

    These are exactly the tiles that become the enemy's forward tile if it steps
    sideways, and they are cardinally adjacent to the blocker — so the blocker
    can build them itself. Sealing both means a sideways step no longer wins the
    enemy anything, so the blocker stops owing mirror steps entirely and is free
    from then on. That is the compounding move in this whole design: each flank
    barrier buys the free turns that pay for the next one.
    """
    if axis == "x":
        return [Position(block.x, block.y - 1), Position(block.x, block.y + 1)]
    return [Position(block.x - 1, block.y), Position(block.x + 1, block.y)]


def is_blocked(enemy: Position, block: Position | None = None) -> bool:
    """True if one of my builder bots already holds `enemy`'s block tile.

    Note `map_info._bm_friendly_bots` excludes the unit doing the asking, so
    callers that might themselves be the blocker must handle that case.
    """
    if block is None:
        block = block_tile(enemy)
    if block is None:
        return True  # nothing to hold -> nothing to dispatch
    bit = 1 << (block.x + block.y * map_info._width)
    return bool(map_info._bm_friendly_bots & bit)


_SIEGE_TURRETS = frozenset({EntityType.GUNNER, EntityType.SENTINEL})


def core_tiles() -> list[Position]:
    box = core_footprint()
    if box is None:
        return []
    x0, x1, y0, y1 = box
    return [Position(x, y) for x in (x0, x1) for y in (y0, y1)]


def core_besiegers(rc: Controller) -> list[tuple[Position, object, object, Position]]:
    """Enemy turrets that can shoot my core right now, nearest first.

    Returned as (turret position, entity type, facing, the core tile it hits).
    Legality is delegated to `can_fire_from`, so this accounts for the real
    line-of-sight rules rather than re-deriving them.

    This is the thing that actually kills us. An enemy gunner planted three tiles
    from the core does 10 damage a round — 500 HP gone in fifty rounds — and no
    amount of healing keeps up, because a heal is 4 HP for a whole builder turn.
    Only breaking the shot works.
    """
    out = []
    my_team = map_info._my_team
    mine = core_tiles()
    if not mine:
        return out
    for bid in rc.get_nearby_buildings():
        if rc.get_team(bid) == my_team:
            continue
        etype = rc.get_entity_type(bid)
        if etype not in _SIEGE_TURRETS:
            continue
        pos = rc.get_position(bid)
        try:
            facing = rc.get_direction(bid)
        except Exception:
            continue
        for tile in mine:
            if rc.can_fire_from(pos, facing, etype, tile):
                out.append((pos, etype, facing, tile))
                break
    my_pos = map_info._my_pos
    out.sort(key=lambda t: my_pos.distance_squared(t[0]))
    return out


def screen_tiles(rc: Controller, turret: Position, facing, etype,
                 hit: Position) -> list[Position]:
    """Empty tiles on a gunner's ray where a barrier would break the shot.

    Gunners fire along a single ray and anything solid stops it, so three
    titanium of barrier turns off a turret that cost them ten — and when they
    shoot the barrier down (30 HP, three shots) rebuilding it costs us three
    again against six of their ammo. That trade is why screening comes before
    trying to kill the turret.

    Sentinels ignore line of sight entirely, so they get an empty list: the only
    answer to those is to destroy them.
    """
    if etype != EntityType.GUNNER:
        return []
    dx, dy = facing.delta()
    if dx == 0 and dy == 0:
        return []
    # Judged from remembered map state, not live queries: the ray runs from the
    # turret toward our core and its far end is regularly outside the vision of
    # the builder asking, which makes `is_tile_empty` raise.
    w = map_info._width
    free = (map_info._bm_seen
            & ~map_info._bm_any_building
            & ~map_info._bm_env[map_info._IDX_ENV_WALL]
            & ~map_info._bm_friendly_bots
            & ~map_info._bm_enemy_bots)
    out = []
    x, y = turret.x + dx, turret.y + dy
    # Walk the ray from the turret up to (not including) the core tile it hits.
    for _ in range(4):
        if (x, y) == (hit.x, hit.y):
            break
        tile = Position(x, y)
        if not map_info.in_bounds(tile):
            break
        if free & (1 << (x + y * w)):
            out.append(tile)
        x += dx
        y += dy
    # Nearest to the core first: a screen there also covers a turret that later
    # rotates onto a different approach.
    out.reverse()
    return out


def free_exits(enemy: Position, ignore: Position | None = None) -> list[Position]:
    """Cardinal tiles the enemy bot could still step onto.

    `ignore` is the asking blocker's own tile, which is not an escape route while
    it stands there but also is not somewhere we can build.
    """
    out = []
    for d in map_info._CARDINAL:
        tile = map_info.pos_add(enemy, d)
        if not map_info.in_bounds(tile):
            continue
        if ignore is not None and tile == ignore:
            continue
        if not map_info.is_passable(tile):
            continue
        bit = 1 << (tile.x + tile.y * map_info._width)
        if (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & bit:
            continue
        out.append(tile)
    return out


def threatening_enemies() -> list[tuple[int, int, Position]]:

    """Enemy builder bots inside the threat radius of my core, nearest first.

    Returned as (distance_squared_to_core, entity id, position). The id lets a
    blocker keep tracking the same bot across rounds instead of re-targeting
    whichever one happens to be nearest this turn.
    """
    if not ENABLED:
        return []
    box = core_footprint()
    if box is None:
        return []
    x0, x1, y0, y1 = box
    limit = threat_r2()
    w = map_info._width
    out = []
    for p in map_info.iter_mask(map_info._bm_enemy_bots):
        cx = min(max(p.x, x0), x1)
        cy = min(max(p.y, y0), y1)
        d2 = (p.x - cx) ** 2 + (p.y - cy) ** 2
        if d2 <= limit:
            out.append((d2, map_info._bot_at.get(p.x + p.y * w, -1), p))
    out.sort()
    return out


# --------------------------------------------------------------------------- #
# Sentry launcher siting
# --------------------------------------------------------------------------- #
_sentry_cache_key: Position | None = None
_sentry_cache: tuple[Position, ...] = ()


def _centre_bias() -> Position:
    """The point the sentry should face: the enemy core once symmetry is solved,
    otherwise the map centre. Both put the sentry on the approach lane."""
    predicted = map_info._predicted_enemy_core
    if predicted is not None:
        return predicted
    return Position(map_info._width // 2, map_info._height // 2)


def sentry_candidates() -> tuple[Position, ...]:
    """Tiles where the sentry launcher belongs, best first.

    A candidate sits exactly SENTRY_RING Chebyshev steps from the core footprint,
    is on the map, is not a wall, and keeps at least one core spawn-ring tile
    inside launcher pickup range (otherwise the core could never hand it a
    defender). Ranked by proximity to the enemy-facing bias point, with the tile
    index as a deterministic tiebreak so every unit agrees on the ordering.
    """
    global _sentry_cache_key, _sentry_cache
    core = map_info._my_core
    if core is None:
        return ()
    if _sentry_cache_key == core and _sentry_cache:
        return _sentry_cache

    box = core_footprint()
    x0, x1, y0, y1 = box
    w, h = map_info._width, map_info._height
    walls = map_info._bm_env[map_info._IDX_ENV_WALL]
    ring = spawn_ring()
    bias = _centre_bias()

    scored = []
    for x in range(x0 - SENTRY_RING, x1 + SENTRY_RING + 1):
        for y in range(y0 - SENTRY_RING, y1 + SENTRY_RING + 1):
            if not (0 <= x < w and 0 <= y < h):
                continue
            # Chebyshev distance to the footprint, which must be exactly the ring.
            cheb = max(
                0 if x0 <= x <= x1 else min(abs(x - x0), abs(x - x1)),
                0 if y0 <= y <= y1 else min(abs(y - y0), abs(y - y1)),
            )
            if cheb != SENTRY_RING:
                continue
            n = x + y * w
            if walls & (1 << n):
                continue
            tile = Position(x, y)
            if not any(tile.distance_squared(r) <= PICKUP_R2 for r in ring):
                continue
            scored.append((tile.distance_squared(bias), n, tile))

    scored.sort()
    _sentry_cache_key = core
    _sentry_cache = tuple(tile for _d, _n, tile in scored)
    return _sentry_cache


def spawn_ring() -> tuple[Position, ...]:
    """Tiles immediately surrounding the core's 2x2 — the only legal spawn tiles."""
    box = core_footprint()
    if box is None:
        return ()
    x0, x1, y0, y1 = box
    w, h = map_info._width, map_info._height
    out = []
    for x in range(x0 - 1, x1 + 2):
        for y in range(y0 - 1, y1 + 2):
            if x0 <= x <= x1 and y0 <= y <= y1:
                continue
            if 0 <= x < w and 0 <= y < h:
                out.append(Position(x, y))
    return tuple(out)


def sentry_launcher_pos() -> Position | None:
    """Position of the live sentry launcher, or None if we don't have one.

    The sentry is whichever friendly launcher stands on a `sentry_candidates()`
    tile. Every unit derives this from the same shared map state, so core,
    launcher, and defenders all agree without spending a comms slot on it.
    """
    launchers = (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    if not launchers:
        return None
    w = map_info._width
    for tile in sentry_candidates():
        if launchers & (1 << (tile.x + tile.y * w)):
            return tile
    return None


def sentry_build_tile() -> Position | None:
    """Best sentry tile that is currently free to build on, or None."""
    blocked = map_info._bm_any_building | map_info._bm_friendly_bots | map_info._bm_enemy_bots
    w = map_info._width
    for tile in sentry_candidates():
        if not (blocked & (1 << (tile.x + tile.y * w))):
            return tile
    return None


def spawn_tile_for(launcher: Position, toward: Position | None) -> Position | None:
    """Core spawn-ring tile inside the launcher's pickup radius, nearest `toward`.

    This is the tile the core drops a defender on so the sentry can throw it the
    same round. Returns None if the ring is unusable; the caller still has to
    check `can_spawn`.
    """
    best = None
    best_key = None
    for tile in spawn_ring():
        if tile.distance_squared(launcher) > PICKUP_R2:
            continue
        key = (tile.distance_squared(toward) if toward is not None else 0,
               tile.x + tile.y * map_info._width)
        if best_key is None or key < best_key:
            best_key = key
            best = tile
    return best


def can_reach_by_throw(launcher: Position, target: Position) -> bool:
    """Raw throw-range check (the engine also requires a bot-passable target)."""
    return 0 < launcher.distance_squared(target) <= THROW_R2
