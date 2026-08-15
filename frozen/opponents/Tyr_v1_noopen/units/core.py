from main import has_op
from fcode import Controller, Direction, Position, EntityType, GameConstants
import map_info
from log import log
from units.spawn_plan import choose_spawn_plan, draw_spawn_plan, INITIAL_SPAWN_COUNT, INITIAL_EXPLORE_MAX_STEPS
import units.defense as defense
import units.opener as opener
import comms
rc: Controller

# --- Configurable ---
SCALE_MULT = 1
DEFENSE_FRIENDLY_RADIUS_SQ = 36
# Rounds to wait after dispatching a blocker before dispatching another. A
# defender that had to walk to its block tile is in flight for several rounds
# and the alarm stays up the whole time; without this the core would spawn one
# bot per round at the same threat.
DISPATCH_COOLDOWN = 5
# Manhattan reach, measured from the core footprint, within which a defender that
# has to walk to its block tile can still get there before the tile has drifted
# out of range. Beyond it we only dispatch when the sentry can throw.
WALK_DISPATCH_RANGE = 5
# Titanium kept back when emergency-spawning under siege, so the very last of the
# bank is still available for the barriers and attacks the responders need.
SIEGE_SPAWN_FLOOR = 60
# Unit-count ceiling for emergency spawns, well under the 50-unit cap.
SIEGE_MAX_UNITS = 20
# Repairers are cheaper than the ammunition they erase, but healing throughput
# is discrete: one builder restores 4 HP per turn. Keep measured repair pressure
# alive across turret reload gaps so a sentinel's quiet rounds do not make the
# core dismiss the workforce just before the next 18-damage shot.
REPAIR_PRESSURE_TTL = 8
REPAIR_SPAWN_FLOOR = 60
REPAIR_MAX_NEAR_CORE = 8
REPAIR_MAX_UNITS = 30
STRUCTURE_REPAIR_TRIGGER = 12
# Titanium kept free beyond a builder's own cost before spawning another one.
# Builders are the engine of the whole economy — in won games we finish with
# ~16 units and 39 buildings, in lost ones with ~9 and 23 — so this buffer wants
# to be small enough to keep hiring and large enough that hiring never starves
# conveyor and harvester construction.
ECON_BUFFER = 150
ECON_BUFFER_MANY = 350
# On 500 starting titanium this buffer keeps clearing, so the opening runs to
# about 7 builders costing ~336 Ti to builder-cost scaling before the first
# harvester. That looks obviously wasteful and is not: gating extra spawns on
# having an economy was measured at two strengths and both lost, monotonically
# in the direction of *fewer* builders being worse.
#
#     bootstrap buffer   none(=150, ~7 bots)   220 (~6 bots)   320 (~4 bots)
#     full suite               62.5%               59.8%           57.6%
#
# loki alone prefers the lean opening (71.2% at 320 against 62.1% now), so it is
# not that early builders are useless — it is that against most opponents the
# extra bodies out-earn the scaling they cost. Leave it alone without new
# evidence.

_spawn_plan: list[Direction] | None = None
_num_spawned = 0
_spawn_tiles: tuple[Position, ...] = ()   # tiles immediately surrounding the 2x2 core
_last_dispatch_round = -DISPATCH_COOLDOWN
_last_core_hp: int | None = None   # core HP last round, to detect real damage
_repair_pressure_until = -1
_repair_target_count = 1


def _compute_spawn_tiles() -> tuple[Position, ...]:
    """Tiles immediately surrounding the core's 2x2 footprint (never on the core
    itself). These are the only legal builder-spawn positions."""
    core = map_info._my_core
    if core is None:
        return ()
    w = map_info._width
    h = map_info._height
    core_tiles = {(core.x + dx, core.y + dy) for dx in (0, 1) for dy in (0, 1)}
    ring: set[tuple[int, int]] = set()
    for tx, ty in core_tiles:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nx, ny = tx + dx, ty + dy
                if (nx, ny) in core_tiles:
                    continue
                if 0 <= nx < w and 0 <= ny < h:
                    ring.add((nx, ny))
    return tuple(Position(x, y) for x, y in ring)


