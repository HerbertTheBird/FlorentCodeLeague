from main import has_op
from fcode import Controller, Position, Team

import comms
import map_info
import units.defense as defense
from log import log

rc: Controller = None
my_team: Team = None
_is_chokepoint_launcher: bool | None = None

# Builder ids seen adjacent last round, so a bot that appears out of nowhere can
# be recognised as this round's freshly spawned defender (see `_pick_defender`).
_prev_adjacent_bots: set[int] = set()


def init(c: Controller):
    global rc, my_team, _is_chokepoint_launcher, _prev_adjacent_bots
    rc = c
    my_team = map_info._my_team
    _is_chokepoint_launcher = None
    _prev_adjacent_bots = set()


def _classify_launcher() -> None:
    global _is_chokepoint_launcher
    if _is_chokepoint_launcher is not None:
        return
    w = map_info._width
    my_pos = rc.get_position()
    my_bit = 1 << (my_pos.x + my_pos.y * w)
    enemy_conveyors = map_info._bm_conveyors & map_info._bm_team[1 - map_info._my_team_idx]
    adjacent = map_info.expand_chebyshev(my_bit) & ~my_bit
    _is_chokepoint_launcher = not bool(adjacent & enemy_conveyors)


def _adjacent_enemy_builders() -> list[tuple[int, Position]]:
    result = []
    my_pos = rc.get_position()
    for pos in rc.get_nearby_tiles(2):
        if pos == my_pos:
            continue
        if max(abs(pos.x - my_pos.x), abs(pos.y - my_pos.y)) > 1:
            continue
        bot_id = rc.get_tile_builder_bot_id(pos)
        if bot_id is None:
            continue
        if rc.get_team(bot_id) == my_team:
            continue
        result.append((bot_id, pos))
    return result


def _try_throw_enemy_away() -> bool:
    adjacent_enemies = _adjacent_enemy_builders()
    if not adjacent_enemies:
        return False

    w = map_info._width
    my_pos = rc.get_position()
    my_bit = 1 << (my_pos.x + my_pos.y * w)

    # Collect legal (bot_pos, tile) launches; keep one bot per destination tile.
    launchable_map = {}
    launchable_mask = 0
    for _enemy_id, bot_pos in adjacent_enemies:
        for tile in rc.get_attackable_tiles():
            if not rc.is_tile_passable(tile):
                continue
            if not rc.can_launch(bot_pos, tile):
                continue
            tn = tile.x + tile.y * w
            if tn in launchable_map:
                continue
            launchable_map[tn] = (bot_pos, tile)
            launchable_mask |= 1 << tn

    if not launchable_mask:
        return False

    # For each candidate destination, Chebyshev flood-fill the region the
    # launched bot could navigate using enemy-POV pathing, with unseen tiles
    # treated as impassable. Smaller region = the bot is more contained
    # post-launch, which is what we want.
    forbidden = (
        map_info.get_avoid(False, enemy_pov=True)
        | (~map_info._bm_seen & map_info._board_mask)
    )
    passable = ~forbidden & map_info._board_mask

    component_size: dict[int, int] = {}
    handled = 0
    m = launchable_mask
    while m:
        lsb = m & -m
        m ^= lsb
        n = lsb.bit_length() - 1
        if handled & lsb:
            continue
        if not (lsb & passable):
            # Destination is enemy-impassable itself (e.g. our core) — best.
            component_size[n] = 0
            handled |= lsb
            continue
        region = lsb
        while True:
            nxt = map_info.expand_chebyshev(region) & passable
            if nxt == region:
                break
            region = nxt
        size = region.bit_count()
        rm = launchable_mask & region
        while rm:
            rl = rm & -rm
            rm ^= rl
            component_size[rl.bit_length() - 1] = size
        handled |= region | lsb

    # Tiebreak with the prior heuristic: prefer destinations farthest from
    # (every conveyor) ∪ (launcher) in a wall-only Chebyshev BFS.
    seed = map_info._bm_conveyors | my_bit
    walls = map_info._bm_env[map_info._IDX_ENV_WALL]
    traversable = ~walls & map_info._board_mask
    UNREACHED = 1 << 30
    layer_of: dict[int, int] = {}
    visited = seed
    frontier = seed
    layer_idx = 0
    rm = seed & launchable_mask
    while rm:
        rl = rm & -rm
        rm ^= rl
        layer_of[rl.bit_length() - 1] = layer_idx
    while frontier:
        layer_idx += 1
        next_frontier = map_info.expand_chebyshev(frontier) & traversable & ~visited
        if not next_frontier:
            break
        rm = next_frontier & launchable_mask
        while rm:
            rl = rm & -rm
            rm ^= rl
            layer_of[rl.bit_length() - 1] = layer_idx
        visited |= next_frontier
        frontier = next_frontier

    best_n = None
    best_key = None
    rm = launchable_mask
    while rm:
        rl = rm & -rm
        rm ^= rl
        rn = rl.bit_length() - 1
        key = (component_size[rn], -layer_of.get(rn, UNREACHED))
        if best_key is None or key < best_key:
            best_key = key
            best_n = rn

    bot_pos, tile = launchable_map[best_n]
    rc.launch(bot_pos, tile)
    log(f"launcher threw enemy from {bot_pos} to {tile} (size={best_key[0]}, layer={-best_key[1]})")
    return True


