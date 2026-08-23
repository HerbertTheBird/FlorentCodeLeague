"""The core: spawn the rusher, maybe two harvesters' worth of help, then spend
every spare titanium on ammunition.

The core does almost nothing, and that is the strategy. It has no defence, no
build-out and no second wave -- the reference bot's core spawns one builder on
round 0 and then converts titanium for the rest of the game. See config.py for
why the titanium cannot be spent twice.
"""

import config
import geom
from fcode import (Direction, EntityType, Environment, GameConstants,
                   Position)


def core_turn(ct, st):
    st.board.my_core = ct.get_position()
    area = st.board.w * st.board.h
    round_num = ct.get_current_round()

    hp = ct.get_hp()
    if hp > st.core_max:
        st.core_max = hp

    spawned = False
    if not st.rusher_named:
        # The rusher comes first and is retried until it is actually on the
        # board: with 500 titanium in the bank and nothing else to spend it on,
        # a round-0 spawn blocked by terrain must not cost us the whole game.
        spawned = _spawn_toward(ct, st, _enemy_bearing(ct, st))
        st.rusher_named = spawned
    elif (config.ECON_ENABLED and area > config.ECON_MIN_AREA
            and st.eco_spawned < config.ECON_MAX_BUILDERS
            and round_num <= config.ECON_LAST_ROUND
            and _worth_an_economy_builder(ct, st)):
        spawned = _spawn_toward(ct, st, _ore_bearing(ct, st))
        st.eco_spawned += spawned

    elif _wants_healer(ct, st, round_num, hp):
        # Away from the enemy and out of the line of any battery they have
        # already planted -- a healer that spawns on a sentinel's ray is 60
        # titanium delivered to their gunnery practice.
        me = ct.get_position()
        enemy = st.board.enemy_core() or me
        away = Position(2 * me.x - enemy.x, 2 * me.y - enemy.y)
        if _spawn_toward(ct, st, away, avoid=_shot_tiles(ct)):
            st.healers += 1
            spawned = True

    _convert(ct, st, spawned)


def _raider_at_home(ct):
    """An enemy builder bot inside our core's vision.

    This is the trigger the rush telegraphs: their single builder crossed the
    map, our rusher saw it go and left a note in the store, and now it is here.
    Nothing else it could be -- an economic opponent builds its conveyors near
    its own base, not next to ours.
    """
    mine = ct.get_team()
    try:
        for uid in ct.get_nearby_units():
            if ct.get_team(uid) == mine:
                continue
            if ct.get_entity_type(uid) == EntityType.BUILDER_BOT:
                return True
    except Exception:
        pass
    return False


def _shot_tiles(ct):
    """Every tile a visible enemy turret already covers.

    A sentinel cannot rotate, so its ray is fixed at build time and knowable:
    ask the engine what a turret of that type at that position and facing can
    hit, and never spawn anything onto the answer.
    """
    danger = set()
    mine = ct.get_team()
    try:
        for bid in ct.get_nearby_buildings():
            if ct.get_team(bid) == mine:
                continue
            kind = ct.get_entity_type(bid)
            if kind not in (EntityType.SENTINEL, EntityType.GUNNER):
                continue
            for q in ct.get_attackable_tiles_from(ct.get_position(bid),
                                                  ct.get_direction(bid), kind):
                danger.add((q.x, q.y))
    except Exception:
        pass
    return danger


def _wants_healer(ct, st, round_num, hp):
    """Spawn a healer when repairing beats shooting, and not before.

    Two triggers. Either their raider has arrived at our core -- in which case
    the fight is a repair race we win, and the spec calls for two healers -- or
    the game has simply gone long and we are being worn down.

    The lateness gate is what makes this nearly free: by HEALER_ROUND the
    battery is already standing, so the +20% a builder puts on the global cost
    scale has nothing left to inflate. Every previous second-builder arm was
    spawned early and lost heavily on exactly that (economy 19.8-39.5%).
    """
    if not config.HEALER_ENABLED or st.healers >= config.HEALER_MAX:
        return False
    # NEVER at the expense of the battery. The cost scale is GLOBAL: each
    # builder puts +20% on the price of every sentinel still to be built, so a
    # healer spawned while the rush is still paying for itself does not just
    # cost 30 titanium, it can price the battery out of existence. Measured with
    # this gate missing -- the raider trigger fires around t20-30, well before
    # the battery is finished -- the mirror went 43.3% -> 21.7%.
    # NOT ONE TITANIUM BEFORE THE BATTERY IS FINISHED. The cost scale is
    # GLOBAL: each builder puts +20% on the price of every sentinel still to be
    # built, so an early healer does not cost 30 titanium, it costs 20% of the
    # whole rush -- and passive income means an affordability check passes
    # anyway, which is exactly the trap. Measured with only an affordability
    # gate, the mirror ran 61.7% -> 21.7%.
    if ct.read_store(config.SLOT_SENTINELS) < config.SENTINEL_TARGET:
        return False
    if ct.get_global_resources() < ct.get_builder_bot_cost():
        return False
    # And only in a confirmed rush mirror: their one builder is on its way to
    # us and their base is empty. Against an economy this is just a builder we
    # cannot afford.
    if not (ct.read_store(config.SLOT_RAIDER_SEEN)
            and ct.read_store(config.SLOT_BASE_EMPTY)):
        return False
    if hp >= st.core_max * config.HEALER_HP_FRACTION:
        return False
    return _raider_at_home(ct) or round_num >= config.HEALER_ROUND