def init(c: Controller):
    global rc
    rc = c
    opener.init(c)


def _spawn_best_toward(target: Position) -> bool:
    """Spawn a builder on the surrounding tile closest to `target`. Returns True
    if a builder was spawned.

    Every non-opener spawn comes through here -- economic, repairer, defender --
    which is why the opener's barred-tile list is applied at this one point. A
    hardcoded opener can seal a gap next to our own core, and a ring tile on the
    wrong side of that seal is a tile a builder never comes back from.
    """
    blocked = opener.no_spawn_tiles()
    best = None
    best_d = None
    for p in _spawn_tiles:
        if p in blocked:
            continue
        if rc.can_spawn(p):
            d = p.distance_squared(target)
            if best_d is None or d < best_d:
                best_d = d
                best = p
    if best is None:
        return False
    rc.spawn_builder(best)
    return True


def _spawn_toward_plan(core_pos: Position) -> bool:
    global _num_spawned
    if _spawn_plan is None or _num_spawned >= len(_spawn_plan):
        return False

    planned_dir = _spawn_plan[_num_spawned]
    dx, dy = map_info._DIRECTION_DELTAS[planned_dir]
    # Aim well past the footprint so the closest surrounding tile is the one best
    # aligned with the planned direction.
    target = Position(core_pos.x + dx * 8, core_pos.y + dy * 8)
    if _spawn_best_toward(target):
        _num_spawned += 1
        return True
    return False


