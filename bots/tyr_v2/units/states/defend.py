"""Builder state: break sieges, block enemy builders, and wall them in.

Three jobs, in priority order:

  1. **Break a siege.** An enemy turret with line of sight on our core is the
     thing that actually ends games, so this outranks everything. Screen a
     gunner's ray with a 3 Ti barrier where possible, otherwise go and destroy
     the turret.

  2. **Block, then trap.** Claim the tile directly in front of a threatening
     enemy bot along the axis it has furthest to travel toward our core, stand on
     it, and restore it every round. Because movement is cardinal-only the enemy
     can never close that axis while we hold the tile, and because we act after
     it in entity-id order we always see where it went before choosing our step.
     Every turn we do *not* owe it a mirror step, we spend walling its exits —
     see `_try_seal`. A fully walled builder is out of the game permanently and
     frees our blocker.

  3. **Build the sentry.** If we have no sentry launcher yet, walk to its tile
     and put one up: the sentry spots threats earlier than the core and throws a
     defender straight onto its block tile instead of walking it there.

     Worth keeping even though the top-rated bot builds zero launchers in ten
     observed games while we build 11-57. Removing ours was measured at 59.5%
     against 64.4% (Heimdall v3 54.5% -> 45.5%). Their bot does not need a
     launcher because it does not block; ours is built around blocking, and the
     throw is what makes a block land in time. Copying a stronger bot's build
     mix piecemeal does not transfer across architectures.

Note builders cannot damage enemy builders in Florent — `fire()` only hits the
building on the target tile. Containment is the only lever against a bot, which
is why trapping matters so much more here than chip damage would.

Claims go through `pathing.claim_subset`, the same Voronoi partition the other
states use, so exactly one builder takes each block tile without any comms.
"""

import map_info
import pathing
import units.builder
import units.defense as defense
from fcode import Controller, Direction, Position
from log import log
from pathing import Pathing

rc: Controller = None
nav: Pathing = None

# Breaking a siege outranks everything else in the bot, including healing. A
# gunner shooting the core does 10 damage a round and a heal restores 4 for a
# whole builder turn, so healing through it is a race we lose by six a round
# while spending titanium to do it — which is exactly how these games were being
# lost: median 99 rounds against loki, core destroyed, us at 500 Ti collected.
SIEGE_SCORE = 20
# Holding a block tile outranks the economy: a bot that gives up its tile for one
# turn hands the enemy the step it has been denied all game. Raising the sentry
# sits just below, a one-off 20 Ti build that pays for every block afterwards.
# Closing in on a block tile is worth less than attacking — an interceptor is not
# blocking anything yet.
BLOCK_SCORE = 7
# Walling a pinned enemy in sits just under blocking: it is the job that finally
# ends the standoff, converting a blocker that must shadow forever into two free
# builders and one enemy permanently out of the game.
TRAP_SCORE = 6.5
SENTRY_SCORE = 5
INTERCEPT_SCORE = 3
MAX_SCORE = SIEGE_SCORE

# How far a builder will travel to answer a siege. The turret has to be within
# ~3.6 tiles of the core to shoot it, so anyone further out than this is not
# arriving in time to matter and should keep working.
SIEGE_RANGE = 8

# How far a builder will walk to help wall in a pinned enemy.
TRAP_RANGE = 5

# The block tile moves with the enemy, one step per round — the same speed we
# move. Chasing one from further out than this never converges: the tile stays
# ahead of us while we abandon whatever we were doing. Beyond it, an enemy is
# the attack state's business.
INTERCEPT_RANGE = 4

# Rounds of "next to the tile, never on it, and nothing is moving" before the
# target is dropped. Blocking runs at the highest priority in the state machine,
# so a claim that can never be satisfied would otherwise pin a builder off the
# economy for the rest of the game.
STALL_LIMIT = 6

