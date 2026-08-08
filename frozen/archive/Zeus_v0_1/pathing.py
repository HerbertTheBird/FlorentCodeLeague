import map_info
import random
from fcode import Controller, Direction, Position, EntityType
import units.builder as builder
from log import DRAW_DEBUG, log
from _config import CONVEYOR_COST_DISCOUNT

ALL_DIRS = list(Direction)

CARD_DIR = [
    Direction.NORTH,
    Direction.SOUTH,
    Direction.EAST,
    Direction.WEST,
]
barrier_cost = 15
threat_cost = 20

def _mask_reference(mask: int, width: int) -> tuple[int, int]:
    """A representative (x, y) for a target bitmask — the single tile if the mask
    has one bit, else its centroid. Only used to weight the horizontal-vs-vertical
    move choice, so an approximate centroid is fine."""
    if mask == 0:
        return (0, 0)
    if mask & (mask - 1) == 0:  # single bit set
        n = mask.bit_length() - 1
        return (n % width, n // width)
    sx = sy = cnt = 0
    m = mask
    while m and cnt < 128:
        b = m & -m
        m ^= b
        n = b.bit_length() - 1
        sx += n % width
        sy += n // width
        cnt += 1
    return (sx // cnt, sy // cnt)


# Offsets (dx, dy) such that lsb_pos = target_pos + (dx, dy) covers all 9
# tiles of the 3x3 around target_pos within d^2 <= 20. Worst-case corner is
# (target + (sign(dx), sign(dy))), so the predicate is
# (|dx|+1)^2 + (|dy|+1)^2 <= 20. Constant set, precomputed once.
_FULL_COVER_OFFSETS = [
    (dx, dy)
    for dy in range(-3, 4) for dx in range(-3, 4)
    if (abs(dx) + 1) ** 2 + (abs(dy) + 1) ** 2 <= 20
]



destroyed_barriers = dict()

_base_claim_cache_key = None
_base_claim_cache_value = (0, 0)

def rebuild_broken_barriers(rc: Controller):
    if not destroyed_barriers:
        return
    if  rc.get_global_resources() < rc.get_barrier_cost() + map_info.builder_ti_reserve():
        return
    if rc.get_action_cooldown() > 0:
        return

    my_pos = map_info._my_pos
    my_team = map_info._my_team
    current_round = rc.get_current_round()
    
    rebuilt_pos = None
    
    for p in destroyed_barriers:
        if p == my_pos:
            continue
        if my_pos.distance_squared(p) > 1:  # Titan: build reach is cardinal-only
            continue
        if destroyed_barriers[p]+1 > current_round:
            continue
        if rc.can_build_barrier(p) and rc.get_global_resources() >= rc.get_barrier_cost() + map_info.builder_ti_reserve():
            rc.build_barrier(p)
            map_info.update_at(p)
            rebuilt_pos = p
            break
    if rebuilt_pos is not None:
        destroyed_barriers.pop(rebuilt_pos, None)
def voronoi_claim(my_mask, others_mask, claims, passable=None):
    if not claims:
        return 0
    if not others_mask:
        return claims
    if passable is None:
        passable = map_info._bm_passable_FFF
    passable |= claims

    my_front = my_mask & passable
    other_front = others_mask & passable
    my_claimed = my_front
    all_claimed = my_claimed | other_front
    remaining_claims = claims & ~all_claimed

    # Inlined expand_chebyshev — saves ~1us function-call overhead per expand,
    # and there can be many expands per call.
    w = map_info._width
    nlc = map_info._not_left_col
    nrc = map_info._not_right_col
    board = map_info._board_mask

    while remaining_claims and (my_front or other_front):
        if my_front:
            my_expand = ((((my_front & nrc) << 1) | ((my_front & nlc) >> 1) | (my_front << w) | (my_front >> w)) & board) & passable & ~all_claimed
            my_claimed |= my_expand
            all_claimed |= my_expand
            remaining_claims &= ~my_expand
            my_front = my_expand
        if not remaining_claims:
            break
        if other_front:
            other_expand = ((((other_front & nrc) << 1) | ((other_front & nlc) >> 1) | (other_front << w) | (other_front >> w)) & board) & passable & ~all_claimed
            all_claimed |= other_expand
            remaining_claims &= ~other_expand
            other_front = other_expand

    return my_claimed & claims


def _claim_zone_on_passable(my_mask: int, others_mask: int, passable: int, self_first: bool) -> int:
    """Ownership zone over an already-passable graph.

    This is exact for claims wholly contained in `passable`. It intentionally
    does not try to handle blocked claim tiles, because in `voronoi_claim`
    those tiles become traversable and can act as corridors.
    """
    if not passable:
        return 0
    if not others_mask:
        return passable

    w = map_info._width
    nlc = map_info._not_left_col
    nrc = map_info._not_right_col
    board = map_info._board_mask

    my_front = my_mask & passable
    other_front = others_mask & passable
    my_claimed = my_front
    all_claimed = my_front | other_front
    remaining = passable & ~all_claimed

    while remaining and (my_front or other_front):
        if self_first:
            first_is_self = True
        else:
            first_is_self = False

        if first_is_self:
            if my_front:
                my_expand = ((((my_front & nrc) << 1) | ((my_front & nlc) >> 1) | (my_front << w) | (my_front >> w)) & board) & passable & ~all_claimed
                my_claimed |= my_expand
                all_claimed |= my_expand
                remaining &= ~my_expand
                my_front = my_expand
            if not remaining:
                break
            if other_front:
                other_expand = ((((other_front & nrc) << 1) | ((other_front & nlc) >> 1) | (other_front << w) | (other_front >> w)) & board) & passable & ~all_claimed
                all_claimed |= other_expand
                remaining &= ~other_expand
                other_front = other_expand
        else:
            if other_front:
                other_expand = ((((other_front & nrc) << 1) | ((other_front & nlc) >> 1) | (other_front << w) | (other_front >> w)) & board) & passable & ~all_claimed
                all_claimed |= other_expand
                remaining &= ~other_expand
                other_front = other_expand
            if not remaining:
                break
            if my_front:
                my_expand = ((((my_front & nrc) << 1) | ((my_front & nlc) >> 1) | (my_front << w) | (my_front >> w)) & board) & passable & ~all_claimed
                my_claimed |= my_expand
                all_claimed |= my_expand
                remaining &= ~my_expand
                my_front = my_expand

    return my_claimed


def _get_base_claim_zones(my_mask: int, others_mask: int, passable: int) -> tuple[int, int]:
    """Return (self_wins_ties_self_zone, others_win_ties_other_zone)."""
    global _base_claim_cache_key, _base_claim_cache_value

    key = (my_mask, others_mask, passable)
    if key == _base_claim_cache_key:
        return _base_claim_cache_value

    if not passable:
        result = (0, 0)
    elif not others_mask:
        result = (passable, 0)
    else:
        result = (
            _claim_zone_on_passable(my_mask, others_mask, passable, self_first=True),
            _claim_zone_on_passable(others_mask, my_mask, passable, self_first=True),
        )

    _base_claim_cache_key = key
    _base_claim_cache_value = result
    return result


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
        passable = map_info._bm_passable_FFF
    if claims & ~passable:
        if tie_self:
            return voronoi_claim(my_mask, others_mask, claims, passable)
        return claims & ~voronoi_claim(others_mask, my_mask, claims, passable) & ~others_mask

    tie_me_zone, others_first_other_zone = _get_base_claim_zones(my_mask, others_mask, passable)
    if tie_self:
        return claims & tie_me_zone
    return claims & ~others_first_other_zone & ~others_mask

class Pathing:


    forget_launcher = set()
    width = height = 0
    rc: Controller

    stuck_turns = 0
    prev_pos = None

    target_p = None

    last_dir = None
    last_last_dir = None




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
    ) -> tuple[Position | None, int]:
        """Shared bitmask BFS for closest-target queries.

        BFS expands outward from any bit set in `start` simultaneously and
        returns the first target reached by Chebyshev distance, breaking ties
        by lowest tile index. Distance is the BFS layer count from the nearest
        start tile. When `max_dist` is provided, the search stops after
        exploring that many layers.

        `avoid` is an optional bitmask of tiles to additionally treat as
        impassable (e.g. enemy can't path through tiles next to our launchers).

        `side` selects whose perspective the passable mask reflects. True (the
        default) is our perspective — uses the cached `_bm_passable_FFF` which
        avoids enemy threat. False models the enemy's pathing: same blockers
        minus the enemy's own threat (they wouldn't avoid it).
        """
        if targets == 0 or start == 0:
            return None, -1
        w = map_info._width
        if side:
            passable = map_info._bm_passable_FFF
        else:
            passable = (
                ~map_info.get_avoid(False, False, False, enemy_pov=not side)
                & map_info._board_mask
            )
        if avoid:
            passable &= ~avoid
        if side:
            passable |= targets
        visited = start
        frontier = start
        dist = 0
        nlc = map_info._not_left_col
        nrc = map_info._not_right_col
        # Titan: a target on the tile we're standing on counts as DISTANCE 2 (you
        # can't build/act on your own tile — you must step off first), so adjacent
        # targets (distance 1) are preferred over our own tile. Everything else
        # unchanged.
        deferred_self = targets & start
        targets &= ~start

        def _self_result():
            lsb = deferred_self & -deferred_self
            n = lsb.bit_length() - 1
            return Position(n % w, n // w), 2

        while frontier:
            hit = frontier & targets
            if hit:
                lsb = hit & -hit
                n = lsb.bit_length() - 1
                return Position(n % w, n // w), dist
            if deferred_self and dist >= 2:
                return _self_result()  # nothing at distance <= 2 beat our own tile
            if max_dist is not None and dist >= max_dist:
                break
            visited |= frontier
            dist += 1
            frontier = ((((frontier & nrc) << 1) | ((frontier & nlc) >> 1) | (frontier << w) | (frontier >> w))) & passable & ~visited  # Titan: cardinal movement
        if deferred_self and (max_dist is None or max_dist >= 2):
            return _self_result()
        return None, -1

    def closest(
        self,
        targets: int,
        pos=None,
        avoid: int = 0,
        side: bool = True,
    ) -> tuple[Position | None, int]:
        """Find closest bit in *targets* from *pos* with full search.

        `pos` accepts None (defaults to my_pos), a single Position, a bitmask
        int, or any iterable of Positions — BFS expands from all start tiles
        simultaneously and returns the closest target."""
        return self._closest_impl(
            targets, start=self._coerce_start(pos), max_dist=None,
            avoid=avoid, side=side,
        )

    def closest_within(
        self,
        targets: int,
        pos=None,
        max_dist: int = 0,
        avoid: int = 0,
        side: bool = True,
    ) -> tuple[Position | None, int]:
        """Find the closest target if it is within `max_dist`, else (None, -1).

        `pos` accepts None, a Position, a bitmask int, or an iterable of
        Positions (see `closest`)."""
        return self._closest_impl(
            targets, start=self._coerce_start(pos), max_dist=max_dist,
            avoid=avoid, side=side,
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
        id = rc.get_tile_building_id(new_pos)
        if id and rc.get_entity_type(id) == EntityType.BARRIER and rc.can_destroy(new_pos) and rc.get_action_cooldown() == 0:
            rc.destroy(new_pos)
            map_info.update_at(new_pos)
            destroyed_barriers[new_pos] = rc.get_current_round()
        if rc.can_move(dir):
            rc.move(dir)
            map_info.update_move()
            self.last_last_dir = self.last_dir
            self.last_dir = map_info._DIRECTION_DELTAS[dir]
            return True
        return False
    # Route reconstruction offsets: (dx, dy, step_cost), cardinal-only.
    _ROUTE_OFFSETS = (
        (0, -1, 1), (0, 1, 1), (-1, 0, 1), (1, 0, 1),
    )

    def bfs_move(self, start_n: int, target_mask: int, avoid: int, avoid_turret: bool = True):
        start_mask = 1 << start_n
        # In the sights of a gunner (enemy OR our own): we must move off the ray
        # this turn — never stand still, and dodge even off the target tile or
        # when the target is unreachable.
        on_gunner_ray = bool(
            start_mask
            & (map_info._bm_enemy_hard_threat | map_info._bm_my_gunner_claims)
        )
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
        bm_conveyors = map_info._bm_conveyors
        bm_my_core_area = map_info._bm_my_core_area
        nlc = map_info._not_left_col
        nrc = map_info._not_right_col
        board = map_info._board_mask
        # Hoist module cost globals.
        bcost = barrier_cost
        tcost = threat_cost

        avoid &= ~start_mask
        builders_mask = (bm_friendly_bots | bm_enemy_bots) & ~start_mask
        can_move_to = map_info.expand_manhattan(start_mask) & ~avoid & ~builders_mask

        my_team_idx = map_info._my_team_idx
        barriers = bm_et[idx_barrier] & bm_team[my_team_idx]
        barriers &= ~start_mask
        threat = map_info._bm_enemy_launch_adj
        if avoid_turret:
            threat |= map_info._bm_enemy_turret_threat
        threat &= ~start_mask
        # builder.draw_mask(target_mask, 0, 255, 255)
        # builder.draw_mask(avoid, 255, 0, 255)

        # builder.draw_mask(barriers, 0, 0, 255)


        w = width
        not_avoid = board & ~avoid

        def _free_cardinal():
            """Passable cardinal neighbours we may step to (off avoid — which
            includes gunner rays — and off other builders)."""
            cx0 = start_n % width
            cy0 = start_n // width
            out = []
            for ddx, ddy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                nx, ny = cx0 + ddx, cy0 + ddy
                if not (0 <= nx < width and 0 <= ny < height):
                    continue
                nb = 1 << (nx + ny * width)
                if nb & (avoid | builders_mask):
                    continue
                if not map_info.is_passable(Position(nx, ny)):
                    continue
                out.append(Position(nx, ny))
            return out


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
                    if on_gunner_ray:
                        # Can't reach the target, but we're being shot — step off
                        # the ray anyway rather than stand and take fire.
                        dodges = _free_cardinal()
                        if dodges:
                            return Position(start_n % width, start_n // width), random.choice(dodges), 0
                    return None
                continue
            else:
                stuck_turns = 0
            hit = cur_frontier & can_move_to
            if hit:
                # can_move_to includes our own tile. Prefer standing still over a
                # same-distance sidestep: if our own tile is in this
                # first-reachable layer, every move here is the same distance to
                # the target, so moving wouldn't get us any closer — stay put.
                # (move_to's stuck-escape still frees us if we wait too long.) We
                # only sidestep/dodge when there is no strictly-closer step.
                non_self = hit & ~start_mask
                if (start_mask & hit) and non_self and not on_gunner_ray:
                    # Same-distance sidestep and we're not in a gunner's sights:
                    # stay put. (If we ARE on a ray we fall through and move, so
                    # we step off the line of fire.)
                    cx = start_n % width
                    cy = start_n // width
                    start_pos = Position(cx, cy)
                    return start_pos, start_pos, i
                if non_self:
                    hit = non_self
                elif not (start_mask & target_mask) or on_gunner_ray:
                    # Own tile is the ONLY hit and either we are not on the target
                    # or we are on a gunner's ray. Every strictly-closer neighbour
                    # is builder-blocked; under cardinal movement a sidestep is
                    # always farther, so greedy search would stand here forever
                    # (two builders meeting head-on deadlock; a builder in a
                    # gunner's line of fire would sit and get shot). Break it by
                    # dodging to a random free cardinal tile (which, since avoid
                    # includes gunner rays, is always off the line of fire).
                    cx = start_n % width
                    cy = start_n // width
                    dodges = _free_cardinal()
                    if dodges:
                        start_pos = Position(cx, cy)
                        return start_pos, random.choice(dodges), i
                cx = start_n % width
                cy = start_n // width
                start_pos = Position(cx, cy)
                from_mask = hit
                if target_mask.bit_count() == 1:
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
                # TITAN: empty tiles are directly walkable (Cambridge required a
                # road, so it preferred already-walkable tiles to save the build
                # action). In Titan stepping onto empty costs no action, so there
                # is no reason to prefer road/conveyor tiles — take the direct path.
                # spawn ring of our 2x2 core: standing there blocks spawns
                border = self._board_border | bm_friendly_bots | map_info.expand_manhattan(bm_my_core_area)
                last_working_mask = from_mask
                c = 0
                while from_mask and c <= 4:
                    c += 1
                    last_working_mask = from_mask
                    from_mask &= ~border
                    border = map_info.expand_manhattan(border)
                from_mask = last_working_mask
                # Titan: cardinal-only moves. The candidates in from_mask are all on a
                # shortest path; when both a horizontal and a vertical step qualify, pick
                # one at random weighted by the remaining x/y gap to the target — this
                # closes the larger gap more often, giving a straight (Bresenham-like)
                # path. (Khaos zig-zagged here to maximise vision; we don't want that.)
                horiz = None
                vert = None
                m = from_mask
                while m:
                    check_bit = m & -m
                    m ^= check_bit
                    n = check_bit.bit_length() - 1
                    dx = n % width - cx
                    dy = n // width - cy
                    if dy == 0:
                        horiz = (dx, dy)
                    elif dx == 0:
                        vert = (dx, dy)
                if horiz is not None and vert is not None:
                    tx, ty = _mask_reference(target_mask, width)
                    xgap = abs(tx - cx)
                    ygap = abs(ty - cy)
                    total = xgap + ygap
                    best_dir = horiz if (total == 0 or random.random() * total < xgap) else vert
                elif horiz is not None:
                    best_dir = horiz
                elif vert is not None:
                    best_dir = vert
                else:
                    # No pure-cardinal candidate (shouldn't happen with Manhattan search);
                    # fall back to the first available neighbour.
                    fb = from_mask & -from_mask
                    n = fb.bit_length() - 1
                    best_dir = (n % width - cx, n // width - cy)

                return start_pos, Position(cx+best_dir[0], cy+best_dir[1]), i
            # Manhattan (4-directional) expansion — Titan builders move cardinally,
            # so the movement graph is 4-connected (no diagonal steps).
            f = cur_frontier
            expanded = ((f & nrc) << 1) | ((f & nlc) >> 1) | (f << w) | (f >> w)
            new = expanded & ~visited & not_avoid

            frontier[(i + 1) % cycle_len]                                        |= new & nb_nt
            frontier[(i + 1 + bcost) % cycle_len]                                |= new & b_nt
            frontier[(i + 1 + tcost) % cycle_len]                                |= new & nb_t
            frontier[(i + 1 + bcost + tcost) % cycle_len]                        |= new & b_t
            i += 1
            frontier[slot] = 0

    def bfs_route(self, start_mask: int, target_mask: int, avoid: int | None = None):
        log("bfs route")
        if start_mask & target_mask:
            s_idx = (start_mask & target_mask).bit_length() - 1
            return Position(s_idx % self.width, s_idx // self.width), Position(s_idx % self.width, s_idx // self.width), 0
        width = self.width
        height = self.height
        if avoid is None:
            avoid = map_info.get_avoid(False, True, False)
        # builder.draw_mask(avoid, 255, 0, 0)

        # builder.draw_mask(target_mask, 0, 255, 255)
        avoid &= ~start_mask

        cycle_len = 2
        frontier = [target_mask, 0]

        nlc = map_info._not_left_col
        nrc = map_info._not_right_col
        w = width
        board = map_info._board_mask
        not_avoid = board & ~avoid

        effective_len = 1
        visited = 0
        visited_layers: list[int] = []
        i = 0
        while True:
            slot = i % cycle_len
            cur_frontier = frontier[slot] & ~visited
            frontier[slot] = 0
            visited_layers.append(cur_frontier)
            visited |= cur_frontier
            # rc, gc, bc = colorsys.hsv_to_rgb((i%8)/8, 1, 1)

            # builder.draw_mask(cur_frontier, int(rc*255), int(gc*255), int(bc*255))

            hit = cur_frontier & start_mask
            if hit:
                start_bit = hit & -hit
                s_idx = start_bit.bit_length() - 1
                cx = s_idx % width
                cy = s_idx // width
                start_pos = Position(cx, cy)
                vl_len = len(visited_layers)

                chosen_prev = None
                for dx, dy, step_cost in self._ROUTE_OFFSETS:
                    px = cx - dx
                    py = cy - dy
                    if not (0 <= px < width and 0 <= py < height):
                        continue
                    prev_layer = i - step_cost
                    if prev_layer < 0 or prev_layer >= vl_len:
                        continue
                    prev_bit = 1 << (py * width + px)
                    if visited_layers[prev_layer] & prev_bit:
                        chosen_prev = Position(px, py)
                        break
                if chosen_prev is None:
                    return None
                return (start_pos, chosen_prev, i)

            if cur_frontier == 0:
                i += 1
                if i >= effective_len:
                    return None
                continue

            if i + 2 > effective_len:
                effective_len = i + 2

            f = cur_frontier
            # Cardinals (cost 1) — unrolled, avoid filter at end
            new_card = (
                ((f & nrc) << 1)
                | ((f & nlc) >> 1)
                | (f << w)
                | (f >> w)
            ) & not_avoid
            frontier[(i + 1) % cycle_len] |= new_card
            # (Loki: bridge expansion removed — no bridges in Titan.)
            i += 1
    def move_adjacent(self, pos: Position, fallback: Position | None = None, **kwargs):
        """Move to a CARDINAL neighbour of pos. Titan 2.3.x actions (build/destroy)
        only reach cardinally adjacent tiles (radius² 1, diagonals rejected), so
        standing diagonal to a build site is useless. Filters by in_bounds,
        passable, no builder bot, and in vision."""
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
            if rc.is_in_vision(p) and rc.get_tile_builder_bot_id(p):
                continue
            adj.add(p)
        if not adj:
            if fallback is not None:
                adj.add(fallback)
            else:
                adj.add(pos)
        return self.move_to(adj, **kwargs)

    def move_to(
        self,
        target: Position | set[Position],
        avoid_turret: bool = True,
        allow_enemy_gunner: bool = False,
    ):
        log("move to", target)
        if isinstance(target, Position):
            target_set = {target}
        else:
            target_set = target
        if target_set != self.target_p:
            self.forget_launcher.clear()
        avoid = map_info.get_avoid(False, False, False)
        if allow_enemy_gunner:
            # Emergency core blockers sometimes must cross an enemy gunner ray
            # to reach the only cardinal stance from which they can interpose a
            # barrier. Preserve every other avoidance class, especially allied
            # gunner lanes, while permitting that deliberate risk.
            avoid &= ~map_info._bm_enemy_hard_threat
        # if avoid_empty:
        #     avoid |= map_info._bm_seen & ~map_info._bm_any_building & ~map_info._bm_env[map_info._IDX_ENV_WALL]
        my_pos = map_info._my_pos
        targets_not_adjacent = True
        if my_pos in target_set:
            targets_not_adjacent = False
        else:
            my_x = my_pos.x
            my_y = my_pos.y
            for t in target_set:
                if abs(my_x - t.x) + abs(my_y - t.y) <= 1:  # Titan: cardinal adjacency
                    targets_not_adjacent = False
                    break
        if target_set == self.target_p and my_pos == self.prev_pos and targets_not_adjacent:
            self.stuck_turns += 1
        else:
            self.prev_pos = my_pos
            self.stuck_turns = 0
            self.target_p = target_set
        if self.stuck_turns > 2 + self.rc.get_id() % 8:
            for d in ALL_DIRS:
                if self.rc.can_move(d):
                    self.rc.move(d)
                    map_info.update_move()
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

    def move_to_adjacent(self, target: Position, avoid_turret: bool = True):
        """Single-step move into the cheb-1 ring of target."""
        rc = self.rc
        my_pos = map_info._my_pos

        adj_set = set()
        for d in ALL_DIRS:
            if d == Direction.CENTRE:
                continue
            p = map_info.pos_add(target, d)
            if not map_info.in_bounds(p):
                continue
            if p == my_pos:
                return False
            if not map_info.is_passable(p):
                continue
            if rc.is_in_vision(p) and rc.get_tile_builder_bot_id(p):
                continue
            adj_set.add(p)
        if not adj_set:
            return False

        threat = map_info._bm_enemy_launch_adj
        if avoid_turret:
            threat |= map_info._bm_enemy_turret_threat

        w = self.width
        safe_dir = None
        risky_dir = None
        for d in ALL_DIRS:
            if d == Direction.CENTRE or not rc.can_move(d):
                continue
            step = map_info.pos_add(my_pos, d)
            if step not in adj_set:
                continue
            bit = 1 << (step.x + step.y * w)
            if bit & threat:
                if risky_dir is None:
                    risky_dir = d
            else:
                safe_dir = d
                break

        chosen = safe_dir if safe_dir is not None else risky_dir
        if chosen is None:
            return False
        return self.move(chosen)

    def _calculate_conveyor_path_to(
        self,
        start: Position,
        target: int,
        avoid: int,
        update: bool,
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
        result = self.bfs_route(start_mask, target, avoid)
        if result is None:
            return None
        s_pos, p_pos, dist = result
        if DRAW_DEBUG:
            self.rc.draw_indicator_line(s_pos, p_pos, 255, 0, 255)
            self.rc.draw_indicator_dot(s_pos, 255, 0, 255)
        return (s_pos, p_pos, dist)

    def calculate_conveyor_path(self, start: Position, update: bool = False):
        log("conveyors from ", start)
        target, avoid = self._get_conveyor_targets_and_avoid()
        return self._calculate_conveyor_path_to(start, target, avoid, update)

    def conveyor_cost(self, dist, scaling=None):
        if scaling is None:
            scaling = self.rc.get_scale_percent() / 100
        if dist is None or dist < 0:
            return None
        # Arithmetic-series equivalent of:
        #   sum(3 * (scaling + 0.01 * k) for k in range(dist))
        return (3 * dist * scaling + 0.015 * dist * (dist - 1)) * CONVEYOR_COST_DISCOUNT
    def _get_conveyor_targets_and_avoid(self):
        avoid = map_info.get_avoid(True, False, True)
        target = map_info._bm_route_targets | map_info._bm_my_core_area
        if not target:
            return 0, 0
        return target, avoid
