"""Two-builder siege around a core-facing gunner.

Attack slot 1 is the rendezvous anchor and gunner builder.  Slot 0 escorts it
and leads the pre-blocking work.  Once together, both derive the same site from
slot 1's position, so coordination does not consume the already-full store.
"""

from fcode import Direction, EntityType, Position

import comms
import map_info
import units.builder as builder
import units.atk_states.attack as attack


_CARDINALS = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)
_siege_site: Position | None = None
_siege_facing = None
_rejected_sites: set[Position] = set()
target: Position | None = None
phase = "siege"


def _bit(pos: Position) -> int:
    return 1 << (pos.x + pos.y * map_info._width)


def _cheb(a: Position, b: Position) -> int:
    return max(abs(a.x - b.x), abs(a.y - b.y))


def _manhattan(a: Position, b: Position) -> int:
    return abs(a.x - b.x) + abs(a.y - b.y)


def _let_partner_catch_up(partner: Position, destination: Position, atk_index: int) -> None:
    """The leading bot waits; the trailing bot advances toward the shared goal.

    Chasing the partner directly deadlocks when the 2x2 core or a wall lies
    between their spawn tiles (the Runestone/Sweden freeze).
    """
    mine = _manhattan(map_info._my_pos, destination)
    theirs = _manhattan(partner, destination)
    if mine < theirs or (mine == theirs and atk_index == 0):
        return
    builder.nav.move_to(destination, avoid_turret=False)


def _enemy_core_origin() -> Position | None:
    if map_info._their_core is not None:
        return map_info._their_core
    if map_info._solved_sym:
        return map_info._predicted_enemy_core
    # map_info._predicted_enemy_core is tiebroken independently in each entity
    # and made the pair chase different axes. This helper is shared with the
    # opening plan/launchers and deliberately returns the same guess for both.
    return builder.atk_symmetry_target()


def _visible_partner(atk_index: int) -> Position | None:
    partner_id = comms.atk_id(1 - atk_index)
    if not partner_id:
        return None
    for entity_id in builder.rc.get_nearby_units():
        if entity_id == partner_id:
            return builder.rc.get_position(entity_id)
    return None


def _empty_structure_site(pos: Position, ignore_bots: bool = False) -> bool:
    if not map_info.in_bounds(pos):
        return False
    bit = _bit(pos)
    if bit & (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
    ):
        return False
    if not ignore_bots and bit & (map_info._bm_friendly_bots | map_info._bm_enemy_bots):
        return False
    return True


def _adjacent_to_core(site: Position, origin: Position) -> bool:
    for dx in (0, 1):
        for dy in (0, 1):
            if _cheb(site, Position(origin.x + dx, origin.y + dy)) == 1:
                return True
    return False


def _choose_site(origin: Position, anchor: Position) -> tuple[Position | None, object | None]:
    choices = []
    # A base-relative anchor is identical in both processes and still means
    # "closest" in the useful sense: first viable site reached by the rush.
    approach = map_info._my_core or getattr(map_info, "_shared_core", None) or anchor
    # Every core-hitting gunner lies within three tiles of the 2x2 footprint.
    for y in range(max(0, origin.y - 3), min(map_info._height, origin.y + 5)):
        for x in range(max(0, origin.x - 3), min(map_info._width, origin.x + 5)):
            site = Position(x, y)
            if site in _rejected_sites:
                continue
            # Friendly builders are temporary occupants and are ignored only
            # for deterministic pair planning; enemies still invalidate it.
            # The run loop explicitly clears a friendly occupant before build.
            if (
                not _empty_structure_site(site, ignore_bots=True)
                or (_bit(site) & map_info._bm_enemy_bots)
            ):
                continue
            facing = attack._enemy_core_facing(site)
            if facing is None or (_bit(site) & map_info._bm_enemy_hard_threat):
                continue
            choices.append((
                0 if _adjacent_to_core(site, origin) else 1,
                abs(site.x - approach.x) + abs(site.y - approach.y),
                site.distance_squared(approach),
                x + y * map_info._width,
                site,
                facing,
            ))
    if not choices:
        return None, None
    best = min(choices)
    return best[-2], best[-1]