# Blocking is a job for bots spawned to do it, not for whoever happens to be
# nearby. Letting any builder take a block claim at top priority strips the
# economy of workers every time an enemy wanders past — measurably worse than not
# defending at all. A bot earns the role only in its first rounds of life, which
# is exactly when a core-dispatched defender is landing on (or beside) its tile;
# everyone else stays on the economy.
DEFENDER_LATCH_ROUNDS = 1

# Rounds without a block claim after which a defender goes back to being an
# ordinary builder. Without this the role is permanent: every bot that ever
# blocked keeps outranking the economy the moment any enemy wanders back into
# range, so against an opponent that keeps feeding builders at us our entire
# workforce ratchets into defenders and never returns.
DEFENDER_EXPIRY_ROUNDS = 15

_cached_block: Position | None = None      # block tile I claimed this round
_cached_enemy: Position | None = None
_cached_sentry: Position | None = None     # sentry tile I claimed this round
_cached_siege = None                       # (kind, target, turret) this round
_cached_trap = None                        # (enemy, barrier tile) this round
_cached_holding = False                    # am I within one step of the block tile?

# Latched target, so a blocker keeps working the same enemy on the same axis
# instead of re-deciding from scratch every round.
_target_id: int | None = None
_target_axis: str | None = None
_stall_rounds = 0
_last_enemy_pos: Position | None = None

_birth_round: int | None = None
_am_defender = False
_seen_threat = False       # has any enemy ever come inside the threat radius?
_last_claim_round = -10**9    # last round I held a block claim
_enemy_moved = True           # did my target move since my last turn?


def init(c: Controller):
    global rc, nav, _target_id, _target_axis, _stall_rounds, _last_enemy_pos
    global _birth_round, _am_defender, _seen_threat, _last_claim_round
    global _enemy_moved
    rc = c
    nav = units.builder.nav
    _target_id = None
    _target_axis = None
    _stall_rounds = 0
    _last_enemy_pos = None
    _birth_round = None
    _am_defender = False
    _seen_threat = False
    _last_claim_round = -10**9
    _enemy_moved = True


def _manhattan(a: Position, b: Position) -> int:
    return abs(a.x - b.x) + abs(a.y - b.y)


def _release_target() -> None:
    """Drop the latch `_claim_block` set, for a claim we turned out not to want."""
    global _target_id, _target_axis, _last_enemy_pos, _stall_rounds
    _target_id = _target_axis = _last_enemy_pos = None
    _stall_rounds = 0


def _tile_taken(tile: Position) -> bool:
    """True if a builder bot other than me already occupies `tile`.

    `map_info.is_passable` only looks at terrain and buildings, so without this
    a defender will happily claim a tile an ally is already holding and then
    stand next to it forever, doing nothing at top priority.
    """
    bit = 1 << (tile.x + tile.y * map_info._width)
    return bool((map_info._bm_friendly_bots | map_info._bm_enemy_bots) & bit)


