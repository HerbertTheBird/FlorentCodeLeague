# 1.0 for full cost, lower = discounted cost to more proactively start routes
CONVEYOR_COST_DISCOUNT = 0.65

# --- pay as you go ------------------------------------------------------------
# How many conveyor hops harvest and route quote up front. Quoting the *entire*
# remaining chain and refusing the project unless all of it is affordable this
# turn silently caps how far from our network a harvester can ever be built, and
# keeps the economy small: against sporks that was 145 conveyors and 22
# harvesters over five games, to their 490 and 46. A conveyor laid this turn is
# useful next turn whether or not the rest of the chain exists, and route picks
# the dead end up and extends it.
#
# Swept against Champion_v45 over all 33 maps, both sides. The horizon has a
# plateau, not a threshold -- too short is worse than not bounding it at all,
# because the builder commits to chains it cannot finish:
#
#     3   42.4%      10  60.6%
#     5   40.9%      11  54.5%
#     8   56.1%      12  48.5%
#     9   51.5%      16  42.4%
#                    unbounded (v45) is the 50.0% baseline
#
# 8-11 are all above the baseline and 3/5/12/16 are all below, so the plateau is
# supported by 264 matches rather than by the single best cell; 10 is the argmax
# and sits in the middle of it, but read 60.6% as the top of a noisy peak whose
# true value is nearer the ~55% the plateau averages.
#
# The effect on the maps we lose is not marginal. saga -- 1-21 on the ladder, our
# worst map by a wide margin -- goes from a loss at turn 144 on 34 conveyors and
# 750 Ti to a *win* at turn 487 on 101 conveyors and 4500 Ti. hive goes 26 -> 97
# conveyors and 1890 -> 4480 Ti. heart is the exception and builds slightly fewer
# (42 -> 37); it still wins.
#
# Both call sites must use the same horizon: harvest quotes harvester + chain and
# route quotes chain alone, and a harvester admitted under one budget whose chain
# is refused under the other is the stranded-harvester case this is meant to fix.
PAYG_HORIZON = 10

# How long a "this project costs X" quote stays usable. Both states record the
# quote per tile when they price a candidate and skip that tile while the quote
# exceeds our balance; entries older than this are evicted on the next read.
COST_MAP_TTL = 100
