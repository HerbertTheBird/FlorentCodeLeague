"""Central seeding for every indifferent tie-break in the bot.

The bot makes many choices it genuinely does not care about -- which of two
equally-good tiles to build on, which valid direction to wander in, which of
several equidistant targets to shoot. Historically those were resolved by
iteration order (the first `Direction` that passed, `list[0]`, whatever a `set`
yielded first), which made play rigid and repetitive: two runs of the same bot
on the same map produced byte-identical games, so an A/B on a change that only
fires occasionally came back as a wall of identical trajectories.

Randomising the *indifferent* picks breaks that up so tests actually explore the
space of games. Every such pick draws from Python's global `random`, seeded HERE
so the whole thing stays reproducible:

  * `BASE_SEED` is fixed (0) by default, so a match replays exactly.
  * Override it with the env var HERBERT_SEED=<int> to get a different-but-
    reproducible game. The engine's own `--seed` is NOT visible to a bot (there
    is no Controller.get_seed) and does not perturb a deterministic bot's play,
    so this env var is the only lever a test harness has to diversify runs.

`seed_for_unit` is called once per unit on its first turn (see main.py). Mixing
the unit id AND its side (team) into the seed keeps each unit's stream distinct
and keeps the two teams in a mirror match from playing in lockstep, while keeping
the match as a whole deterministic given BASE_SEED.

Only ever randomise choices the bot does not rank. Anything scored, sorted by a
real cost, or feeding a deterministic structure (comms hashing, precomputed
tables, bitboard construction) must stay deterministic -- do not draw from
`random` there.
"""
import os
import random

# The env override above was commented out and BASE_SEED pinned to a literal,
# while the docstring kept advertising HERBERT_SEED as "the only lever a test
# harness has to diversify runs". It is not a cosmetic inconsistency: with the
# override dead, every indifferent tie-break in the bot is fixed at one arbitrary
# draw, so an A/B over the whole map pool is a sample of size ONE in the tie-break
# dimension -- and a change that merely perturbs the RNG stream (DRAW_DEBUG does
# exactly that, via _draw_attack_candidates -> get_best_direction -> random.choice)
# moved single-opponent win rates by up to 8.5 points with no strategy change.
#
# Restored, defaulting to the same 67 so play is byte-identical when the variable
# is unset. Set HERBERT_SEED=<int> to draw an independent, still-reproducible
# sample; averaging a candidate over several is the only honest way to measure a
# change in this bot.
try:
    BASE_SEED = int(os.environ.get("HERBERT_SEED", "67"))
except (TypeError, ValueError):
    BASE_SEED = 67
# Large odd multiplier so adjacent (base, side) pairs land far apart in seed
# space. unit_id (always < a few hundred) fits below it without colliding.
_MIX = 1_000_003


def seed_for_unit(unit_id: int, side: int = 0) -> None:
    """Seed the global RNG for this unit, deterministically from BASE_SEED.

    `side` (0/1, the unit's team) is mixed in so the two teams in a mirror match
    make *different* indifferent choices under the same HERBERT_SEED. random.seed
    only accepts a scalar, so the three axes are folded into one int: (base, side)
    picks the stream, +unit_id offsets within it.
    """
    random.seed((BASE_SEED * 2 + side) * _MIX + unit_id)


def shuffled(seq):
    """A new list holding `seq`'s items in a random order. Convenience for the
    `for x in seq: if ok(x): pick x` pattern where any valid x is equally good."""
    out = list(seq)
    random.shuffle(out)
    return out