def _claim_block():
    """(enemy, block tile, holding) for the enemy I should block, or None.

    `holding` is True when the block tile is already mine to keep — I am on it,
    or one cardinal step away, so I can restore it this turn. Otherwise I am
    merely closing in and the caller scores it far lower.
    """
    global _target_id, _target_axis, _stall_rounds, _last_enemy_pos, _enemy_moved
    threats = defense.threatening_enemies()
    if not threats:
        _target_id = _target_axis = _last_enemy_pos = None
        _stall_rounds = 0
        return None

    my_pos = map_info._my_pos
    w = map_info._width

    # Stick with the latched target while it is still a threat: re-picking every
    # round makes a blocker oscillate between two enemies and hold neither.
    if _target_id is not None:
        for _d2, uid, enemy in threats:
            if uid != _target_id:
                continue
            # Stay on the pinned axis. If its forward tile is already denied —
            # by terrain, a barrier we laid, or us standing on it — the enemy
            # cannot advance and we owe it nothing this turn.
            block = defense.forward_tile(enemy, _target_axis)
            if block is None:
                break
            if defense.denies_advance(block, my_pos):
                _enemy_moved = enemy != _last_enemy_pos
                _last_enemy_pos = enemy
                _stall_rounds = 0
                return enemy, block, True
            if not _tile_taken(block):
                dist = _manhattan(my_pos, block)
                # Neither of us has moved and I still am not on the tile: the
                # claim is going nowhere, so stop paying top priority for it.
                _enemy_moved = enemy != _last_enemy_pos
                if dist > 0 and enemy == _last_enemy_pos:
                    _stall_rounds += 1
                else:
                    _stall_rounds = 0
                _last_enemy_pos = enemy
                if dist <= INTERCEPT_RANGE and _stall_rounds < STALL_LIMIT:
                    return enemy, block, dist <= 1
            break
        _target_id = _target_axis = _last_enemy_pos = None
        _stall_rounds = 0

    # Otherwise take the nearest reachable block tile no other builder has a
    # better claim on. claim_subset is the same Voronoi partition the other
    # states use, so two builders never converge on one tile.
    by_tile: dict[int, tuple[int, Position, str]] = {}
    claims = 0
    for _d2, uid, enemy in threats:
        block, axis = defense.block_tile_axis(enemy)
        if block is None or _manhattan(my_pos, block) > INTERCEPT_RANGE:
            continue
        if block != my_pos and _tile_taken(block):
            continue
        n = block.x + block.y * w
        if n in by_tile:
            continue
        by_tile[n] = (uid, enemy, axis)
        claims |= 1 << n
    if not claims:
        return None

    my_bit = 1 << (my_pos.x + my_pos.y * w)
    if my_bit & claims:
        # A block tile I am already standing on is mine by definition;
        # claim_subset would hand it to a bot that merely paths there faster.
        best = my_pos
    else:
        mine = pathing.claim_subset(my_bit, map_info._bm_friendly_bots, claims, tie_self=True)
        if not mine:
            return None
        best = min(map_info.iter_mask(mine),
                   key=lambda p: (_manhattan(my_pos, p), p.x + p.y * w))

    uid, enemy, axis = by_tile[best.x + best.y * w]
    _target_id = uid
    _target_axis = axis
    _enemy_moved = enemy != _last_enemy_pos
    _last_enemy_pos = enemy
    _stall_rounds = 0
    return enemy, best, _manhattan(my_pos, best) <= 1


def _claim_siege():
    """('screen', tile, turret) for a screenable besieging gunner.

    Screening comes first because it is far cheaper: a barrier on the gunner's
    ray costs 3 Ti and stops the damage this turn, whereas chewing a 40 HP turret
    down at 2 damage a hit takes twenty builder-turns under fire. We only go for
    Sentinels ignore screens, so Tyr answers them with repair throughput instead
    of sending builders across the map to spend twenty attack turns on a turret.
    """
    besiegers = defense.core_besiegers(rc)
    if not besiegers:
        return None
    my_pos = map_info._my_pos
    for turret, etype, facing, hit in besiegers:
        if my_pos.distance_squared(turret) > SIEGE_RANGE * SIEGE_RANGE:
            continue
        for tile in defense.screen_tiles(rc, turret, facing, etype, hit):
            # One screen per ray is enough; let the nearest builder take it and
            # leave everyone else free to attack the turret itself.
            w = map_info._width
            bit = 1 << (tile.x + tile.y * w)
            my_bit = 1 << (my_pos.x + my_pos.y * w)
            if pathing.claim_subset(my_bit, map_info._bm_friendly_bots, bit, tie_self=True):
                return "screen", tile, turret
        return None
    return None


def _run_siege() -> None:
    kind, target, turret = _cached_siege
    my_pos = map_info._my_pos
    adjacent = abs(target.x - my_pos.x) + abs(target.y - my_pos.y) == 1

    if kind == "screen":
        # Deliberately ignores map_info.ti_reserve(): the reserve exists to keep a
        # defender spawnable, and there is no point holding titanium for that
        # while the core is being shot to pieces.
        if adjacent and rc.can_build_barrier(target) \
                and rc.get_global_resources() >= rc.get_barrier_cost():
            log(f"siege screen: barrier at {target} vs turret {turret}")
            rc.build_barrier(target)
            map_info.update_at(target)
            return
        nav.move_adjacent(target)
        return

    return