def _existing_core_siege_gunner(origin: Position) -> tuple[Position | None, object | None]:
    allied = (
        map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    choices = []
    for pos in map_info.iter_mask(allied):
        if _cheb(pos, origin) > 4:
            continue
        facing = attack._enemy_core_facing(pos)
        if facing is not None:
            choices.append((pos.x + pos.y * map_info._width, pos, facing))
    if not choices:
        return None, None
    best = min(choices)
    return best[1], best[2]


def _visible_enemy_builders() -> list[Position]:
    result = []
    my_team = builder.rc.get_team()
    for entity_id in builder.rc.get_nearby_units():
        if (
            builder.rc.get_entity_type(entity_id) == EntityType.BUILDER_BOT
            and builder.rc.get_team(entity_id) != my_team
        ):
            result.append(builder.rc.get_position(entity_id))
    return result


def _preblock_targets(site: Position) -> list[Position]:
    """Immediate enemy gunner placements whose fixed ray can kill our gunner."""
    targets = set()
    for enemy in _visible_enemy_builders():
        for direction in _CARDINALS:
            tile = map_info.pos_add(enemy, direction)
            if tile == site:
                continue
            if (
                _empty_structure_site(tile, ignore_bots=False)
                and _gunner_site_can_hit(tile, site)
            ):
                targets.add(tile)
    return sorted(targets, key=lambda p: (p.distance_squared(map_info._my_pos), p.x, p.y))


def _gunner_site_can_hit(build_pos: Position, target: Position) -> bool:
    """Whether some initial facing from build_pos has target in its clear ray."""
    for ray in map_info._GUNNER_RAYS:
        for dx, dy in ray:
            pos = Position(build_pos.x + dx, build_pos.y + dy)
            if not map_info.in_bounds(pos):
                break
            if pos == target:
                return True
            bit = _bit(pos)
            if (
                bit & map_info._bm_env[map_info._IDX_ENV_WALL]
                or bit & map_info._bm_any_building
            ):
                break
    return False


def _gunner_firing_ray(site: Position, facing) -> set[Position]:
    try:
        direction_index = map_info._DIRECTIONS.index(facing)
    except ValueError:
        return set()
    result = set()
    for dx, dy in map_info._GUNNER_RAYS[direction_index]:
        pos = Position(site.x + dx, site.y + dy)
        if not map_info.in_bounds(pos):
            break
        result.add(pos)
        bit = _bit(pos)
        if (
            bit & map_info._bm_env[map_info._IDX_ENV_WALL]
            or bit & map_info._bm_any_building
        ):
            break
    return result


def _barrier_at(pos: Position) -> bool:
    bit = _bit(pos)
    return bool(
        bit
        & map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )


def _near_site_stances(site: Position) -> set[Position]:
    result = set()
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            pos = Position(site.x + dx, site.y + dy)
            if max(abs(dx), abs(dy)) != 2 or not map_info.in_bounds(pos):
                continue
            if map_info.is_passable(pos):
                result.add(pos)
    return result


def _barrier_stances(pos: Position, home_site: Position) -> set[Position]:
    """Reachable build/repair stances, preferring the outside of the enclosure."""
    all_stances = set()
    outside = set()
    for direction in _CARDINALS:
        stance = map_info.pos_add(pos, direction)
        if (
            not map_info.in_bounds(stance)
            or not map_info.is_passable(stance)
        ):
            continue
        all_stances.add(stance)
        if _cheb(stance, home_site) > _cheb(pos, home_site):
            outside.add(stance)
    return outside or all_stances


def _reachable_mask(extra_block: int = 0) -> int:
    """Cardinal structural reachability, ignoring temporary bot occupancy."""
    blocked = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_blocked
        | map_info._bm_et[map_info._IDX_BARRIER]
        | extra_block
    )
    start = _bit(map_info._my_pos)
    passable = (map_info._board_mask & ~blocked) | start
    reached = start
    frontier = start
    while frontier:
        frontier = map_info.expand_manhattan(frontier) & passable & ~reached
        reached |= frontier
    return reached


def _reachable_barrier_targets(targets: list[Position], home_site: Position) -> list[Position]:
    """Drop cells already sealed behind the barriers we have placed.

    If no cardinal stance for a missing cell remains reachable, an enemy coming
    from our builder's outside component cannot reach that cell either, so an
    additional barrier there would only waste titanium.
    """
    reached = _reachable_mask()
    result = []
    for pos in targets:
        if any(_bit(stance) & reached for stance in _barrier_stances(pos, home_site)):
            result.append(pos)
    return result