def _enemy_bearing(ct, st):
    """Which way to put the rusher down: the side of the core facing the enemy,
    so its first step is not around its own building."""
    core = st.board.enemy_core()
    me = ct.get_position()
    if core is None:
        return Position(st.board.w // 2, st.board.h // 2)
    return core


def _ore_bearing(ct, st):
    me = ct.get_position()
    best = None
    for p in ct.get_nearby_tiles():
        try:
            if ct.get_tile_env(p) != Environment.ORE_TITANIUM:
                continue
        except Exception:
            continue
        d = abs(p.x - me.x) + abs(p.y - me.y)
        if d > config.ECON_MAX_ROUTE:
            continue
        if best is None or d < best[0]:
            best = (d, p)
    return me if best is None else best[1]


def _worth_an_economy_builder(ct, st):
    """Only if the core can SEE ore inside ECON_MAX_ROUTE, and only if paying for
    the builder still leaves the remaining sentinels affordable."""
    if _ore_bearing(ct, st) == ct.get_position():
        return False
    left = max(0, config.SENTINEL_TARGET - ct.read_store(config.SLOT_SENTINELS))
    # The cost scale is GLOBAL, not per-category (probed: spawning builders took
    # sentinel/gunner/launcher/harvester/conveyor costs from 30/20/20/20/3 to
    # 60/40/40/40/6). So an extra builder does not just cost its own price -- it
    # raises the price of everything still to be built by 20%.
    need = left * (ct.get_sentinel_cost() + 6)
    return ct.get_global_resources() >= need + ct.get_builder_bot_cost()


def _spawn_toward(ct, st, target, avoid=()):
    if ct.get_action_cooldown() != 0:
        return False
    if ct.get_global_resources() < ct.get_builder_bot_cost():
        return False
    me = ct.get_position()
    # Rank the spawn ring by how much closer to `target` it leaves the builder.
    #
    # There is no free tempo here, which is worth recording because it looks like
    # there should be. CORE_SPAWNING_RADIUS_SQ is 2, NOT the 8 of
    # CORE_ACTION_RADIUS_SQ -- probed directly, the legal offsets from a 2x2 core
    # are exactly the ring: x and y each in -1..2, corners included, nothing
    # further out. Widening this search finds the same eleven tiles and buys
    # nothing.
    cands = []
    for x in range(me.x - 1, me.x + 3):
        for y in range(me.y - 1, me.y + 3):
            p = Position(x, y)
            if not st.board.in_bounds(x, y):
                continue
            try:
                if not ct.can_spawn(p):
                    continue
            except Exception:
                continue
            # Ranked so that any tile out of the enemy's fire beats every
            # tile in it, and bearing only breaks ties.
            cands.append((1 if (x, y) in avoid else 0,
                          abs(x - target.x) + abs(y - target.y), p))
    if not cands:
        return False
    cands.sort(key=lambda t: (t[0], t[1]))
    try:
        new_id = ct.spawn_builder(cands[0][2])
    except Exception:
        return False
    st.last_spawn_id = new_id
    if ct.get_current_round() == 0:
        # Name the rusher. Role by id is the only assignment that survives a
        # round-0 spawn failing: if the spawn tiles were all blocked and the
        # rusher only goes down on round 2, this still points at the right unit.
        ct.write_store(config.SLOT_RUSHER_ID, new_id)
    return True


def _convert(ct, st, spawned_this_turn):
    """Hold 120 ammunition, keep 10 titanium, convert everything else.

    This is the reference bot's policy, read straight off the wire: the replay
    records global ammunition (economy event field 6 -> per-team submessage f7,
    alongside f1 titanium), and in 15 of 15 games -- every map size, every
    opponent -- not adgato's core converts EXACTLY 120 titanium on round 0,
    before its builder has taken a step, and thereafter tops back up to 120 every
    single turn. Ammunition never exceeds 120 in any game.

    It needs no sentinel reserve because the cap IS the reserve: conversion stops
    at 120, so the rest of the bank simply stays as titanium until the sentinels
    are built and start burning it. The elaborate reserve this replaces was doing
    the opposite of its job -- holding titanium back from turrets that were
    standing, aimed, and out of ammunition.

    The budget, corrected: there is ONE GLOBAL cost scale, additive on each base
    cost, and every entity of every kind contributes to it (builders and turrets
    +20%, launchers +10%, harvesters +5%, conveyors and barriers +1%). So the
    round-0 builder makes sentinel #1 cost floor(1.2*30)=36 and the ladder is
    36/42/48/54 = 180, not the 138 this file used to claim. Builder plus battery is 210 Ti, leaving
    ~290 of the starting stack: 29 shots at 10 ammunition each, 522 damage
    against a 500 HP core. The rush is budgeted to the last shot, which is why
    nothing may be spent on anything else.
    """
    keep = config.TITANIUM_FLOOR
    if st.healers and ct.get_hp() < st.core_max:
        # Stop feeding the guns. While the healer has something to repair, a
        # titanium in its hands is worth 4 HP and the same titanium in a
        # sentinel is worth 1.8 -- see HEAL_RESERVE.
        keep = config.HEAL_RESERVE
    want = config.AMMO_TARGET - ct.get_global_ammo()
    if want <= 0:
        return
    amount = min(want, ct.get_global_resources() - keep)
    if amount <= 0:
        return
    try:
        if ct.can_convert_ammo(amount):
            ct.convert_ammo(amount)
    except Exception:
        pass