def _claim_trap():
    """(enemy, barrier tile) that helps wall in an enemy an ally is already blocking.

    A blocker can never do this itself. It acts after the enemy, so on any turn
    the enemy moved it owes a mirror step, and building costs the turn's move —
    which means a mobile enemy leaves its blocker with no free actions at all.
    Instrumented games bear that out: 175 rounds of blocking produced 9 rounds
    where the blocker was even standing on its tile at decision time.

    So the walling is a second builder's job. It has no mirror obligation and can
    lay a barrier every turn. Perpendicular exits come first, because those are
    the ones that force the blocker to keep shadowing; once both are gone the
    enemy can only retreat, the blocker stops needing to mirror, and the pair can
    finish the box together.
    """
    if not defense.may_wall():
        return None
    my_pos = map_info._my_pos
    w = map_info._width
    my_bit = 1 << (my_pos.x + my_pos.y * w)
    for _d2, _uid, enemy in defense.threatening_enemies():
        if my_pos.distance_squared(enemy) > TRAP_RANGE * TRAP_RANGE:
            continue
        block = defense.block_tile(enemy)
        if block is None or not defense.is_blocked(enemy, block):
            continue          # nobody is pinning it; walling alone achieves little
        exits = defense.free_exits(enemy, ignore=my_pos)
        if not exits:
            continue
        perp = set(_perp_exits(enemy, block))
        ordered = [t for t in exits if t in perp] + [t for t in exits if t not in perp]
        for tile in ordered:
            bit = 1 << (tile.x + tile.y * w)
            if pathing.claim_subset(my_bit, map_info._bm_friendly_bots, bit, tie_self=True):
                return enemy, tile
    return None


def _run_trap() -> None:
    enemy, tile = _cached_trap
    my_pos = map_info._my_pos
    if abs(tile.x - my_pos.x) + abs(tile.y - my_pos.y) == 1:
        if rc.can_build_barrier(tile) and rc.get_global_resources() >= rc.get_barrier_cost():
            log(f"trapping {enemy}: barrier at {tile}")
            rc.build_barrier(tile)
            map_info.update_at(tile)
            return
    nav.move_adjacent(tile)


def _claim_sentry() -> Position | None:
    """The sentry launcher's tile, if it needs building and it should be me."""
    global _seen_threat
    if defense.threatening_enemies():
        _seen_threat = True
    if defense.sentry_launcher_pos() is not None:
        return None
    # Build it only once something has actually come for us. On a map where the
    # enemy never approaches, the sentry is 20 Ti, a builder's detour, and a tile
    # of core-adjacent space — right where our conveyors want to run — spent for
    # nothing.
    if not _seen_threat:
        return None
    if rc.get_global_resources() < rc.get_launcher_cost() + map_info.ti_reserve():
        return None
    tile = defense.sentry_build_tile()
    if tile is None:
        return None
    w = map_info._width
    bit = 1 << (tile.x + tile.y * w)
    my_bit = 1 << (map_info._my_pos.x + map_info._my_pos.y * w)
    if not pathing.claim_subset(my_bit, map_info._bm_friendly_bots, bit, tie_self=True):
        return None
    return tile