def _spawn_toward_center() -> bool:
    """Spawn on the surrounding tile closest to map center."""
    center = Position(map_info._width // 2, map_info._height // 2)
    return _spawn_best_toward(center)


def _threat_to_answer(alarm) -> tuple[Position, Position] | None:
    """(enemy, block tile) the core should dispatch a blocker to, or None.

    Two independent sources feed this. The sentry launcher's alarm reaches
    further up the approach lane than the core can see, because the sentry sits
    two tiles out toward the enemy. The core's own vision (r² 36) covers threats
    the sentry has no line on — and works before the sentry has even been built.
    Whichever fires, we only answer threats nothing of ours is already blocking.
    """
    if alarm is not None and alarm[1] is not None:
        enemy = alarm[1]
        block = defense.block_tile(enemy)
        if block is not None and not defense.is_blocked(enemy, block):
            return enemy, block
    for _d2, _uid, enemy in defense.threatening_enemies():
        block = defense.block_tile(enemy)
        if block is not None and not defense.is_blocked(enemy, block):
            return enemy, block
    return None


def _builder_in_pickup_range(sentry: Position) -> bool:
    """Is one of my builders already standing where the sentry could pick it up?"""
    bit_mask = map_info._bm_friendly_bots
    w = map_info._width
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            x, y = sentry.x + dx, sentry.y + dy
            if 0 <= x < w and 0 <= y < map_info._height and bit_mask & (1 << (x + y * w)):
                return True
    return False


def _blocker_count() -> int:
    return sum(1 for _d2, _uid, enemy in defense.threatening_enemies()
               if defense.is_blocked(enemy))


def _spawn_defender(alarm, threat: tuple[Position, Position]) -> bool:
    """Spawn the blocker for `threat`, preferring a tile the sentry can throw from.

    The core has the lowest entity id on the team, so it always acts before the
    sentry launcher in the same round: a defender dropped inside the sentry's
    pickup radius now gets thrown onto its block tile a few microseconds later,
    same round. Only if we have no sentry does the defender have to walk, and
    then we at least start it on the side facing the threat.
    """
    global _last_dispatch_round
    enemy, block = threat
    round_num = rc.get_current_round()
    if round_num - _last_dispatch_round < DISPATCH_COOLDOWN:
        return False
    if _blocker_count() >= defense.MAX_BLOCKERS:
        return False
    if rc.get_global_resources() < rc.get_builder_bot_cost():
        return False

    sentry = alarm[0] if alarm is not None else defense.sentry_launcher_pos()
    if sentry is not None and defense.can_reach_by_throw(sentry, block):
        # If a builder is already inside the sentry's pickup radius the sentry
        # will borrow it this same round. Spawning a second body would cost 30 Ti
        # and a permanent +20% on every future builder for nothing.
        if _builder_in_pickup_range(sentry):
            return False
        tile = defense.spawn_tile_for(sentry, block)
        if tile is not None and rc.can_spawn(tile):
            rc.spawn_builder(tile)
            _last_dispatch_round = round_num
            log(f"core spawned defender at {tile} for enemy {enemy} (sentry throw to {block})")
            return True

    # No throw available, so the defender must walk. The block tile moves one
    # step per round — the same speed the defender walks — so a distant one is
    # never caught and the spawn is simply wasted titanium.
    box = defense.core_footprint()
    if box is not None:
        x0, x1, y0, y1 = box
        reach = (abs(block.x - min(max(block.x, x0), x1))
                 + abs(block.y - min(max(block.y, y0), y1)))
        if reach > WALK_DISPATCH_RANGE:
            return False
    if _spawn_best_toward(block):
        _last_dispatch_round = round_num
        log(f"core spawned walking defender toward {block} for enemy {enemy}")
        return True
    return False


def _visible_structure_damage() -> tuple[int, Position | None]:
    """Missing HP on visible friendly non-core structures, plus worst target."""
    mask = (map_info._bm_damaged
            & map_info._bm_visible
            & map_info._bm_team[map_info._my_team_idx]
            & ~map_info._bm_my_core_area)
    total = 0
    worst = None
    worst_missing = 0
    w = map_info._width
    while mask:
        bit = mask & -mask
        mask ^= bit
        n = bit.bit_length() - 1
        et_idx = map_info._building_et_idx[n]
        if et_idx < 0:
            continue
        missing = map_info._MAX_HP_BY_IDX[et_idx] - map_info._building_hp[n]
        if missing <= 0:
            continue
        total += missing
        if missing > worst_missing:
            worst_missing = missing
            worst = Position(n % w, n // w)
    return total, worst


def _measure_repair_demand() -> tuple[int, int, Position | None]:
    """Update the standing repair demand from this round's damage.

    Split out of `_spawn_repairer` and called unconditionally, because the
    measurement is differential: `core_loss` is HP lost since the LAST call, so a
    round where the caller skips it (a hardcoded opener owning the spawn, say)
    would otherwise leave `_last_core_hp` stale and make the next call read a
    loss of zero no matter what happened in between.
    """
    global _last_core_hp, _repair_pressure_until, _repair_target_count
    hp = rc.get_hp()
    core_loss = max(0, (_last_core_hp - hp) if _last_core_hp is not None else 0)
    _last_core_hp = hp

    structure_missing, repair_target = _visible_structure_damage()
    demanded = 1
    if core_loss:
        demanded = min(REPAIR_MAX_NEAR_CORE, 1 + (core_loss + 3) // 4)
    if structure_missing >= STRUCTURE_REPAIR_TRIGGER:
        demanded = max(demanded, min(5, 1 + structure_missing // 20))

    now = rc.get_current_round()
    if demanded > 1:
        _repair_target_count = max(_repair_target_count, demanded)
        _repair_pressure_until = now + REPAIR_PRESSURE_TTL
    elif now > _repair_pressure_until:
        _repair_target_count = 1
    return core_loss, structure_missing, repair_target


def _spawn_repairer(titanium: int, nearby_builders: int, measured) -> bool:
    """Spawn enough local builders to outheal measured incoming damage.

    The target is based on *actual* net core HP loss, not merely a turret having
    geometric line of fire. One extra builder is included so the bank catches
    up instead of only holding steady. Damaged nearby structures create a
    smaller standing repair demand even when the core itself was not hit.
    """
    core_loss, structure_missing, repair_target = measured
    if nearby_builders >= _repair_target_count:
        return False
    cost = rc.get_builder_bot_cost()
    if titanium < cost + REPAIR_SPAWN_FLOOR:
        return False
    if rc.get_unit_count() >= REPAIR_MAX_UNITS:
        return False
    target = repair_target if repair_target is not None else map_info._my_pos
    if _spawn_best_toward(target):
        log(f"core spawned repairer (target={_repair_target_count}, nearby={nearby_builders}, "
            f"core_loss={core_loss}, structure_missing={structure_missing})")
        return True
    return False


def _scan_nearby_builders(core_pos: Position, my_team):
    ally_builder_count = 0
    has_close_ally = False
    closest_enemy = None
    closest_enemy_d = None

    for uid in rc.get_nearby_units():
        if rc.get_entity_type(uid) != EntityType.BUILDER_BOT:
            continue
        p = rc.get_position(uid)
        if rc.get_team(uid) == my_team:
            if p.distance_squared(core_pos) <= DEFENSE_FRIENDLY_RADIUS_SQ:
                ally_builder_count += 1
                has_close_ally = True
        else:
            d = p.distance_squared(core_pos)
            if (closest_enemy_d is None or d < closest_enemy_d) and d <= 20:
                closest_enemy_d = d
                closest_enemy = p

    return ally_builder_count, has_close_ally, closest_enemy


def run():
    global _spawn_plan, _spawn_tiles
    # if rc.get_current_round() == 200:

    #     rc.resign()
    # Sync round info
    map_info.update()
    comms.read()          # absorb every slot's shared tiles/symmetry
    alarm = comms.read_alarm()   # once per turn: also ages the sentry's heartbeat
    map_info.recompute_derived()
    for i in map_info.iter_mask(map_info._bm_env[map_info._IDX_ENV_WALL]):
        rc.draw_indicator_dot(i, 255, 255, 255)
    # The core's footprint never moves; compute the surrounding spawn ring once
    # _my_core is known (after the first observation).
    if not _spawn_tiles:
        _spawn_tiles = _compute_spawn_tiles()
    titanium = rc.get_global_resources()
    scaling = rc.get_scale_percent()
    core_pos = map_info._my_pos
    my_team = map_info._my_team
    
    # Initialize spawn plan
    if _spawn_plan is None:
        _spawn_plan = choose_spawn_plan(rc, core_pos, INITIAL_SPAWN_COUNT)
    if rc.get_current_round() <= INITIAL_SPAWN_COUNT + INITIAL_EXPLORE_MAX_STEPS:
        draw_spawn_plan(rc, core_pos, _spawn_plan, rc.get_map_width(), rc.get_map_height())

    # Measure repair staffing and any mobile threat before choosing this round's
    # single spawn. Repair throughput has first claim; a blocker is second.
    ally_builder_count, _has_close_ally, _closest_enemy = _scan_nearby_builders(core_pos, my_team)
    threat = _threat_to_answer(alarm)
    map_info.arm_reserve(threat is not None)
    # Measured unconditionally, above the opener guard: the reading is a
    # difference against the last one, so a skipped round loses real damage.
    measured = _measure_repair_demand()
    # A hardcoded opener, where one exists, owns the core's single spawn for as
    # long as its script runs -- including the turns it deliberately spawns
    # nothing while a launcher clears the tile it wants. Substituting a repairer,
    # a defender, or an economic builder on a different ring tile would put a
    # builder where the script's launchers cannot reach it, and every throw after
    # that is aimed at an empty tile.
    #
    # Repair throughput comes before chasing an enemy builder. A fresh repairer
    # can erase damage forever; a defender sent toward a mobile target often
    # abandons the economy without ever catching it.
    if opener.core_spawn():
        pass
    elif not _spawn_repairer(titanium, ally_builder_count, measured):
        if not (threat is not None and _spawn_defender(alarm, threat)):

            # Otherwise only spawn if we have extra resources.
            #
            # This used to gate on `get_scale_percent() + 200 < titanium`, which
            # compares a *percentage* against a titanium amount. The scale climbs
            # with every building the team has ever built — 273 by round 40 on
            # quarry — so the gate demanded 473 Ti and then kept rising, and the
            # core simply stopped making economic builders: unit counts froze
            # around 8 while the eventual winner ran 16. Gating on the bot's
            # actual cost plus a working buffer is the dimensionally correct
            # version and keeps the workforce growing.
            buffer = ECON_BUFFER_MANY if ally_builder_count >= 12 else ECON_BUFFER
            buffer = max(buffer, opener.builder_buffer())
            # A builder is only worth its cost if there is work for it. Builder
            # cost scales +20% per build while a harvester scales +5%, so the
            # seventh builder costs several harvesters, and the state trace on a
            # saga loss shows the marginal builders falling through to disrupt
            # (414 of ~1130 turns) and explore (206) -- walking, not building.
            # We finished that game 7 builders / 2 harvesters against loki's
            # 4 builders / 5 harvesters, and lost 390 Ti to 910. Tie workforce
            # growth to income: past a small starting crew, only add a builder if
            # the harvesters exist to pay for it.
            # Swept twice, all 33 maps both sides, against the champion of the
            # day. On the v44 base (before pay-as-you-go): *4 48.5%, *3 50.0%,
            # *2 54.5%, *1.5 54.5%, *1 51.5%. Re-swept on the v46 base once
            # pay-as-you-go had changed the economy, and the optimum moved
            # tighter as you would expect when harvesters get cheaper to reach:
            # *4 45.5%, *3 47.0%, *2 50.0% (baseline), *1.5 54.5%, *1 50.0%.
            #
            # *1.5 is the only setting that has beaten or matched every rival on
            # both bases, which is why it wins over *2 on a 36-30 margin that
            # would not carry on its own.
            #
            # The starting crew is sharply peaked at 4 -- 3 scores 43.9%, 5
            # scores 48.5%, 6 scores 39.4% -- so it stays put.
            free_crew = 4
            harvesters = defense.my_count(map_info._IDX_HARVESTER)
            crew_ok = ally_builder_count < free_crew or harvesters * 3 >= ally_builder_count * 2
            # Leave room under the 50-unit cap for launchers the opener has not
            # put up yet. A launcher is a unit, so an economy that fills every
            # slot with builders makes them unbuildable for the rest of the game
            # -- measured on drakkarfjord at units=50 with 4000 titanium banked
            # and can_build_launcher refusing forever.
            headroom = (rc.get_unit_count() + opener.needs_unit_slots()
                        < GameConstants.MAX_TEAM_UNITS)
            hire_ok = opener.may_hire(rc.get_unit_count(), titanium)
            if (crew_ok and headroom and hire_ok
                    and titanium >= rc.get_builder_bot_cost() + buffer):

                # First spawn according to initial plan, then spawn toward center
                if not _spawn_toward_plan(core_pos):
                    _spawn_toward_center()
    # The high nibble of the core broadcast leases slots 2..N+1 to the repair
    # crew. Broadcast after measuring damage so builders receive the new target
    # on the next round instead of one round later.
    comms.set_repair_target(_repair_target_count)
    comms.write()
    # Never let ammunition conversion consume the titanium our repair crew
    # needs for healing. Turret damage is deliberately secondary here: every
    # titanium left in this bank erases four HP of incoming fire.
    heal_bank = max(2, 10 + _repair_target_count)
    ammo_amount = min(min(50, rc.get_global_resources() - heal_bank),
                      rc.get_current_round() * 2) - rc.get_global_ammo()
    if ammo_amount > 0 and rc.can_convert_ammo(ammo_amount):
        rc.convert_ammo(ammo_amount)