def _build_or_camp_barrier(pos: Position, home_site: Position) -> None:
    my_pos = map_info._my_pos
    if abs(pos.x - my_pos.x) + abs(pos.y - my_pos.y) != 1:
        stances = _barrier_stances(pos, home_site)
        if stances:
            builder.nav.move_to(stances, avoid_turret=False)
        return
    if _barrier_at(pos):
        if (_bit(pos) & map_info._bm_damaged) and builder.rc.can_heal(pos):
            builder.rc.heal(pos)
        return
    if (
        builder.rc.can_build_barrier(pos)
        and builder.rc.get_global_resources()
        >= builder.rc.get_barrier_cost() + map_info.builder_ti_reserve()
    ):
        builder.rc.build_barrier(pos)
        map_info.update_at(pos)


def _gunner_at(site: Position) -> bool:
    bit = _bit(site)
    return bool(
        bit
        & map_info._bm_et[map_info._IDX_GUNNER]
        & map_info._bm_team[map_info._my_team_idx]
    )


def _protect_gunner(site: Position, facing, atk_index: int) -> None:
    # New builder positions can create new one-turn gunner placements. Deny
    # only those exact cells; a complete 3x3 barrier shell is unnecessary.
    immediate = _reachable_barrier_targets(_preblock_targets(site), site)
    if immediate:
        partner = _visible_partner(atk_index)
        if atk_index == 1 and partner is not None and _cheb(partner, site) <= 5:
            # Slot 0 is the dedicated blocker. Slot 1 stays ready beside the
            # gunner instead of both builders walking to the same barrier.
            if _cheb(map_info._my_pos, site) > 2:
                stances = _near_site_stances(site)
                if stances:
                    builder.nav.move_to(stances, avoid_turret=False)
            return
        _build_or_camp_barrier(immediate[0], site)
        return

    allied_barriers = (
        map_info._bm_et[map_info._IDX_BARRIER]
        & map_info._bm_team[map_info._my_team_idx]
    )
    nearby_barriers = [
        p for p in map_info.iter_mask(allied_barriers)
        if p.distance_squared(site) <= 20
    ]
    threatened = [
        p for p in nearby_barriers
        if _bit(p) & (map_info._bm_enemy_hard_threat | map_info._bm_damaged)
    ]
    threatened = _reachable_barrier_targets(threatened, site)
    if threatened:
        threatened.sort(key=lambda p: (p.distance_squared(map_info._my_pos), p.x, p.y))
        target = threatened[atk_index % len(threatened)]
        _build_or_camp_barrier(target, site)
        return
    # No enemy builder can place a killing gunner now. Return to the gunner and
    # wait for a new immediate threat instead of filling every adjacent cell.
    if _cheb(map_info._my_pos, site) > 2:
        stances = _near_site_stances(site)
        if stances:
            builder.nav.move_to(stances, avoid_turret=False)


