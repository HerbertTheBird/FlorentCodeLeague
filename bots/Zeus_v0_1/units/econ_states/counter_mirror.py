"""Detect and punish an enemy builder that is mirroring this builder.

Mirroring is identified per enemy id from three consecutive meaningful movement
samples where the enemy repeats this builder's exact movement vector. Standing
still does not create evidence, avoiding false positives from two idle bots.
Once confirmed, the defender builds a gunner whose initial ray contains that
builder; Zeus gunners are taught to fire at and rotate toward builders.
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

target: Position | None = None
_target_facing: Direction | None = None
_target_enemy_id = 0
_tracks: dict[int, tuple[int, Position, Position, int]] = {}
_last_update_round = -1
_cooldown_until = -1


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


def score() -> int:
    global target, _target_facing, _target_enemy_id
    confirmed = _update_tracks()
    target = None
    _target_facing = None
    _target_enemy_id = 0
    if (
        not (units.builder._economy_builder or units.builder._atk_bot)
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
        return MAX_SCORE
    return 0


def run() -> None:
    global _cooldown_until
    log("COUNTER MIRROR")
    if target is None or _target_facing is None:
        return
    if map_info._my_pos.distance_squared(target) != 1:
        nav.move_adjacent(target, avoid_turret=False)
        return
    if _can_afford() and rc.can_build_gunner(target, _target_facing):
        rc.build_gunner(target, _target_facing)
        comms.note_gunner_built()
        map_info.update_at(target)
        _cooldown_until = rc.get_current_round() + REBUILD_COOLDOWN