def _try_throw_enemy_to_history() -> bool:
    adjacent_enemies = _adjacent_enemy_builders()
    if not adjacent_enemies:
        return False

    w = map_info._width
    histories = [
        (enemy_id, bot_pos, map_info.bot_position_history(enemy_id))
        for enemy_id, bot_pos in adjacent_enemies
    ]
    max_history_len = max((len(history) for _enemy_id, _bot_pos, history in histories), default=0)

    for depth in range(1, max_history_len):
        for enemy_id, bot_pos, history in histories:
            if depth >= len(history):
                continue
            tn = history[depth]
            tile = Position(tn % w, tn // w)
            if tile == bot_pos:
                continue
            if not rc.is_tile_passable(tile):
                continue
            if not rc.can_launch(bot_pos, tile):
                continue
            rc.launch(bot_pos, tile)
            log(f"chokepoint launcher threw enemy {enemy_id} from {bot_pos} back to {tile}")
            return True
    return False


# --------------------------------------------------------------------------- #
# Sentry role: watch the approach, raise the alarm, deliver the blocker
# --------------------------------------------------------------------------- #
def _alarm_target(sentry_pos: Position):
    """The enemy bot this sentry wants a blocker dispatched to, or None.

    Only enemies inside the sentry's own vision count — `map_info._bm_enemy_bots`
    is already scoped to what this unit has seen. An enemy whose block tile is
    held by one of our builders is, by definition, already handled.

    Among the unhandled ones, prefer any whose block tile we can actually throw
    a defender onto; a blocker that has to walk there usually arrives too late.
    """
    throwable = None
    walkable = None
    for _d2, _uid, enemy in defense.threatening_enemies():
        block = defense.block_tile(enemy)
        if block is None or defense.is_blocked(enemy, block):
            continue
        if walkable is None:
            walkable = (enemy, block)
        if throwable is None and defense.can_reach_by_throw(sentry_pos, block):
            throwable = (enemy, block)
            break
    return throwable or walkable


def _pick_defender(sentry_pos: Position, new_bot_ids: set[int]) -> Position | None:
    """Position of the builder bot in pickup range that we should throw.

    Prefers a bot that appeared this round on the core's spawn ring — the
    defender the core just spawned in answer to our alarm. Failing that, it
    borrows whichever builder is already standing next to us.

    Borrowing beats spawning by a wide margin. Builder cost scales +20% *per
    builder ever spawned*, so every purpose-built defender permanently taxes the
    whole economy, while a borrowed one costs only the trip — and it goes back to
    work the moment the block is no longer needed. The core knows this and
    declines to spawn when a body is already in reach.
    """
    ring = set(defense.spawn_ring())
    fallback = None
    for tile in rc.get_nearby_tiles(defense.PICKUP_R2):
        bot_id = rc.get_tile_builder_bot_id(tile)
        if bot_id is None or rc.get_team(bot_id) != my_team:
            continue
        if bot_id in new_bot_ids and tile in ring:
            return tile
        if fallback is None:
            fallback = tile
    return fallback


def _adjacent_friendly_bot_ids() -> set[int]:
    out = set()
    for tile in rc.get_nearby_tiles(defense.PICKUP_R2):
        bot_id = rc.get_tile_builder_bot_id(tile)
        if bot_id is not None and rc.get_team(bot_id) == my_team:
            out.add(bot_id)
    return out


def _deliver_defender(sentry_pos: Position, target, new_bot_ids: set[int]) -> bool:
    """Throw a freshly spawned defender onto its block tile."""
    if rc.get_action_cooldown() != 0:
        return False
    enemy, block = target
    bot_pos = _pick_defender(sentry_pos, new_bot_ids)
    if bot_pos is None:
        return False
    if not rc.can_launch(bot_pos, block):
        return False
    rc.launch(bot_pos, block)
    log(f"sentry threw defender {bot_pos} -> {block} to block {enemy}")
    return True


def run():
    global _prev_adjacent_bots
    map_info.update()
    _classify_launcher()

    if _try_relay_throw():
        return

    sentry_pos = defense.sentry_launcher_pos()
    am_sentry = sentry_pos is not None and sentry_pos == map_info._my_pos

    target = None
    new_bot_ids: set[int] = set()
    if am_sentry:
        # The alarm is published unconditionally, every round: readers use the
        # flipping heartbeat to tell "quiet" from "sentry is dead".
        target = _alarm_target(sentry_pos)
        comms.write_alarm(sentry_pos, target[0] if target else None)
        adjacent = _adjacent_friendly_bot_ids()
        new_bot_ids = adjacent - _prev_adjacent_bots
        _prev_adjacent_bots = adjacent

    # An enemy already standing next to us is the most urgent thing on the board;
    # deal with that before delivering a blocker further out.
    if _is_chokepoint_launcher and _try_throw_enemy_to_history():
        return
    if _try_throw_enemy_away():
        return
    if am_sentry and target is not None:
        _deliver_defender(sentry_pos, target, new_bot_ids)


# ---- #36 relay throw ----------------------------------------------------------
def _try_relay_throw() -> bool:
    """Throw the siege builder along its relay hop.

    Only the designated siege builder is thrown, and only by a launcher that is
    not the defensive sentry. The landing is recomputed with the same rule the
    builder used, so both agree without any shared state.
    """
    import units.states.relay as relay
    import units.defense as _def
    # relay.init() is only called by units/builder.py's state loop. A launcher runs
    # in its OWN interpreter, which never runs that loop, so relay.rc is None here
    # and every call raised (measured: 826 'NoneType has no get_current_round').
    if relay.rc is None:
        relay.rc = rc
    # NO SHARED STATE. Each unit runs in its OWN interpreter, so module globals are
    # per-unit -- the launcher cannot see the builder's siege_ids (measured: it read
    # them as empty while the builder was actively building launchers from that set).
    # Instead both sides derive the SAME hop from the map: if the builder standing
    # next to us would have chosen OUR tile as its launcher site, then we are its
    # relay and we throw it to the landing it already picked.
    me = map_info._my_pos
    # Stand down for the SENTRY only when it actually has defensive work. The old
    # test was positional -- "am I standing on the sentry tile" -- and the relay's
    # first launcher is built right beside our core, which IS that tile. So our own
    # relay launcher was classified as the sentry and refused to throw, every game:
    # the builder waited, gave up, walked off and built another. Measured: launcher
    # at t1, first throw not until t12.
    try:
        s = _def.sentry_launcher_pos()
        if s is not None and s.x == me.x and s.y == me.y:
            if comms.read_alarm() is not None:
                return False            # a defender is needed right now
    except Exception:
        pass
    for uid, bot_pos in _adjacent_friendly_builders_with_ids():
        if not relay.is_siege_id(uid):
            continue        # only a siege builder rides the relay
        cur = relay.dist_at(bot_pos.x, bot_pos.y)
        if cur >= 999:
            continue
        # Compute the landing from OUR OWN tile. Re-deriving the builder's site is
        # wrong once we exist: _open() excludes buildings, so our own square is no
        # longer a candidate site and the match would never succeed.
        landing = relay.best_landing(me.x, me.y, cur)
        if landing is None:
            continue
        if rc.can_launch(bot_pos, landing):
            rc.launch(bot_pos, landing)
            log(f"relay threw siege builder {bot_pos} -> {landing}")
            return True
    return False


def _adjacent_friendly_builders_with_ids():
    """(id, position) for every friendly builder in pickup range (r2 <= 2)."""
    out = []
    for tile in rc.get_nearby_tiles(defense.PICKUP_R2):
        bot_id = rc.get_tile_builder_bot_id(tile)
        if bot_id is not None and rc.get_team(bot_id) == my_team:
            out.append((bot_id, tile))
    return out
