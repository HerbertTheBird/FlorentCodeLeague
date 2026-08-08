"""Detect and recover from an enemy builder mirroring this builder.

Mirroring is identified per enemy id from three consecutive meaningful movement
samples where the enemy repeats this builder's exact movement vector. Standing
still does not create evidence, avoiding false positives from two idle bots.
Completed economy defenders build a gunner initially faced at the mirror.
Attackers wait until the mirror prevents destination progress, then retreat to
a visible launcher, take an immediate core sentinel, or use the scaled-cost
gunner/positional-pressure fallback.
"""

from fcode import Controller, Direction, EntityType, Position

import comms
import map_info
import units.builder
import units.atk_states.attack as attack
from log import log
from pathing import Pathing


rc: Controller = None
nav: Pathing = None

MAX_SCORE = 11
MATCHES_REQUIRED = 3
TRACK_MAX_GAP = 2
REBUILD_COOLDOWN = 10
MIRROR_MAX_DISTANCE_SQ = 5
STALL_REQUIRED = 2
RECOVERY_MEMORY = 12

target: Position | None = None
_target_facing: Direction | None = None
_target_enemy_id = 0
_tracks: dict[int, tuple[int, Position, Position, int]] = {}
_last_update_round = -1
_cooldown_until = -1
_mode = ""
_progress_round = -1
_progress_destination: Position | None = None
_progress_position: Position | None = None
_progress_distance = 10 ** 9
_stalled_turns = 0
_recovery_enemy_id = 0
_recovery_until = -1
_recovery_position: Position | None = None


def init(c: Controller) -> None:
    global rc, nav
    rc = c
    nav = units.builder.nav


def _bit(pos: Position) -> int:
    return 1 << (pos.x + pos.y * map_info._width)


def _visible_enemy_builders() -> list[tuple[int, Position]]:
    result = []
    my_team = rc.get_team()
    for entity_id in rc.get_nearby_units():
        if (
            rc.get_entity_type(entity_id) == EntityType.BUILDER_BOT
            and rc.get_team(entity_id) != my_team
        ):
            result.append((entity_id, rc.get_position(entity_id)))
    return result


def _update_tracks() -> list[tuple[int, Position, int]]:
    global _tracks, _last_update_round
    current_round = rc.get_current_round()
    if _last_update_round == current_round:
        return [
            (enemy_id, enemy_pos, matches)
            for enemy_id, (seen, enemy_pos, _self_pos, matches) in _tracks.items()
            if seen == current_round and matches >= MATCHES_REQUIRED
        ]

    my_pos = map_info._my_pos
    visible = _visible_enemy_builders()
    new_tracks = {
        enemy_id: track
        for enemy_id, track in _tracks.items()
        if current_round - track[0] <= TRACK_MAX_GAP
    }
    confirmed = []
    for enemy_id, enemy_pos in visible:
        previous = _tracks.get(enemy_id)
        matches = 0
        if enemy_pos.distance_squared(my_pos) > MIRROR_MAX_DISTANCE_SQ:
            # Parallel travel across the map is not body-block mirroring. The
            # technique only works while the opponent stays next to us.
            new_tracks[enemy_id] = (current_round, enemy_pos, my_pos, 0)
            continue
        if previous is not None:
            seen_round, old_enemy, old_self, old_matches = previous
            if current_round - seen_round <= TRACK_MAX_GAP:
                my_delta = (my_pos.x - old_self.x, my_pos.y - old_self.y)
                enemy_delta = (enemy_pos.x - old_enemy.x, enemy_pos.y - old_enemy.y)
                if my_delta != (0, 0) or enemy_delta != (0, 0):
                    matches = old_matches + 1 if my_delta == enemy_delta else 0
                else:
                    matches = old_matches
        new_tracks[enemy_id] = (current_round, enemy_pos, my_pos, matches)
        if matches >= MATCHES_REQUIRED:
            confirmed.append((enemy_id, enemy_pos, matches))

    _tracks = new_tracks
    _last_update_round = current_round
    return confirmed


def _facing_to_builder(site: Position, enemy: Position) -> Direction | None:
    for di, direction in enumerate(map_info._DIRECTIONS):
        for dx, dy in map_info._GUNNER_RAYS[di]:
            tile = Position(site.x + dx, site.y + dy)
            if not map_info.in_bounds(tile):
                break
            if tile == enemy:
                return direction
            bit = _bit(tile)
            if (
                bit & map_info._bm_env[map_info._IDX_ENV_WALL]
                or bit & map_info._bm_any_building
            ):
                break
    return None


