from main import has_op
from fcode import Controller, Direction, Position, EntityType
import map_info
from log import log, DRAW_DEBUG
from units.spawn_plan import choose_spawn_plan, draw_spawn_plan, INITIAL_SPAWN_COUNT, INITIAL_EXPLORE_MAX_STEPS
import units.defense as defense
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


def _spawn_best_toward(target: Position) -> bool:
    """Spawn a builder on the surrounding tile closest to `target`. Returns True
    if a builder was spawned."""
    best = None
    best_d = None
    for p in _spawn_tiles:
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


def _spawn_under_siege(titanium: int) -> bool:
    """While a turret is shooting the core, turn banked titanium into bodies.

    The economic spawn gate wants ~200 Ti of headroom before it will make a
    builder. That is right in peacetime and suicidal under fire: instrumented
    losses show the core sitting on 200-240 Ti with five units while three enemy
    sentinels take it from 500 HP to 0 in twenty rounds. Titanium in the bank is
    worth nothing if the core dies, and each builder is another 2 damage a turn
    against a 30-40 HP turret, so under siege we spend down to the floor.
    """
    global _last_core_hp
    hp = rc.get_hp()
    losing_hp = _last_core_hp is not None and hp < _last_core_hp
    _last_core_hp = hp
    # Gate on damage actually landing, not merely on a turret having line of
    # sight. "Could shoot us" fires on any turret that happens to face our way
    # and had us emptying the bank against threats that were never going to
    # connect — worth about eight points against loki on its own.
    if not losing_hp:
        return False
    besiegers = defense.core_besiegers(rc)
    if not besiegers:
        return False
    cost = rc.get_builder_bot_cost()
    # Leave a working balance: spending literally to the floor buys bodies we
    # then cannot afford to build barriers or attack with.
    if titanium < cost + SIEGE_SPAWN_FLOOR:
        return False
    if rc.get_unit_count() >= SIEGE_MAX_UNITS:
        return False
    if _spawn_best_toward(besiegers[0][0]):
        log(f"core spawned siege responder toward {besiegers[0][0]} (ti={titanium})")
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
    comms.read()          # absorb every slot's shared tiles/symmetry, broadcast our own
    alarm = comms.read_alarm()   # ages slot 15 by round number, so calling it
                                 # more than once a turn is harmless
    comms.write()
    map_info.recompute_derived()
    # Ungated, this walks every wall the team has ever seen and issues one
    # draw call per tile, every round, in a shipped build. It is cheap while the
    # map is mostly unexplored and stops being cheap the moment it is not --
    # 164 tiles on saga, 208 on archipelago -- and the hardcoded-map layer
    # hands the core all of them on turn 0. Behind DRAW_DEBUG like every other
    # indicator in the bot.
    if DRAW_DEBUG:
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

    # On-demand defence takes priority over every economic spawn: the whole
    # point of reserving a builder's cost (map_info.ti_reserve) is that this
    # spawn is always affordable the round it is needed.
    ally_builder_count, _has_close_ally, _closest_enemy = _scan_nearby_builders(core_pos, my_team)
    threat = _threat_to_answer(alarm)
    map_info.arm_reserve(threat is not None)
    if not (threat is not None and _spawn_defender(alarm, threat)):
        if not _spawn_under_siege(titanium):

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
            if crew_ok and titanium >= rc.get_builder_bot_cost() + buffer:

                # First spawn according to initial plan, then spawn toward center
                if not _spawn_toward_plan(core_pos):
                    _spawn_toward_center()
    ammo_amount = min(min(50, rc.get_global_resources()-2), rc.get_current_round() * 2) - rc.get_global_ammo()
    if ammo_amount > 0 and rc.can_convert_ammo(ammo_amount):
        rc.convert_ammo(ammo_amount)
