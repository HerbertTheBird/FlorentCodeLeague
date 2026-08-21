from main import has_op
from fcode import Controller, Position, Team

import comms
import map_info
import relaygeom
import units.defense as defense
from log import log

rc: Controller = None
my_team: Team = None
_is_chokepoint_launcher: bool | None = None

# Builder ids seen adjacent last round, so a bot that appears out of nowhere can
# be recognised as this round's freshly spawned defender (see `_pick_defender`).
_prev_adjacent_bots: set[int] = set()

# Relay state. `_relay_role` is latched on this launcher's FIRST run and
# never revisited: the builder that built us was standing next to us on that
# turn and on no other, so that one look is the whole test.
_relay_role = None
_relay_thrown = False
_relay_waited = 0
# Turns a confirmed relay link will wait for the core's slot-0 position
# before throwing on the builder's direction hint alone.
RELAY_CORE_WAIT = 3


def init(c: Controller):
    global rc, my_team, _is_chokepoint_launcher, _prev_adjacent_bots
    global _relay_role, _relay_thrown, _relay_waited
    rc = c
    my_team = map_info._my_team
    _is_chokepoint_launcher = None
    _prev_adjacent_bots = set()
    _relay_role = None
    _relay_thrown = False
    _relay_waited = 0
    relaygeom.reset()


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



# --------------------------------------------------------------------------- #
# Relay role: throw the siege builder on to the next hop
# --------------------------------------------------------------------------- #
def _relay_builder_in_pickup():
    """Position of the relay builder standing in our pickup radius, or None.

    Identity comes from the entity id and nothing else. Every unit runs in its
    own interpreter, so the builder cannot simply tell us it is the relay -- and
    Tyr's answer (derive the whole chain from a shipped map table, so both ends
    agree without talking) is not available here, because shipping map tables is
    exactly what we are not allowed to do.
    """
    my_pos = map_info._my_pos
    for tile in rc.get_nearby_tiles(relaygeom.PICKUP_RANGE_SQ):
        if tile == my_pos:
            continue
        bot_id = rc.get_tile_builder_bot_id(tile)
        if bot_id is None or bot_id not in relaygeom.RELAY_BOT_IDS:
            continue
        if rc.get_team(bot_id) != my_team:
            continue
        return tile
    return None


def _relay_throw() -> bool:
    """If we are a link in the relay, throw the builder on. True if the turn is
    ours -- i.e. we launched, or we retired."""
    global _relay_role, _relay_thrown, _relay_waited

    if _relay_role is False:
        return False
    if _relay_thrown:
        # One throw, then gone. Tyr's reasoning, which survives the port intact:
        # a relay launcher that stays alive is 20+ titanium of dead weight on the
        # turret cost scale, holds a unit-cap slot, and stands on a tile the map
        # would rather have empty. Retiring hands all three back before the next
        # hop's launcher is bought.
        rc.self_destruct()
        return True

    bot_pos = _relay_builder_in_pickup()
    if bot_pos is None:
        if _relay_role is None:
            # First run and nobody to throw: an ordinary launcher. Latched, so
            # the relay checks cost this launcher one turn's lookup and nothing
            # for the rest of the game.
            _relay_role = False
        return False
    _relay_role = True
    _relay_waited += 1

    if rc.get_action_cooldown() != 0:
        return False
    bot = (bot_pos.x, bot_pos.y)
    me = (map_info._my_pos.x, map_info._my_pos.y)
    theirs = relaygeom.their_core()
    if theirs is not None:
        mode = "field"
        cands = relaygeom.landings(me, relaygeom.dist_at(bot))
    elif _relay_waited <= RELAY_CORE_WAIT:
        # We are a relay link and we do not know where the enemy is. The core
        # broadcasts its position every round and `comms.core_position()`
        # latches it, so this normally resolves on the next read; a turn spent
        # waiting is far cheaper than a throw in the wrong direction. Measured
        # on frostgate before this existed: the fallback fired on hop 2 and put
        # the builder six tiles SOUTH of a due-east target, costing two extra
        # launchers and four turns.
        relaygeom.trace(rc, "relay link waiting: enemy core unknown")
        return False
    else:
        mode = "hint"
        cands = relaygeom.fallback_landings(me, bot)
    for entry in cands:
        landing = entry[-1]
        tile = Position(landing[0], landing[1])
        if not rc.is_tile_passable(tile):
            continue
        if not rc.can_launch(bot_pos, tile):
            continue
        rc.launch(bot_pos, tile)
        _relay_thrown = True
        relaygeom.trace(rc, f"throw[{mode}] {bot} -> {landing}",
                        "their_core", theirs, "my_core", relaygeom.my_core())
        return True
    relaygeom.trace(rc, f"no legal landing[{mode}] for bot at", bot)
    return False


def run():
    global _prev_adjacent_bots
    map_info.update()
    _classify_launcher()

    # A launcher never called comms.read(), so it never absorbed the core's
    # broadcast symmetry bits and ran on its own untouched flags -- which diverge
    # from the builder's. Absorb first, then everything agrees.
    try:
        comms.read()
    except Exception:
        pass
    if rc.get_current_round() <= 60:
        import relaygeom as _rg
        print("DBGL r=%-3d launcher@(%d,%d) sym[h=%d v=%d r=%d] mycore=%s theircore=%s" % (
            rc.get_current_round(), map_info._my_pos.x, map_info._my_pos.y,
            map_info._hor_sym, map_info._ver_sym, map_info._rot_sym,
            _rg.my_core(), _rg.their_core()), flush=True)

    # Above everything else this launcher could do: it exists to move one
    # builder, the rest of the chain is waiting on it, and a turn lost here is a
    # turn lost by every hop after it. Placed AFTER map_info.update() on purpose
    # -- run before it, the masks it reads are last turn's.
    if _relay_role is not False:
        comms.read()          # symmetry from slot 0, and pooled terrain
    if _relay_throw():
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