def _candidate_sites(enemy: Position) -> tuple[int, dict[int, Direction]]:
    w, h = map_info._width, map_info._height
    occupied = (
        map_info._bm_env[map_info._IDX_ENV_WALL]
        | map_info._bm_env[map_info._IDX_ENV_ORE_TI]
        | map_info._bm_any_building
        | map_info._bm_friendly_bots
        | map_info._bm_enemy_bots
        | map_info._bm_my_gunner_claims
    )
    mask = 0
    facings = {}
    for y in range(max(0, enemy.y - 3), min(h, enemy.y + 4)):
        for x in range(max(0, enemy.x - 3), min(w, enemy.x + 4)):
            site = Position(x, y)
            bit = _bit(site)
            if occupied & bit:
                continue
            facing = _facing_to_builder(site, enemy)
            if facing is None:
                continue
            n = x + y * w
            mask |= bit
            facings[n] = facing
    return mask, facings


def _can_afford() -> bool:
    reserve = max(map_info.builder_ti_reserve(), attack.GUNNER_TI_FLOOR)
    return rc.get_global_resources() >= rc.get_gunner_cost() + reserve


def _attack_destination() -> Position | None:
    """Stable destination used to decide whether a mirrored attacker is stuck."""
    from units.atk_states import sentinel_siege

    siege_target = sentinel_siege.target
    if siege_target is not None:
        return siege_target
    return (
        map_info._their_core
        or map_info._predicted_enemy_core
        or units.builder.atk_symmetry_target()
    )


def _update_progress(destination: Position | None) -> int:
    global _progress_round, _progress_destination, _progress_position
    global _progress_distance, _stalled_turns
    current_round = rc.get_current_round()
    if _progress_round == current_round:
        return _stalled_turns
    my_pos = map_info._my_pos
    distance = (
        abs(my_pos.x - destination.x) + abs(my_pos.y - destination.y)
        if destination is not None else 10 ** 9
    )
    if (
        destination is not None
        and destination == _progress_destination
        and _progress_position is not None
        and current_round - _progress_round <= TRACK_MAX_GAP
    ):
        moved = my_pos != _progress_position
        if not moved or distance >= _progress_distance:
            _stalled_turns += 1
        else:
            _stalled_turns = 0
    else:
        _stalled_turns = 0
    _progress_round = current_round
    _progress_destination = destination
    _progress_position = my_pos
    _progress_distance = distance
    return _stalled_turns


def _visible_relaunch_launcher():
    """Closest visible allied launcher to which the attacker can retreat."""
    launchers = (
        map_info._bm_et[map_info._IDX_LAUNCHER]
        & map_info._bm_team[map_info._my_team_idx]
        & map_info._bm_visible
    )
    candidates = []
    for launcher in map_info.iter_mask(launchers):
        candidates.append((launcher.distance_squared(map_info._my_pos), launcher.x, launcher.y, launcher))
    return min(candidates)[-1] if candidates else None


def _confirmed_enemy_position(enemy_id: int, confirmed):
    for eid, pos, _matches in confirmed:
        if eid == enemy_id:
            return pos
    track = _tracks.get(enemy_id)
    if track is not None and rc.get_current_round() - track[0] <= TRACK_MAX_GAP:
        return track[1]
    return None


