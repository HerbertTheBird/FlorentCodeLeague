"""Sentinel-rush cost model -- the bot-side port of /rush_math.py.

Every quantity is per ROUND (rush_math writes them per "second"; a round is the
game's unit of time, so the numbers carry over unchanged):

    sentinel damage          9 HP/round   (18 dmg on a reload-2 turret)
    sentinel operating cost  5 Ti/round   (10 ammo per shot, one shot per 2 rounds)
    sentinel base price     30 Ti
    price scaling           +0.2 of base per sentinel already built
    builder heal            +4 HP/round for 1 Ti

Two questions are asked of the same model, from opposite ends:

  ATTACK   Given the defender's healing rate R, how many sentinels n minimise
           the total titanium needed to kill a structure of H HP? -> best_n()

  DEFENCE  Given what the attacker can afford, how many healers k make every n
           unaffordable (or impossible)? -> healers_needed()

`c_build` mirrors rush_math.c_build exactly, including its /2: the k-th sentinel
costs 30*(a + 0.2*(k-1)), and summing k = 1..n gives 30*n*(a + 0.1*(n-1)).
"""

DAMAGE = 9              # HP per round per sentinel
OPERATING_COST = 5      # Ti per round per sentinel
BASE_PRICE = 30         # Ti
PRICE_SCALING = 0.2     # multiplier added per sentinel already built
HEAL_PER_BUILDER = 4    # HP per round per healing builder
HEAL_COST = 1           # Ti per heal

INF = float("inf")

# rush_math sweeps n = 1..9; the brief asks for 1..10. The extra row is free --
# the whole sweep is ten arithmetic expressions.
N_MIN = 1
N_MAX = 10


def c_build(n: int, a: float) -> float:
    """Ti to build n sentinels when the price multiplier starts at `a`."""
    return BASE_PRICE * n * (a + PRICE_SCALING * (n - 1) / 2)


def c_op(n: int, hp: float, heal_rate: float) -> float:
    """Ti of ammunition n sentinels burn before the structure dies.

    INF when the defenders out-heal the incoming damage -- the structure never
    falls, so no amount of titanium finishes the job.
    """
    net = DAMAGE * n - heal_rate
    if net <= 0:
        return INF
    return OPERATING_COST * n * (hp / net)


def c_total(n: int, hp: float, heal_rate: float, a: float) -> float:
    return c_build(n, a) + c_op(n, hp, heal_rate)


def sweep(hp: float, heal_rate: float, a: float):
    """[(n, c_build, c_op, c_total)] for n = N_MIN..N_MAX. Brute force, as the
    brief asks -- ten rows of arithmetic, far cheaper than one BFS."""
    return [(n, c_build(n, a), c_op(n, hp, heal_rate), c_total(n, hp, heal_rate, a))
            for n in range(N_MIN, N_MAX + 1)]


def best_n(hp: float, heal_rate: float, a: float):
    """(n, cost) minimising total titanium, or (None, INF) if no n in range can
    break through the healing."""
    best, best_cost = None, INF
    for n, _cb, _co, ct in sweep(hp, heal_rate, a):
        if ct < best_cost:
            best, best_cost = n, ct
    return best, best_cost


def min_rush_cost(hp: float, heal_rate: float, a: float) -> float:
    """Cheapest titanium an attacker can kill `hp` for, against `heal_rate`."""
    return best_n(hp, heal_rate, a)[1]


def healers_needed(enemy_ti: float, hp: float, a: float, cap: int = 12) -> int:
    """Fewest simultaneous healers that price the attacker out of the rush.

    Sweeps k upward and returns the first k whose induced healing rate 4k makes
    the attacker's CHEAPEST option (min over n = 1..10) cost more titanium than
    they can field. Returns `cap` if even that is not enough -- the caller is
    then defending a losing position and should still put everyone on heals.

    Note the asymmetry that makes this worth doing: a healer costs 1 Ti/round
    and denies 4 HP/round, while a sentinel costs 5 Ti/round to deliver 9. Each
    healer we add forces roughly half a sentinel more, at five times the price.
    """
    for k in range(0, cap + 1):
        if min_rush_cost(hp, HEAL_PER_BUILDER * k, a) > enemy_ti:
            return k
    return cap


def estimated_enemy_ti(round_num: int,
                       starting_ti: float = 500.0,
                       income_per_round: float = 5.0) -> float:
    """Crude upper bound on the enemy's cumulative titanium, per the brief:
    500 to start plus a flat 5/round. Deliberately simple -- we cannot see
    their balance, and over-estimating only makes us defend harder."""
    return starting_ti + income_per_round * max(0, round_num)
