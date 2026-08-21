from main import has_op
import map_info
from fcode import Controller, Direction, Position, EntityType, GameConstants
import units.builder as builder
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


# Offsets (dx, dy) such that lsb_pos = target_pos + (dx, dy) covers all 9
# tiles of the 3x3 around target_pos within d^2 <= 20. Worst-case corner is
# (target + (sign(dx), sign(dy))), so the predicate is
# (|dx|+1)^2 + (|dy|+1)^2 <= 20. Constant set, precomputed once.
_FULL_COVER_OFFSETS = [
    (dx, dy)
    for dy in range(-3, 4) for dx in range(-3, 4)
    if (abs(dx) + 1) ** 2 + (abs(dy) + 1) ** 2 <= 20
]



_base_claim_cache_key = None
_base_claim_cache_value = (0, 0)

def voronoi_claim(my_mask, others_mask, claims, passable=None):
    if not claims:
        return 0
    if not others_mask:
        return claims
    if passable is None:
        passable = map_info.passable()

    my_front = my_mask
    other_front = others_mask
    my_claims = 0
    all_claimed = my_front | other_front

    # Inlined expand_manhattan (4-neighbour — movement is cardinal-only)
    w = map_info._width
    nlc = map_info._not_left_col
    nrc = map_info._not_right_col
    while claims and (my_front or other_front):
        if my_front:
            my_claims |= map_info.manhattan(my_front) & claims
            claims &= ~my_claims
            my_expand = ((my_front | ((my_front & nrc) << 1) | ((my_front & nlc) >> 1) | (my_front << w) | (my_front >> w))) & passable & ~all_claimed
            all_claimed |= my_expand
            my_front = my_expand
        if not claims:
            break
        if other_front:
            claims &= ~(map_info.manhattan(other_front) & claims)
            other_expand = ((other_front | ((other_front & nrc) << 1) | ((other_front & nlc) >> 1) | (other_front << w) | (other_front >> w))) & passable & ~all_claimed
            all_claimed |= other_expand
            other_front = other_expand

    return my_claims


def claim_subset(
    my_mask: int,
    others_mask: int,
    claims: int,
    passable: int | None = None,
    tie_self: bool = True,
) -> int:
    """Exact wrapper around `voronoi_claim` with a cached fast path.

    If all claims are already passable on the base graph, we can reuse a shared
    territorial partition for the current builder turn. If any blocked claim is
    present, we fall back to the original exact computation because blocked
    claims become traversable corridors in `voronoi_claim`.
    """
    if not claims:
        return 0
    if passable is None:
        passable = map_info.passable()
    if tie_self:
        return voronoi_claim(my_mask, others_mask, claims, passable)
    return claims & ~voronoi_claim(others_mask, my_mask, claims, passable) & ~others_mask

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

    def bfs_move(self, start_n: int, target_mask: int, avoid: int, avoid_turret: bool = True):
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
        start_in_threat = bool(die & start_mask)
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
                border = self._board_border | bm_friendly_bots
                last_working_mask = from_mask
                c = 0
                while from_mask and c <= 4:
                    c += 1
                    last_working_mask = from_mask
                    from_mask &= ~border
                    border = map_info.expand_manhattan(border)
                from_mask = last_working_mask
                # Tiebreak among equal-cost first steps: pick the tile whose
                # Chebyshev distance to the (nearest) target is smallest.
                if single_target:
                    targets = ((tx, ty),)
                else:
                    targets = []
                    tm = target_mask
                    while tm:
                        tb = tm & -tm
                        tm ^= tb
                        tnn = tb.bit_length() - 1
                        targets.append((tnn % w, tnn // w))
                best_dir = None
                best_cheb = None
                while from_mask:
                    check_bit = from_mask & -from_mask
                    from_mask ^= check_bit
                    n = check_bit.bit_length() - 1
                    nx = n % width
                    ny = n // width
                    cheb = min(max(abs(nx - tx2), abs(ny - ty2)) for tx2, ty2 in targets)
                    if best_cheb is None or cheb < best_cheb:
                        best_cheb = cheb
                        best_dir = (nx - cx, ny - cy)

                return start_pos, Position(cx + best_dir[0], cy + best_dir[1]), i
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

        max_c = 1
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
                    for dx, dy, step_cost in self._ROUTE_OFFSETS:
                        px = cx - dx
                        py = cy - dy
                        if not (0 <= px < width and 0 <= py < height):
                            continue
                        prev_layer = i - step_cost
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
                can_move: bool = True):
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
            for d in CARD_DIR:
                if self.move(d):
                    return True

        w = self.width
        target_mask = 0
        for t in target_set:
            target_mask |= 1 << (t.x + t.y * w)
        result = self.bfs_move(my_pos.x + my_pos.y * w, target_mask, avoid, avoid_turret=avoid_turret)
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