def _score_attack_recovery(confirmed) -> int:
    global target, _target_facing, _target_enemy_id, _mode
    global _recovery_enemy_id, _recovery_until, _recovery_position

    current_round = rc.get_current_round()
    my_pos = map_info._my_pos

    # A large position jump, or a fresh adjacent launcher handoff, means the
    # retry worked. Yield immediately so sentinel_siege consumes the handoff.
    from units.atk_states import sentinel_siege
    handoff = comms.siege_insert(rc.get_id())
    if (
        _recovery_enemy_id
        and (
            (_recovery_position is not None
             and my_pos.distance_squared(_recovery_position) > 2)
            or (handoff is not None and my_pos.distance_squared(handoff[0]) == 1)
        )
    ):
        _recovery_enemy_id = 0
        _recovery_until = -1
        _recovery_position = my_pos
        return 0

    stalled = _update_progress(_attack_destination())
    if not _recovery_enemy_id and confirmed and (
        stalled >= STALL_REQUIRED or nav.stuck_turns >= STALL_REQUIRED
    ):
        confirmed.sort(
            key=lambda item: (
                -item[2], item[1].distance_squared(my_pos), item[0]
            )
        )
        _recovery_enemy_id = confirmed[0][0]
        _recovery_until = current_round + RECOVERY_MEMORY

    if not _recovery_enemy_id or current_round > _recovery_until:
        _recovery_enemy_id = 0
        return 0
    enemy = _confirmed_enemy_position(_recovery_enemy_id, confirmed)
    if enemy is None:
        return 0

    _recovery_position = my_pos
    _target_enemy_id = _recovery_enemy_id

    launcher = _visible_relaunch_launcher()
    if launcher is not None:
        target = launcher
        _mode = "launcher"
        return MAX_SCORE

    # With no usable visible launcher, take an immediate safe sentinel shot at
    # the core whenever the current cardinal stance permits it.
    sentinel_site, sentinel_facing = sentinel_siege.adjacent_sentinel_site()
    if (
        sentinel_site is not None
        and sentinel_facing is not None
        and rc.get_global_resources() >= rc.get_sentinel_cost()
        and rc.can_build_sentinel(sentinel_site, sentinel_facing)
    ):
        target = sentinel_site
        _target_facing = sentinel_facing
        _mode = "sentinel"
        return MAX_SCORE

    # get_gunner_cost() is already the fully scaled live price. At 30 Ti or
    # below, build a gunner initially faced at the mirroring bot. Above 30, do
    # not sink more economy into it; pressure the bot's position directly.
    if rc.get_gunner_cost() <= 30:
        candidates, facings = _candidate_sites(enemy)
        if candidates:
            site, _distance = nav.closest(candidates)
            if site is not None:
                target = site
                _target_facing = facings[site.x + site.y * map_info._width]
                _mode = "gunner"
                return MAX_SCORE
    target = enemy
    _mode = "pressure"
    return MAX_SCORE


def score() -> int:
    global target, _target_facing, _target_enemy_id, _mode
    confirmed = _update_tracks()
    target = None
    _target_facing = None
    _target_enemy_id = 0
    _mode = ""
    if units.builder._atk_bot:
        return _score_attack_recovery(confirmed)
    if (
        not units.builder._economy_builder
        or rc.get_current_round() < _cooldown_until
        or not _can_afford()
    ):
        return 0

    # Highest confidence first; if tied, punish the closest mirroring bot.
    confirmed.sort(
        key=lambda item: (
            -item[2],
            item[1].distance_squared(map_info._my_pos),
            item[0],
        )
    )
    for enemy_id, enemy_pos, _matches in confirmed:
        if _bit(enemy_pos) & map_info._bm_my_gunner_claims:
            continue  # an existing gunner already owns this target
        candidates, facings = _candidate_sites(enemy_pos)
        if not candidates:
            continue
        site, _distance = nav.closest(candidates)
        if site is None:
            continue
        target = site
        _target_facing = facings[site.x + site.y * map_info._width]
        _target_enemy_id = enemy_id
        _mode = "gunner"
        return MAX_SCORE
    return 0


def run() -> None:
    global _cooldown_until, _recovery_enemy_id
    log("COUNTER MIRROR")
    if target is None:
        return
    if _mode == "launcher":
        comms.request_siege_relaunch(rc.get_id())
        nav.move_to_adjacent(target, avoid_turret=False)
        return
    if _mode == "pressure":
        nav.move_to_adjacent(target, avoid_turret=False)
        return
    if _target_facing is None:
        return
    if map_info._my_pos.distance_squared(target) != 1:
        nav.move_adjacent(target, avoid_turret=False)
        return
    if _mode == "sentinel":
        if (
            rc.get_global_resources() >= rc.get_sentinel_cost()
            and rc.can_build_sentinel(target, _target_facing)
        ):
            rc.build_sentinel(target, _target_facing)
            from units.atk_states import sentinel_siege
            sentinel_siege._placed_locally += 1
            sentinel_siege.last_positions.append(target)
            comms.note_sentinel_built()
            map_info.update_at(target)
            _recovery_enemy_id = 0
        return
    if _can_afford() and rc.can_build_gunner(target, _target_facing):
        rc.build_gunner(target, _target_facing)
        comms.note_gunner_built()
        map_info.update_at(target)
        _cooldown_until = rc.get_current_round() + REBUILD_COOLDOWN
