from main import has_op
import map_info
from fcode import Controller, Direction, Position, EntityType, GameConstants
import units.builder as builder
import random
from log import DRAW_DEBUG, log
from _config import CONVEYOR_COST_DISCOUNT

CARD_DIR = [
    Direction.NORTH,
    Direction.SOUTH,
    Direction.EAST,
    Direction.WEST,
]

barrier_cost = 15
threat_cost = 3
conveyor_end_cost = 4
# Conveyor routing (bfs_route) may run a route THROUGH an enemy barrier -- it is not
# impassable, just expensive (~10 extra route-steps), so the search prefers a detour
# of up to this length before committing to attack a barrier down. route.run fires on
# the barrier instead of building until it dies, then lays the conveyor.
BARRIER_ROUTE_COST = 10


# Offsets (dx, dy) such that lsb_pos = target_pos + (dx, dy) covers all 9
# tiles of the 3x3 around target_pos within d^2 <= 20. Worst-case corner is
# (target + (sign(dx), sign(dy))), so the predicate is
# (|dx|+1)^2 + (|dy|+1)^2 <= 20. Constant set, precomputed once.
_FULL_COVER_OFFSETS = [
    (dx, dy)
    for dy in range(-3, 4) for dx in range(-3, 4)
    if (abs(dx) + 1) ** 2 + (abs(dy) + 1) ** 2 <= 20
]



def voronoi_claim_tiebreak(my_mask, lose_mask, win_mask, claims, passable=None):
    """Voronoi partition of `claims` for ME against other friendly bots, with a
    per-competitor equal-distance tie rule: at EQUAL distance I beat everyone in
    `lose_mask` (I take the tile) but lose to everyone in `win_mask` (they take it).
    lose_mask | win_mask is the full set of other bots. Returns my claimed subset."""
    if not claims:
        return 0
    if not (lose_mask or win_mask):
        return claims                       # no competitors -> everything is mine
    if passable is None:
        passable = map_info.passable()
    w = map_info._width
    nlc = map_info._not_left_col
    nrc = map_info._not_right_col
    my_front = my_mask
    lose_front = lose_mask
    win_front = win_mask
    my_claims = 0
    all_claimed = my_front | lose_front | win_front
    while claims and (my_front or lose_front or win_front):
        # 1) win-group takes equal-distance tiles BEFORE me -> they win ties vs me
        if win_front:
            claims &= ~(map_info.manhattan(win_front) & claims)
        # 2) I take what's left this layer -> I beat lose-group on ties
        if my_front and claims:
            got = map_info.manhattan(my_front) & claims
            my_claims |= got
            claims &= ~got
        # 3) lose-group only takes tiles strictly closer to it than to me
        if lose_front and claims:
            claims &= ~(map_info.manhattan(lose_front) & claims)
        if my_front:
            e = (my_front | ((my_front & nrc) << 1) | ((my_front & nlc) >> 1) | (my_front << w) | (my_front >> w)) & passable & ~all_claimed
            all_claimed |= e
            my_front = e
        if win_front:
            e = (win_front | ((win_front & nrc) << 1) | ((win_front & nlc) >> 1) | (win_front << w) | (win_front >> w)) & passable & ~all_claimed
            all_claimed |= e
            win_front = e
        if lose_front:
            e = (lose_front | ((lose_front & nrc) << 1) | ((lose_front & nlc) >> 1) | (lose_front << w) | (lose_front >> w)) & passable & ~all_claimed
            all_claimed |= e
            lose_front = e
    return my_claims


def claim_subset(
    my_mask: int,
    others_mask: int,
    claims: int,
    passable: int | None = None,
    tie_self: bool = True,
) -> int:
    """Voronoi-partition `claims` between me (`my_mask`) and the other friendly bots
    in `others_mask`, using the per-bot equal-distance tie rule from map_info: I take
    a tied tile unless the competitor is one I only know globally/staler AND has a
    lower id (then it takes it). `tie_self` is retained for signature compatibility but
    no longer used -- the id/observed rule supersedes the old blanket tie direction."""
    if not claims:
        return 0
    if passable is None:
        passable = map_info.passable()
    lose = others_mask & map_info._bm_friendly_tie_lose
    win = others_mask & map_info._bm_friendly_tie_win
    return voronoi_claim_tiebreak(my_mask, lose, win, claims, passable)