def run(atk_index: int) -> bool:
    """Run the coordinated siege. Return False only before a core target exists."""
    global _siege_site, _siege_facing, _rejected_sites, target, phase
    origin = _enemy_core_origin()
    if origin is None:
        return False

    partner = _visible_partner(atk_index)
    if map_info._their_core is None and not map_info._solved_sym:
        # Use symmetry guesses only as a shared rendezvous/scouting target. A
        # gunner site is committed only after the real core is seen or symmetry
        # is solved, so a discarded guess can never receive the siege turret.
        phase = "siege-scout"
        if partner is not None and _cheb(partner, map_info._my_pos) > 2:
            _let_partner_catch_up(partner, origin, atk_index)
        else:
            builder.nav.move_to(origin, avoid_turret=False)
        return True

    existing_site, existing_facing = _existing_core_siege_gunner(origin)
    if existing_site is not None:
        _siege_site = existing_site
        _siege_facing = existing_facing
    if partner is None and _siege_site is None:
        # Both builders converge on the same core before committing a site.
        phase = "siege-rally"
        builder.nav.move_to(origin, avoid_turret=False)
        return True

    # If both can see each other, commit the shared site immediately; trying to
    # become adjacent first can deadlock on opposite sides of the enemy core.
    # After commitment only regroup if one actually leaves the work area.
    if (
        _siege_site is not None
        and partner is not None
        and _cheb(partner, map_info._my_pos) > 5
    ):
        phase = "siege-regroup"
        _let_partner_catch_up(partner, origin, atk_index)
        return True

    if existing_site is None:
        if atk_index == 1:
            # Slot 1 is the single authority for site changes. Local vision can
            # differ between the pair near the enemy core, so independently
            # rejecting threatened sites made Runestone's attackers split and
            # repeatedly pre-block different locations.
            if _siege_site is not None:
                site_bit = _bit(_siege_site)
                if site_bit & (
                    map_info._bm_enemy_hard_threat
                    | map_info._bm_any_building
                    | map_info._bm_enemy_bots
                ):
                    _rejected_sites.add(_siege_site)
                    _siege_site = None
                    _siege_facing = None
        else:
            shared_site = comms.shared_siege_site()
            if shared_site is not None and map_info.in_bounds(shared_site):
                _siege_site = shared_site
                _siege_facing = attack._enemy_core_facing(shared_site)
            elif _siege_site is None:
                phase = "siege-site-sync"
                builder.nav.move_to(origin, avoid_turret=False)
                return True

    if _siege_site is None:
        # Both agents use attack slot 1's position as the identical anchor.
        anchor = map_info._my_pos if atk_index == 1 else partner
        if anchor is None:
            builder.nav.move_to(origin, avoid_turret=False)
            return True
        _siege_site, _siege_facing = _choose_site(origin, anchor)
        if _siege_site is None:
            phase = "siege-no-site"
            builder.nav.move_to(origin, avoid_turret=False)
            return True

    if atk_index == 1 and existing_site is None:
        comms.publish_siege_site(_siege_site)

    site = _siege_site
    target = site
    facing = _siege_facing or attack._enemy_core_facing(site)
    if not _gunner_at(site) and _cheb(map_info._my_pos, site) > 3:
        phase = "siege-approach"
        builder.nav.move_adjacent(site, avoid_turret=False)
        return True
    if map_info._my_pos == site:
        phase = "siege-clear-site"
        stances = _near_site_stances(site)
        if stances:
            builder.nav.move_to(stances, avoid_turret=False)
        return True
    if _gunner_at(site):
        phase = "siege-enclose"
        _protect_gunner(site, facing, atk_index)
        return True

    # Every immediate enemy turret placement is physically denied before the
    # gunner goes down. Either member of the pair can perform this work.
    # Recompute reachability after every barrier. If the last placement sealed
    # another candidate away from the outside builder, that candidate no longer
    # needs its own barrier.
    preblocks = _reachable_barrier_targets(_preblock_targets(site), site)
    firing_ray = _gunner_firing_ray(site, facing)
    firing_conflict = next((tile for tile in preblocks if tile in firing_ray), None)
    if firing_conflict is not None:
        # A barrier here would protect the gunner but also block its core shot.
        # The authority selects another deterministic site; its partner simply
        # waits for the updated mailbox rather than inventing a second site.
        phase = "siege-site-risk"
        if atk_index == 1:
            _rejected_sites.add(site)
            _siege_site = None
            _siege_facing = None
        return True
    if preblocks:
        phase = "siege-preblock"
        if atk_index == 0 or partner is None or _cheb(partner, site) > 5:
            _build_or_camp_barrier(preblocks[0], site)
        else:
            # Slot 1 stages beside the gunner site while slot 0 walks the
            # outside and closes the dangerous enemy build cells one by one.
            if _cheb(map_info._my_pos, site) > 2:
                builder.nav.move_adjacent(site, avoid_turret=False)
        return True

    if atk_index == 0:
        # Once all immediate killing placements are blocked, the escort holds
        # and slot 1 may place the gunner this same round. It still follows the
        # gunner builder toward the site; "safe" must never mean freezing at
        # the point where symmetry happened to resolve.
        phase = "siege-safe"
        partner = _visible_partner(atk_index)
        if partner is None:
            builder.nav.move_adjacent(site, avoid_turret=False)
        elif _cheb(partner, map_info._my_pos) > 2:
            if not builder.nav.move_to_adjacent(partner, avoid_turret=False):
                builder.nav.move_adjacent(site, avoid_turret=False)
        return True

    if abs(site.x - map_info._my_pos.x) + abs(site.y - map_info._my_pos.y) != 1:
        phase = "siege-goto"
        builder.nav.move_adjacent(site, avoid_turret=False)
        return True
    if (
        facing is not None
        and builder.rc.can_build_gunner(site, facing)
        and builder.rc.get_global_resources()
        >= builder.rc.get_gunner_cost()
        + max(map_info.builder_ti_reserve(), attack.GUNNER_TI_FLOOR)
    ):
        phase = "siege-build"
        builder.rc.build_gunner(site, facing)
        attack.last_gunner_pos = site
        comms.note_gunner_built()
        map_info.update_at(site)
    else:
        phase = "siege-wait-ti"
    return True