def score(can_move=True):
    if not can_move:
        return 0
    global _cached_block, _cached_enemy, _cached_sentry, _cached_holding, _cached_siege
    global _cached_trap, _birth_round, _am_defender, _last_claim_round
    _cached_block = _cached_enemy = _cached_sentry = _cached_siege = _cached_trap = None
    _cached_holding = False

    round_num = rc.get_current_round()
    if _birth_round is None:
        _birth_round = round_num

    # A turret shooting the core outranks every other job, defence included.
    _cached_siege = _claim_siege()
    if _cached_siege is not None:
        return SIEGE_SCORE

    claimed = _claim_block()
    if claimed is not None:
        enemy, block, holding = claimed
        # Three ways to be the one who blocks: I already am; I am newly spawned
        # (so the core dispatched me); or I am standing exactly on the block tile,
        # which is what being thrown there by the sentry looks like from inside.
        if (_am_defender
                or round_num - _birth_round <= DEFENDER_LATCH_ROUNDS
                or block == map_info._my_pos):
            _am_defender = True
            _last_claim_round = round_num
            _cached_enemy, _cached_block, _cached_holding = enemy, block, holding
            return BLOCK_SCORE if holding else INTERCEPT_SCORE
        _release_target()

    _cached_trap = _claim_trap()
    if _cached_trap is not None:
        return TRAP_SCORE

    if _am_defender and round_num - _last_claim_round > DEFENDER_EXPIRY_ROUNDS:
        _am_defender = False

    _cached_sentry = _claim_sentry()
    if _cached_sentry is not None:
        return SENTRY_SCORE
    return 0





def _hold_block() -> None:
    """Restore the block tile, or spend a safe turn walling the enemy in.

    Builder `fire()` only damages the *building* on the target tile, so a builder
    cannot hurt an enemy builder at all (verified: `can_fire` on a bot-only tile
    is always False). Containment is the entire win condition here — a sealed
    enemy cannot approach, build a turret, or harvest.
    """
    block = _cached_block
    enemy = _cached_enemy
    my_pos = map_info._my_pos

    # Do we actually owe a mirror step? Only if the enemy's forward tile on the
    # blocked axis is somewhere it could still step. If it is denied — because we
    # are standing on it, or because terrain or one of our barriers already seals
    # it — the enemy cannot advance and the turn is ours to spend.
    if enemy is not None and defense.denies_advance(block, my_pos):
        # Spend it widening the seal. Flanks first: sealing the two tiles beside
        # us kills the payoff of a sideways step, which is what generates every
        # future free turn. Then the enemy's own remaining exits, which finish
        # the box and take it out of the game for good.
        if _try_seal_flanks(my_pos):
            return
        if _try_seal(enemy, block, my_pos):
            return
        # Nothing to build and nowhere to move: we are a parked body. This is a
        # known cost, not an oversight, and two ways out have been measured.
        #
        # Releasing the claim after 25 idle turns: 61.7% against 62.5%.
        # Dropping to a score the economy beats whenever the enemy is already
        # denied by terrain or our own barriers (i.e. the block holds without
        # us): 62.5% against 64.4%. Both look obviously right and both lose --
        # the body standing here is load-bearing even when the tile in front of
        # the enemy appears sealed, because a barrier can be broken and terrain
        # can be walked around, and neither is true of a builder. `tools/replay.py stuck` on a 0-5 ladder
        # loss shows blockers frozen for up to 936 of 1000 turns, and us
        # carrying ~1.5-2x the winner's idle unit-turns across the match.
        #
        # Releasing the claim after 25 idle turns (with a 60-turn cooldown so the
        # bot does not immediately re-take the tile it is standing on) was
        # implemented and measured at 61.7% against 62.5% — not proven worse, but
        # not better either, because letting our blocker go also lets theirs go.
        # The idle turns are real; converting them into an advantage needs
        # something better than simply walking away.
        return

    if my_pos != block:
        # The enemy moved at most one cardinal step before us, so the block tile
        # is at most one step away; take it directly when that step is legal and
        # only fall back to BFS when something is in the way.
        d = map_info.direction_to(my_pos, block)
        if d is not Direction.CENTRE and d.is_cardinal() and rc.can_move(d):
            rc.move(d)
            map_info.update_move()
        else:
            nav.move_to(block)
        return