class Pathing:
    width = height = 0
    rc: Controller

    stuck_turns = 0
    prev_pos = None

    target_p = None

    last_dir = None
    last_last_dir = None
    def bfs_dist(self, start, end, unknown_passable = True):
        w = self.width
        start_mask = 1 << (start.x + start.y * w)
        end_mask = 1 << (end.x + end.y * w)
        passable = map_info.passable() if unknown_passable else (map_info.passable() & map_info._bm_seen)
        dist = 0
        if start_mask & end_mask:
            return 0
        while start_mask & end_mask == 0:
            start_mask = map_info.expand_manhattan(start_mask) & passable
            passable &= ~start_mask
            if start_mask == 0:
                return -1
            dist+= 1
        return dist

    def _coerce_start(self, pos) -> int:
        """Convert `pos` to a start bitmask. Accepts None (defaults to my_pos),
        a single Position, an int (already-mask), or any iterable of Positions."""
        w = map_info._width
        if pos is None:
            my = map_info._my_pos
            return 1 << (my.x + my.y * w)
        if isinstance(pos, Position):
            return 1 << (pos.x + pos.y * w)
        if isinstance(pos, int):
            return pos
        mask = 0
        for p in pos:
            mask |= 1 << (p.x + p.y * w)
        return mask

    def _closest_impl(
        self,
        targets: int,
        start: int,
        max_dist: int | None = None,
        avoid: int = 0,
        side: bool = True,
        to_adjacent: bool = True,
    ) -> tuple[Position | None, int]:
        """Shared bitmask BFS for closest-target queries.

        BFS expands outward from any bit set in `start` simultaneously and
        returns the first target reached by Manhattan (4-neighbour) distance,
        breaking ties by lowest tile index — builder bots move cardinal-only, so
        that is the movement metric. Distance is the BFS layer count from the
        nearest start tile. When `max_dist` is provided, the search stops after
        exploring that many layers.

        `avoid` is an optional bitmask of tiles to additionally treat as
        impassable (e.g. enemy can't path through tiles next to our launchers).
        Note that with `side=True` the target tiles are re-added to the passable
        set afterwards (targets are usually buildings, which are never passable),
        so callers must pre-subtract `avoid` from `targets` if they need a
        target inside `avoid` to be unreachable.

        `side` selects whose perspective the passable mask reflects. True (the
        default) is our perspective — uses `map_info.passable()` which
        avoids enemy threat. False models the enemy's pathing: same blockers
        minus the enemy's own threat (they wouldn't avoid it).

        `to_adjacent` selects what "reaching" a target means. True (the default)
        returns the distance to a tile ORTHOGONALLY ADJACENT to a target -- the
        usual case, since targets are buildings we act on from a neighbour. False
        returns the distance to move ONTO the target tile itself (e.g. chip wants
        to stand on the tile); the target tiles are made passable so the search
        may walk onto them.
        """
        if targets == 0 or start == 0:
            return None, -1
        w = map_info._width
        if side:
            passable = map_info.passable()
        else:
            passable = (
                ~map_info.get_avoid(False, enemy_pov=not side)
                & map_info._board_mask
            )
        if avoid:
            passable &= ~avoid
        if not to_adjacent:
            passable |= targets
        visited = start
        frontier = start
        dist = 0
        nlc = map_info._not_left_col
        nrc = map_info._not_right_col
        while frontier:
            hit = (frontier if not to_adjacent else map_info.manhattan(frontier)) & targets
            if hit:
                lsb = hit & -hit
                n = lsb.bit_length() - 1
                return Position(n % w, n // w), dist
            if max_dist is not None and dist >= max_dist:
                break
            visited |= frontier
            dist += 1
            expanded = frontier | ((frontier & nrc) << 1) | ((frontier & nlc) >> 1) | (frontier << w) | (frontier >> w)
            frontier = expanded & passable & ~visited
        return None, -1

    def closest(
        self,
        targets: int,
        pos=None,
        avoid: int = 0,
        side: bool = True,
        to_adjacent: bool = True,
    ) -> tuple[Position | None, int]:
        """Find closest bit in *targets* from *pos* with full search.

        `pos` accepts None (defaults to my_pos), a single Position, a bitmask
        int, or any iterable of Positions — BFS expands from all start tiles
        simultaneously and returns the closest target. `to_adjacent=False`
        measures distance to move ONTO the target rather than adjacent to it."""
        return self._closest_impl(
            targets, start=self._coerce_start(pos), max_dist=None,
            avoid=avoid, side=side, to_adjacent=to_adjacent,
        )

    def closest_within(
        self,
        targets: int,
        pos=None,
        max_dist: int = 0,
        avoid: int = 0,
        side: bool = True,
        to_adjacent: bool = True,
    ) -> tuple[Position | None, int]:
        """Find the closest target if it is within `max_dist`, else (None, -1).

        `pos` accepts None, a Position, a bitmask int, or an iterable of
        Positions (see `closest`). `to_adjacent=False` measures distance to move
        ONTO the target rather than adjacent to it."""
        return self._closest_impl(
            targets, start=self._coerce_start(pos), max_dist=max_dist,
            avoid=avoid, side=side, to_adjacent=to_adjacent,
        )

    def __init__(self, c: Controller):
        self.width = c.get_map_width()
        self.height = c.get_map_height()
        self.rc = c

        w = self.width
        h = self.height

        # Precomputed perimeter (top + bottom rows | left + right cols), masked
        # to the board. Used in bfs_move; constant for the lifetime of the bot.
        nlc = map_info._not_left_col
        nrc = map_info._not_right_col
        board = map_info._board_mask
        row_mask = (1 << w) - 1
        self._board_border = (~nlc | ~nrc | row_mask | (row_mask << (w * (h - 1)))) & board

    def move(self, dir: Direction):
        rc = self.rc
        px, py = map_info._my_pos.x, map_info._my_pos.y
        dx, dy = map_info._DIRECTION_DELTAS[dir]
        new_pos = Position(px + dx, py + dy)
        if not map_info.in_bounds(new_pos):
            return False
        if rc.get_tile_builder_bot_id(new_pos) != None:
            return False
        if rc.can_move(dir) and has_op():
            rc.move(dir)
            map_info.update_move()
            self.last_last_dir = self.last_dir
            self.last_dir = map_info._DIRECTION_DELTAS[dir]
            return True
        return False
    # Route reconstruction offsets: 4 cardinals, all cost 1.
    _ROUTE_OFFSETS = (
        [(0, -1, 1), (0, 1, 1), (-1, 0, 1), (1, 0, 1)]
    )

    def bfs_move(self, start_n: int, target_mask: int, avoid: int, avoid_turret: bool = True,
                 hard_avoid_turret: bool = False, axis_priority=None):
        start_mask = 1 << start_n
        # if start_mask & target_mask:
        #     s_idx = (start_mask & target_mask).bit_length() - 1
        #     return Position(s_idx % self.width, s_idx // self.width), Position(s_idx % self.width, s_idx // self.width), 0
        width = self.width
        height = self.height
        # Hoist commonly-read map_info attrs into locals once.
        bm_et = map_info._bm_et
        bm_team = map_info._bm_team
        bm_friendly_bots = map_info._bm_friendly_bots
        bm_enemy_bots = map_info._bm_enemy_bots
        idx_barrier = map_info._IDX_BARRIER
        nlc = map_info._not_left_col
        nrc = map_info._not_right_col
        board = map_info._board_mask
        # Hoist module cost globals.
        bcost = barrier_cost
        tcost = threat_cost

        # Hard "would-die" block: never move INTO a tile where this bot's current
        # HP is <= the damage it would take there. Gunners and sentinels deal
        # separate per-hit damage (GUNNER_DAMAGE / SENTINEL_DAMAGE), tracked as
        # separate masks, so a tile covered by one gunner is 7, one sentinel 18,
        # both 25.
        die = map_info.lethal_mask(self.rc.get_hp())
        # Hard turret avoidance: treat every enemy-turret-threatened tile as
        # impassable (added to `avoid`), so the search will NEVER route through fire
        # no matter how much shorter that path is -- the soft tcost bucket only
        # detours around it. Our own tile stays passable (~start_mask below), so a
        # bot already in threat can still step out.
        # Our OWN barriers are walkable at a COST (the `bcost` bucket below routes around
        # them when a short detour exists, through them when there isn't). _compute_avoid's
        # blanket "all buildings" rule marks them impassable, so drop them out of `avoid`
        # -- but do it BEFORE folding in the lethal (`die`) and hard-turret avoidance, so a
        # barrier tile that is ALSO lethal / hard-avoided gets re-blocked and is never
        # walked onto. Only a barrier avoided SOLELY for being a barrier becomes passable.
        avoid = avoid & ~(bm_et[idx_barrier] & bm_team[map_info._my_team_idx])
        if hard_avoid_turret:
            avoid = avoid | map_info._bm_enemy_turret_threat
        avoid = (avoid | die) & ~start_mask
        builders_mask = (bm_friendly_bots | bm_enemy_bots) & ~start_mask

        # SOFT threat cost (bucket `tcost`): enemy-turret fire AND enemy-launcher
        # adjacency. A single hit is survivable, so these tiles stay passable -- but
        # we pay to enter them, so the search routes AROUND fire when the detour is
        # short and only walks through it when there's no cheaper way. (Without this
        # a bot happily strolls along a sentinel's whole firing line, eating 18/turn,
        # since no single hit is lethal.) It's always on; `avoid_turret` is vestigial.
        # Only a tile where we'd actually DIE this turn (die) forces "stay put" off
        # the table -- non-lethal threat never ejects an act-in-place builder, so it
        # can still hold a threatened tile to build/heal.
        threat = map_info._bm_enemy_turret_threat | map_info._bm_enemy_launch_adj
        # Standing in enemy TURRET threat ejects us at once -- but ONLY once we've
        # taken damage (below full HP). A full-HP builder can hold a threatened tile
        # to build/heal; a damaged one refuses to rest on it and is forced to step,
        # even if every step just leads back into threat. (Launcher-adjacency threat
        # still only adds cost below; it's turret fire a hurt builder won't sit under.)
        start_in_turret = (bool(map_info._bm_enemy_turret_threat & start_mask)
                           and self.rc.get_hp() < GameConstants.BUILDER_BOT_MAX_HP)
        # Never sit in a FRIENDLY gunner's line of fire: a builder on one of its ray
        # tiles is the first obstruction, so our own gunner's shot lands on us. Force
        # a step off it, unconditionally (no HP gate) -- exactly like the turret eject.
        start_in_gunner_lane = bool(map_info._bm_my_gunner_rays & start_mask)
        eject = start_in_turret or start_in_gunner_lane
        if eject:
            target_mask &= ~start_mask
        start_in_threat = bool(die & start_mask) or eject
        threat &= ~start_mask

        # Our own tile is normally a valid "stay" option so it can win a tie
        # against a step (prefer not moving when no move gets us closer) -- but
        # not while we stand in threat and have somewhere to step.
        movable = (map_info.expand_manhattan(start_mask)
                   & ~avoid & ~builders_mask & ~start_mask)
        if start_in_threat and movable:
            can_move_to = movable
        else:
            can_move_to = movable | start_mask
        my_team_idx = map_info._my_team_idx
        barriers = bm_et[idx_barrier] & bm_team[my_team_idx]
        barriers &= ~start_mask

        # Ejecting (turret threat or a friendly gunner lane) removed our own tile as a
        # target; if that left NO target at all, there's no real destination -- just
        # take the cheapest legal step (out of threat when we can), ties broken at
        # random. Cheapest first: clear, then threat-only (tcost 3), then barrier
        # (bcost 15), then both.
        if eject and target_mask == 0:
            start_pos = Position(start_n % width, start_n // width)
            if not movable:
                return start_pos, start_pos, 0
            for bucket in (movable & ~threat & ~barriers,
                           movable & threat & ~barriers,
                           movable & ~threat & barriers,
                           movable & threat & barriers):
                if bucket:
                    picks = []
                    bb = bucket
                    while bb:
                        b = bb & -bb
                        bb ^= b
                        picks.append(b.bit_length() - 1)
                    choice = random.choice(picks)
                    return start_pos, Position(choice % width, choice // width), 0
            return start_pos, start_pos, 0
        # builder.draw_mask(target_mask, 0, 255, 255)
        # builder.draw_mask(avoid, 255, 0, 255)

        # builder.draw_mask(barriers, 0, 0, 255)

        w = width
        not_avoid = board & ~avoid


        nb_nt  = board & ~barriers & ~threat
        b_nt   = board & barriers  & ~threat
        nb_t   = board & ~barriers & threat
        b_t    = board & barriers  & threat

        max_c = 1 + bcost + tcost
        max_seed = bcost + tcost
        cycle_len = max(max_c, max_seed) + 1
        frontier = [0] * cycle_len
        frontier[0]                                      = target_mask & nb_nt
        frontier[bcost]                                 |= target_mask & b_nt
        frontier[tcost]                                 |= target_mask & nb_t
        frontier[bcost + tcost]                         |= target_mask & b_t
        visited = 0
        i = 0
        stuck_turns = 0
        while True:
            slot = i % cycle_len
            cur_frontier = frontier[slot] & ~visited
            # builder.draw_mask(cur_frontier, (i*64)%256, 0, 0)
            visited |= cur_frontier
            if cur_frontier == 0:
                stuck_turns += 1
                i += 1
                if stuck_turns >= cycle_len:
                    log("bfs move miss")
                    return None
                continue
            else:
                stuck_turns = 0
            hit = cur_frontier & can_move_to
            if hit:
                cx = start_n % width
                cy = start_n // width
                start_pos = Position(cx, cy)
                # Prefer no move: the BFS reaches tiles in increasing cost order,
                # so if our own tile is in the first hit layer, staying is at
                # least as close to the target as any step — don't move.
                if hit & start_mask:
                    # Exception (swap-deadlock breaker): if a friendly builder
                    # sits on a strictly-closer tile — a step toward the target we
                    # can't take only because we can't move onto a bot — and we
                    # hold the LOWER id, yield instead of staying: force a move so
                    # the two of us don't both sit still forever. The higher-id
                    # builder stays put and takes the tile we vacate. Tiles reached
                    # before this layer are strictly lower cost (closer); equal-cost
                    # tiles don't count.
                    closer = visited & ~cur_frontier
                    sm = start_mask
                    neigh = (((sm & nlc) >> 1) | ((sm & nrc) << 1)
                             | (sm >> w) | (sm << w)) & board
                    blocking = neigh & closer & bm_friendly_bots
                    if not blocking:
                        return start_pos, start_pos, i
                    tcoords = []
                    tm2 = target_mask
                    while tm2:
                        tb = tm2 & -tm2
                        tm2 ^= tb
                        tnn = tb.bit_length() - 1
                        tcoords.append((tnn % w, tnn // w))
                    # Yield to the higher-id blocker nearest our target; its axis is
                    # the collision we want to step clear of.
                    my_id = self.rc.get_id()
                    force_move = False
                    collision_dx = 0
                    best_bd = None
                    bb = blocking
                    while bb:
                        b = bb & -bb
                        bb ^= b
                        bn = b.bit_length() - 1
                        bx, by = bn % width, bn // width
                        bid = self.rc.get_tile_builder_bot_id(Position(bx, by))
                        if bid is not None and my_id < bid:
                            force_move = True
                            d = min(abs(bx - tX) + abs(by - tY) for tX, tY in tcoords)
                            if best_bd is None or d < best_bd:
                                best_bd = d
                                collision_dx = bx - cx   # 0 => collision is vertical
                    if not force_move:
                        return start_pos, start_pos, i
                    avail = can_move_to & ~start_mask
                    if not avail:
                        return start_pos, start_pos, i     # boxed in — can't move
                    # Least negative progress first (nearest a target); tie-break to
                    # the side, i.e. perpendicular to the collision, so we clear the
                    # lane rather than shuffle along it.
                    collision_vertical = collision_dx == 0
                    best_b = None
                    best_key = None
                    ab = avail
                    while ab:
                        b = ab & -ab
                        ab ^= b
                        bn = b.bit_length() - 1
                        bx2, by2 = bn % w, bn // w
                        dist = min(abs(bx2 - tX) + abs(by2 - tY) for tX, tY in tcoords)
                        if collision_vertical:
                            perp = 0 if bx2 != cx else 1   # prefer a horizontal step
                        else:
                            perp = 0 if by2 != cy else 1   # prefer a vertical step
                        key = (dist, perp)
                        if best_key is None or key < best_key:
                            best_key = key
                            best_b = (bx2, by2)
                    return start_pos, Position(best_b[0], best_b[1]), i
                from_mask = hit
                single_target = target_mask.bit_count() == 1
                if single_target:
                    tn = target_mask.bit_length() - 1
                    tx = tn % w
                    ty = tn // w
                    cover_mask = 0
                    for dx, dy in _FULL_COVER_OFFSETS:
                        nx = tx + dx
                        ny = ty + dy
                        if 0 <= nx < width and 0 <= ny < height:
                            cover_mask |= 1 << (nx + ny * w)
                    all_covered = hit & cover_mask
                    if all_covered:
                        from_mask = all_covered
                    targets = ((tx, ty),)
                else:
                    targets = []
                    tm = target_mask
                    while tm:
                        tb = tm & -tm
                        tm ^= tb
                        tnn = tb.bit_length() - 1
                        targets.append((tnn % w, tnn // w))

                # Step 5 -- runs BEFORE the border/friendly-bot peeling below: keep only
                # the candidates minimizing (Chebyshev to nearest target, axis penalty).
                # The "get closer" rule DOMINATES the peeling, so a strictly-closer step
                # is never discarded just for being near the board edge or a teammate.
                # axis_priority is the secondary soft tiebreak (prefer that axis).
                def _cheb_key(n):
                    nx = n % width
                    ny = n // width
                    cheb = min(max(abs(nx - tx2), abs(ny - ty2)) for tx2, ty2 in targets)
                    if axis_priority == 'horizontal':
                        ap = 0 if nx != cx else 1
                    elif axis_priority == 'vertical':
                        ap = 0 if ny != cy else 1
                    else:
                        ap = 0
                    return (cheb, ap)
                best_key = None
                m2 = from_mask
                while m2:
                    b = m2 & -m2
                    m2 ^= b
                    k = _cheb_key(b.bit_length() - 1)
                    if best_key is None or k < best_key:
                        best_key = k
                keep = 0
                m2 = from_mask
                while m2:
                    b = m2 & -m2
                    m2 ^= b
                    if _cheb_key(b.bit_length() - 1) == best_key:
                        keep |= b
                from_mask = keep

                # Step 4 -- now only among the CLOSEST candidates: prefer interior tiles
                # away from the board edge / friendly bots (keep the last non-empty set).
                border = self._board_border | bm_friendly_bots
                last_working_mask = from_mask
                c = 0
                while from_mask and c <= 4:
                    c += 1
                    last_working_mask = from_mask
                    from_mask &= ~border
                    border = map_info.expand_manhattan(border)
                from_mask = last_working_mask

                # DEBUG: the candidates the final tiebreak chooses among -> blue dots.
                if DRAW_DEBUG:
                    _dbg = from_mask
                    while _dbg:
                        _db = _dbg & -_dbg
                        _dbg ^= _db
                        _dn = _db.bit_length() - 1
                        self.rc.draw_indicator_dot(Position(_dn % width, _dn // width), 0, 0, 255)

                # Any survivor of the closest-then-interior set (lowest tile index).
                pick = from_mask & -from_mask
                pn = pick.bit_length() - 1
                return start_pos, Position(pn % width, pn // width), i
            # 4-neighbour (cardinal) expansion — movement is cardinal-only
            f = cur_frontier
            expanded = f | ((f & nrc) << 1) | ((f & nlc) >> 1) | (f << w) | (f >> w)
            new = expanded & ~visited & not_avoid

            frontier[(i + 1) % cycle_len]                                        |= new & nb_nt
            frontier[(i + 1 + bcost) % cycle_len]                                |= new & b_nt
            frontier[(i + 1 + tcost) % cycle_len]                                |= new & nb_t
            frontier[(i + 1 + bcost + tcost) % cycle_len]                        |= new & b_t
            i += 1
            frontier[slot] = 0

    def _core_ward_key(self, pn: int, px: int, py: int):
        """Sort key (lower is better) for a candidate next-step tile `pn`, used to
        tie-break equal-length routes toward the core. Order:
          1. my core tiles first,
          2. then nearest to the core along the belt (map_info.conv_dist_core --
             the hop distance to core; tiles not connected to the core rank last),
          3. then Manhattan distance to the core's 2x2 footprint.
        """
        mi = map_info
        is_core = (mi._bm_my_core_area >> pn) & 1
        cd = mi.conv_dist_core
        d = cd[pn] if pn < len(cd) else -1
        if d < 0:
            d = 1 << 30                       # unconnected: rank behind any real hop count
        core = mi._my_core
        if core is None:
            man = 0
        else:
            cxr = core.x if px < core.x else (core.x + 1 if px > core.x + 1 else px)
            cyr = core.y if py < core.y else (core.y + 1 if py > core.y + 1 else py)
            man = abs(px - cxr) + abs(py - cyr)
        return (0 if is_core else 1, d, man)

    def bfs_route(self, start_mask: int, target_mask: int, avoid: int | None = None, end_cost_mask: int = 0):
        log("bfs route")
        if start_mask & target_mask:
            s_idx = (start_mask & target_mask).bit_length() - 1
            return Position(s_idx % self.width, s_idx // self.width), Position(s_idx % self.width, s_idx // self.width), 0
        width = self.width
        height = self.height
        if avoid is None:
            avoid = map_info.get_avoid(False)
        # builder.draw_mask(avoid, 255, 0, 0)

        # builder.draw_mask(target_mask, 0, 255, 255)
        avoid &= ~start_mask

        if end_cost_mask:
            t_end = target_mask & end_cost_mask
            t_core = target_mask & ~t_end
        else:
            convs = map_info._bm_conveyors & ~map_info._bm_my_core_area & map_info._bm_ti_carrying
            t_end = target_mask & convs
            t_core = target_mask & ~convs

        # A core-facing conveyor with nothing feeding it and no adjacent harvester
        # is an empty feed line into the core -- routing onto it just completes
        # that line, so it attaches for free (exempt from the conveyor end cost).
        exempt = t_end & map_info.end_cost_exempt_conveyors()
        if exempt:
            t_end &= ~exempt
            t_core |= exempt

        # Enemy barriers are traversable at BARRIER_ROUTE_COST (they were dropped from
        # `avoid`, so they are in `not_avoid`). Only widen the cost cycle / search depth
        # when some are actually in play, so the common (barrier-free) route keeps its
        # tight cost-1 behaviour and search depth.
        enemy_barriers = (map_info._bm_et[map_info._IDX_BARRIER]
                          & map_info._bm_team[1 - map_info._my_team_idx])
        max_c = BARRIER_ROUTE_COST if enemy_barriers else 1
        max_seed = conveyor_end_cost
        cycle_len = max(max_c, max_seed) + 1
        frontier = [0] * cycle_len
        frontier[0] = t_core
        frontier[conveyor_end_cost % cycle_len] |= t_end

        nlc = map_info._not_left_col
        nrc = map_info._not_right_col
        w = width
        board = map_info._board_mask
        not_avoid = board & ~avoid

        effective_len = max_seed + 1
        visited = 0
        visited_layers: list[int] = []
        i = 0
        while True:
            # log("route",i,file=sys.stderr)
            slot = i % cycle_len
            cur_frontier = frontier[slot] & ~visited
            frontier[slot] = 0
            visited_layers.append(cur_frontier)
            visited |= cur_frontier
            # rc, gc, bc = colorsys.hsv_to_rgb((i%8)/8, 1, 1)

            # builder.draw_mask(cur_frontier, int(rc*255), int(gc*255), int(bc*255))

            hit = cur_frontier & start_mask
            if hit:
                vl_len = len(visited_layers)
                # All start tiles in `hit` were reached at the same (minimal) cost
                # `i`, and each may offer several equal-cost next steps toward the
                # network. Enumerate every (start, next-step) pair and keep the one
                # whose next step is most core-ward (see _core_ward_key), so the
                # route commits toward the core rather than an arbitrary branch.
                best_key = None
                best_start = None
                best_prev = None
                h = hit
                while h:
                    start_bit = h & -h
                    h ^= start_bit
                    s_idx = start_bit.bit_length() - 1
                    cx = s_idx % width
                    cy = s_idx // width
                    # If this start tile is an enemy barrier, the step that LANDED on it
                    # cost BARRIER_ROUTE_COST, so its predecessor sits that many layers
                    # back (not one).
                    start_is_barrier = (enemy_barriers >> s_idx) & 1
                    for dx, dy, step_cost in self._ROUTE_OFFSETS:
                        px = cx - dx
                        py = cy - dy
                        if not (0 <= px < width and 0 <= py < height):
                            continue
                        prev_layer = i - (BARRIER_ROUTE_COST if start_is_barrier else step_cost)
                        if prev_layer < 0 or prev_layer >= vl_len:
                            continue
                        pn = py * width + px
                        if not (visited_layers[prev_layer] >> pn) & 1:
                            continue
                        key = self._core_ward_key(pn, px, py)
                        if best_key is None or key < best_key:
                            best_key = key
                            best_start = Position(cx, cy)
                            best_prev = Position(px, py)
                if best_prev is None:
                    return None
                return (best_start, best_prev, i)

            if cur_frontier == 0:
                i += 1
                if i >= effective_len:
                    return None
                continue

            if i + max_c + 1 > effective_len:
                effective_len = i + max_c + 1

            f = cur_frontier
            # Cardinals (cost 1) — unrolled, avoid filter at end
            new_card = (
                ((f & nrc) << 1)
                | ((f & nlc) >> 1)
                | (f << w)
                | (f >> w)
            ) & not_avoid
            # Enemy-barrier tiles cost BARRIER_ROUTE_COST to step onto; everything else
            # costs 1. (nb is 0 whenever there are no enemy barriers -> no extra work.)
            nb = new_card & enemy_barriers
            if nb:
                frontier[(i + 1) % cycle_len] |= new_card & ~nb
                frontier[(i + BARRIER_ROUTE_COST) % cycle_len] |= nb
            else:
                frontier[(i + 1) % cycle_len] |= new_card
            i += 1
    def move_adjacent(self, pos: Position, fallback: Position | None = None,
                      allow_bots: bool = False, can_move: bool = True, **kwargs):
        """Move to a CARDINAL neighbour of pos. Titan build/destroy/heal reach is
        cardinal-only (dist^2 == 1), so a diagonal neighbour is useless — the
        follow-up action would just fail. Filters by in_bounds, passable, no
        builder bot, and in vision.

        allow_bots=True keeps neighbour tiles that currently hold a (mobile)
        builder bot as valid approach targets: they clear on their own, so a
        committed job -- e.g. building an assigned conveyor plan -- should path
        toward the tile and slot in when it frees rather than give up because
        friendly bots are momentarily in the way.

        can_move=False is the in-place mode used by the free-action retry: never
        step. Returns like a move that "did nothing worth acting on" -- False
        (i.e. "act now") only when we're already cardinally adjacent to pos, True
        ("bail, can't act from here") otherwise. Keeps the caller's usual
        `if move_adjacent(...): return` shape working unchanged."""
        log("move adjacent", pos)
        if not can_move:
            my = map_info._my_pos
            return abs(my.x - pos.x) + abs(my.y - pos.y) != 1
        rc = self.rc
        adj = set()
        for d in CARD_DIR:
            p = map_info.pos_add(pos, d)
            if not map_info.in_bounds(p):
                continue
            if p == map_info._my_pos:
                adj.add(p)
                continue
            if not map_info.is_passable(p):
                continue
            if not allow_bots and rc.is_in_vision(p) and rc.get_tile_builder_bot_id(p):
                continue
            adj.add(p)
        if not adj:
            if fallback is not None:
                adj.add(fallback)
            else:
                adj.add(pos)
        return self.move_to(adj, **kwargs)

    def move_to(self, target: Position | set[Position], avoid_turret: bool = True,
                can_move: bool = True, hard_avoid_turret: bool = False, axis_priority=None):
        if not can_move:
            # In-place mode: never step. "act now" (False) only if we're already
            # standing on a target tile, else "bail" (True).
            my = map_info._my_pos
            if isinstance(target, Position):
                return my != target
            return my not in target
        log("move to", target)
        if isinstance(target, Position):
            target_set = {target}
        else:
            target_set = target
        avoid = map_info.get_avoid(False)

        my_pos = map_info._my_pos
        targets_not_adjacent = True
        if my_pos in target_set:
            targets_not_adjacent = False
        else:
            my_x = my_pos.x
            my_y = my_pos.y
            for t in target_set:
                if abs(my_x - t.x) + abs(my_y - t.y) <= 1:
                    targets_not_adjacent = False
                    break
        if target_set == self.target_p and my_pos == self.prev_pos and targets_not_adjacent:
            self.stuck_turns += 1
        else:
            self.prev_pos = my_pos
            self.stuck_turns = 0
            self.target_p = target_set
        if self.stuck_turns > 2 + self.rc.get_id() % 8:
            # Any direction that unsticks us is as good as any other -- shuffle.
            for d in random.sample(CARD_DIR, len(CARD_DIR)):
                if self.move(d):
                    return True

        w = self.width
        target_mask = 0
        for t in target_set:
            target_mask |= 1 << (t.x + t.y * w)
        result = self.bfs_move(my_pos.x + my_pos.y * w, target_mask, avoid,
                               avoid_turret=avoid_turret, hard_avoid_turret=hard_avoid_turret,
                               axis_priority=axis_priority)
        if result is None:
            return False
        s_pos, p_pos, _ = result
        if s_pos == p_pos:
            return False
        if DRAW_DEBUG:
            self.rc.draw_indicator_line(s_pos, p_pos, 0, 255, 255)
        return self.move(map_info.direction_to(s_pos, p_pos))

    def _calculate_conveyor_path_to(
        self,
        start: Position,
        target: int,
        avoid: int,
        update: bool,
        end_cost_mask: int = 0,
    ):
        w = self.width
        if not target:
            log("no target")
            return None
        if not update:
            start_mask = 0
            for d in CARD_DIR:
                sp = map_info.pos_add(start, d)
                if map_info.in_bounds(sp) and ((avoid >> (sp.x + sp.y * w)) & 1) == 0:
                    start_mask |= 1 << (sp.x + sp.y * w)
            if start_mask == 0:
                log("no start")
                return None
        else:
            start_mask = 1 << (start.x + start.y * w)
    
        result = self.bfs_route(start_mask, target, avoid, end_cost_mask=end_cost_mask)
        if result is None:
            return None
        
        if DRAW_DEBUG:
            s_pos, p_pos, _ = result
            # self.rc.draw_indicator_line(s_pos, p_pos, 255, 0, 255)
            # self.rc.draw_indicator_dot(s_pos, 255, 0, 255)

        return result

    def calculate_conveyor_path(self, start: Position, update: bool = False,
                                end_cost_mask: int = 0):
        # `end_cost_mask` picks which targets pay the conveyor_end_cost penalty.
        # 0 (default) => bfs_route's own rule: only *loaded* conveyor ends are
        # penalised, core + unloaded ends seed at 0. Passing all conveyors makes
        # ONLY the core seed at 0, so every conveyor attach is penalised equally
        # and raw distance decides between them (route uses this so a nearer
        # loaded attach isn't tied with a farther unloaded one).
        log("conveyors from ", start)
        target, avoid = self._get_conveyor_targets_and_avoid()
        return self._calculate_conveyor_path_to(
            start,
            target,
            avoid,
            update,
            end_cost_mask=end_cost_mask,
        )

    def conveyor_cost(self, dist, scaling=None):
        if scaling is None:
            scaling = self.rc.get_scale_percent() / 100
        if dist is None or dist < 0:
            return None
        # Arithmetic-series equivalent of:
        #   sum(3 * (scaling + 0.01 * k) for k in range(dist))
        return (3 * dist * scaling + 0.015 * dist * (dist - 1)) * CONVEYOR_COST_DISCOUNT

    def _get_conveyor_targets_and_avoid(
        self
    ):
        avoid = map_info.get_avoid(True)
        target = map_info._bm_route_targets
        if not target:
            return 0, 0
        return target, avoid