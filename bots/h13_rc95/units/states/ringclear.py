"""Builder state: keep OUR OWN core ring clear of enemy structures.

The mirror of siege.py. Our core is 2x2 and delivery into it is a directed
cardinal push, so the eight cardinally-adjacent tiles are the only ones that can
feed it. An enemy barrier or conveyor sitting on one of those tiles throttles our
economy at the sink -- and unlike a walled ore tile, we cannot route around it.

Nothing else in the bot treats that tile as special:
  * `block` (10) walls enemy conveyor OUTPUT tiles, not our own ring.
  * `attack` (9) answers enemy turrets by BUILDING turrets -- a lump sum, and
    then the turret is scrapped, which measured as a breakeven trade.
  * `chip` (3.9) attacks enemy buildings, but flat, so a building strangling our
    core ranks no higher than one in a far corner.

Builder attack rather than a turret, because the economics favour it here:
  builder attack   2 damage / turn for 2 Ti, no lump sum, already in position
  gunner           7 damage / turn for a turret cost, then scrapped
A barrier is BARRIER_MAX_HP=30, so one builder clears it in 15 turns for 30 Ti;
a conveyor is 20 HP, 10 turns, 20 Ti. Both are cheap next to a throttled core.

THE HEALER EXCEPTION. An enemy builder heals HEAL_AMOUNT=4 per turn for 1 Ti
against our 2 damage for 2 Ti -- one healer beats one attacker outright, and
does it at a quarter of the cost. Three attackers (6) are needed to out-damage
one healer, so below that we defer and let `attack` answer with a gunner, whose
7/turn wins alone. This is the case the user flagged: "in the worst case where
enemy builder is healing it, perhaps put a turret down".
"""
import map_info
import pathing
from pathing import Pathing
import units.builder
from fcode import *
from log import log

rc: Controller = None
nav: Pathing = None


def init(c: Controller):
    global rc, nav
    rc = c
    nav = units.builder.nav


# Sits ABOVE harvest (7) and route (8) -- clearing our own sink outranks growing
# the economy, because a throttled core makes that growth undeliverable -- but
# BELOW attack (9), heal (9.5) and block (10).
MAX_SCORE = 9.5
# Do not cross the map for this. Our ring is near our own spawn, so a builder
# that is far away has better things to do and a nearer one will claim it.
LEASH = 8
# Attackers needed to out-damage one enemy healer: 3 * 2 > 4.
ATTACKERS_PER_HEALER = 3

_cached_target = None


def _ring_tiles() -> int:
    """The eight cardinally-adjacent tiles of OUR 2x2 core."""
    core = map_info._bm_my_core_area
    if not core:
        return 0
    return map_info.manhattan(core) & ~core & map_info._board_mask


def _enemy_buildings() -> int:
    return map_info._bm_any_building & map_info._bm_team[1 - map_info._my_team_idx]


def _blockers() -> int:
    """Enemy structures sitting on our ring."""
    return _ring_tiles() & _enemy_buildings()


def _healer_protected(tile_bit: int) -> bool:
    """True if enemy healing out-paces what our nearby builders can deal.

    Counts enemy builders adjacent to the target against our own builders already
    adjacent to it. Each enemy heals 4/turn; each of ours deals 2/turn.
    """
    adj = map_info.manhattan(tile_bit) & map_info._board_mask
    healers = (adj & map_info._bm_enemy_bots).bit_count()
    if not healers:
        return False
    attackers = (adj & map_info._bm_friendly_bots).bit_count()
    return attackers < healers * ATTACKERS_PER_HEALER


def score(can_move=True):
    global _cached_target
    _cached_target = None
    blockers = _blockers()
    if not blockers:
        return 0
    if rc.get_global_resources() < GameConstants.BUILDER_BOT_ATTACK_COST:
        return 0
    # Never stand in turret fire to chip a barrier -- a builder loses that trade.
    reachable = blockers & ~map_info._bm_enemy_turret_threat
    if not reachable:
        return 0
    claims = pathing.claim_subset(
        1 << (map_info._my_pos.x + map_info._my_pos.y * map_info._width),
        map_info._bm_friendly_bots, reachable, tie_self=True)
    if not claims:
        return 0
    if not can_move:
        my = map_info._my_pos
        for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            p = Position(my.x + d[0], my.y + d[1])
            if not map_info.in_bounds(p):
                continue
            bit = 1 << (p.x + p.y * map_info._width)
            if (claims >> (p.x + p.y * map_info._width)) & 1 and not _healer_protected(bit):
                _cached_target = p
                return MAX_SCORE
        return 0
    target, dist = nav.closest(claims, to_adjacent=True)
    if target is None or dist > LEASH:
        return 0
    bit = 1 << (target.x + target.y * map_info._width)
    if _healer_protected(bit):
        # Let `attack` put a gunner on it instead; our 2/turn cannot win here.
        return 0
    _cached_target = target
    return MAX_SCORE


def run(can_move=True):
    target = _cached_target
    if target is None:
        return
    log("RINGCLEAR", target)
    # Attack from wherever we already stand if we are in range; otherwise close.
    if rc.can_fire(target) and rc.get_global_resources() >= GameConstants.BUILDER_BOT_ATTACK_COST:
        rc.fire(target)
        return
    if nav.move_adjacent(target, can_move=can_move):
        return
    if rc.can_fire(target) and rc.get_global_resources() >= GameConstants.BUILDER_BOT_ATTACK_COST:
        rc.fire(target)