def _try_seal_flanks(my_pos: Position) -> bool:
    """Barrier a tile beside me, perpendicular to the axis I am blocking.

    This is the move that ends the treadmill. A blocker with both flanks sealed
    never has to mirror again: the enemy can step sideways all it likes and its
    forward tile stays a barrier.
    """
    if _target_axis is None or rc.get_action_cooldown() != 0:
        return False
    if not defense.may_wall():
        return False
    if rc.get_global_resources() < rc.get_barrier_cost():
        return False
    for tile in defense.flank_tiles(my_pos, _target_axis):
        if not map_info.in_bounds(tile):
            continue
        if not rc.can_build_barrier(tile):
            continue
        log(f"flank seal: barrier at {tile} (axis {_target_axis})")
        rc.build_barrier(tile)
        map_info.update_at(tile)
        return True
    return False



def _perp_exits(enemy: Position, block: Position) -> list[Position]:
    """The enemy's free exits perpendicular to the axis I am blocking.

    These are the only moves that force me to mirror. Sealing them is what turns
    a blocker that must shadow the enemy forever into one that can stand still.
    """
    dirs = ((Direction.NORTH, Direction.SOUTH) if block.y == enemy.y
            else (Direction.EAST, Direction.WEST))
    free = []
    for d in dirs:
        tile = map_info.pos_add(enemy, d)
        if not map_info.in_bounds(tile) or not map_info.is_passable(tile):
            continue
        bit = 1 << (tile.x + tile.y * map_info._width)
        if (map_info._bm_friendly_bots | map_info._bm_enemy_bots) & bit:
            continue
        free.append(tile)
    return free


def _try_seal(enemy: Position, block: Position, my_pos: Position) -> bool:
    """Barrier one of the enemy's exits. True if we spent the turn on it.

    Building costs the turn's move, so a blocker that seals at the wrong moment
    lets the enemy slip a step along the very axis it is meant to be denying. It
    is only safe in two situations, both checked here:

      * the enemy did not move last turn, so the block tile is still under me and
        I owed it no mirror step anyway; or
      * this barrier removes its *last* perpendicular exit — after which it can
        only step backwards, and a blocker never has to mirror a retreat.

    Sealing deliberately ignores `ti_reserve()`. A barrier is 3 Ti and a trapped
    enemy builder is worth vastly more than holding 40 Ti back for a spawn.
    """
    if rc.get_action_cooldown() != 0 or not defense.may_wall():
        return False
    if rc.get_global_resources() < rc.get_barrier_cost():
        return False

    perp = _perp_exits(enemy, block)
    # Perpendicular exits first: they are the ones that cost us mirror steps.
    # The tile behind the enemy is only worth sealing once it is pinned.
    if len(perp) == 1 or (not _enemy_moved and perp):
        candidates = perp
    elif not _enemy_moved:
        candidates = defense.free_exits(enemy, ignore=my_pos)
    else:
        return False

    for tile in candidates:
        if abs(tile.x - my_pos.x) + abs(tile.y - my_pos.y) != 1:
            continue
        if not rc.can_build_barrier(tile):
            continue
        log(f"trapping {enemy}: barrier at {tile} (perp left {len(perp)})")
        rc.build_barrier(tile)
        map_info.update_at(tile)
        return True
    return False




def _build_sentry() -> None:
    tile = _cached_sentry
    if rc.can_build_launcher(tile) and (
        rc.get_global_resources() >= rc.get_launcher_cost() + map_info.ti_reserve()
    ):
        log(f"building sentry launcher at {tile}")
        rc.build_launcher(tile)
        map_info.update_at(tile)
        return
    nav.move_adjacent(tile)


def run(can_move=True):
    if not can_move:
        return
    if _cached_siege is not None:
        _run_siege()
    elif _cached_block is not None:
        log(f"DEFEND blocking {_cached_enemy} at {_cached_block}")
        _hold_block()
    elif _cached_trap is not None:
        _run_trap()
    elif _cached_sentry is not None:
        log(f"DEFEND sentry -> {_cached_sentry}")
        _build_sentry()
