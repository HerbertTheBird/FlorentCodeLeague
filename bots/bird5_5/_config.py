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

# --- route reach limit --------------------------------------------------------
# Longest conveyor chain we are willing to own, counted in CONVEYORS from the
# titanium source to the core (the conveyor that empties into a core tile is 1).
# Route and harvest both refuse a candidate whose cheapest chain to the core is
# longer than this, so we never start a line we do not intend to finish.
#
# This is a *reach* limit and is distinct from PAYG_HORIZON above, which is a
# *quote* horizon: PAYG says "only price the next 10 hops before committing this
# turn's titanium", and deliberately lets an arbitrarily long chain grow one
# affordable hop at a time. That is what produced 100-conveyor networks on saga
# and hive. MAX_ROUTE_CONVEYORS is the cap those runs never had.
#
# Why a cap at all: a chain of n conveyors costs ~3n Ti to lay and every tile of
# it is a 20 HP building an enemy builder kills in ten attacks, anywhere along
# its length. A harvester pays 10 Ti per 4 rounds, so the further out the ore,
# the longer the payback and the more chain there is to defend while it pays.
#
# The two are read together on purpose: with both at 10, PAYG quotes exactly the
# chain length the reach limit permits, so a project that passes the reach gate
# is priced in full rather than in instalments.
MAX_ROUTE_CONVEYORS = 10

# --- bird2: econ-light sentinel rush ------------------------------------------
# The lone opening builder lays this many harvesters before committing to the
# rush. "A few (possibly none or one)" -- the point is not an economy, it is
# enough passive income that the siege's ammunition stays paid for. Every round
# spent harvesting is a round the enemy spends fortifying, so this is cheap by
# design.
OPENING_HARVESTERS = 1

# What we assume the defender's heal rate is, in BUILDERS (each worth 4 HP/round
# to rush.py). We cannot see their builders from across the map, and the cost
# curve is flat near its minimum, so a fixed guess costs little: at H=500 and our
# opening price multiplier the optimum only moves by one sentinel between 0 and 4
# assumed healers. 2 is the brief's default.
RUSH_ASSUMED_ENEMY_HEALERS = 2

# Enemy core HP -- what the siege has to chew through.
RUSH_SENTINEL_HP = 500

# Hard cap on sentinels in one siege, independent of what the model asks for.
# Sentinel cost scales +20% each, so the model's n is priced against a rising
# curve already; this only bounds the pathological case where the assumed heal
# rate makes every n look necessary.
RUSH_MAX_SENTINELS = 8

# --- defensive healers --------------------------------------------------------
# What we credit the ENEMY with earning, for sizing our own defence: 500 to start
# plus a flat 5 Ti/round. Deliberately crude -- their balance is unobservable, and
# over-estimating only makes us hold more builders home, which is the safe error.
ENEMY_START_TI = 500.0
ENEMY_TI_PER_ROUND = 5.0

# Never hold more than this many builders at home purely as healers, however
# expensive rush.healers_needed says the enemy's rush is. Past this the defence
# has eaten the whole economy and we have lost on tiebreakers anyway.
MAX_DEFENSIVE_HEALERS = 8

# Round by which the rusher abandons the opening economy and leaves, whether or
# not it managed OPENING_HARVESTERS. Without a deadline the quota deadlocks a
# rusher that spawns with no reachable ore: it waits for a harvester it will
# never build, and the rush never happens.
#
# 40 was tried first and was far too late. Traced on saga: the rusher hit the
# deadline at round 40 having built no harvester, started walking at round 41
# from (13,16) toward an enemy core at (4,4), was still walking at round 55 --
# and the game ended at 58. It crossed the map exactly once and never placed a
# sentinel. Travel is 15-25 rounds on a mid-size map, so anything the rush is
# meant to influence has to leave before round ~12.
RUSH_COMMIT_ROUND = 10

# Fraction of the model's total rush cost (C_build + C_op, from rush.best_n) the
# bank must hold before the rusher leaves home. 1.0 = the whole thing.
# C_op is ammunition spent over the kill rather than up front, so 1.0 is stricter
# than the model strictly requires -- but the failure it prevents is the worst one
# available: a builder that walks the map, plants what it can afford, and leaves
# sentinels standing there with no ammunition to fire.
RUSH_COMMIT_TI_FRACTION = 1.0

# --- core defence: rate and absolute-HP tiers ---------------------------------
# Rounds over which the core measures its own NET HP trend. Net, not incoming
# damage: the healing already happening is subtracted for free, so "still losing"
# is exactly the signal that the current garrison is too small. Long enough to
# span a sentinel's reload (and an ammo-starved one firing every 4th round),
# short enough to react inside a 500 HP pool.
CORE_HP_WINDOW = 24

# Below this the core sizes its garrison to FLIP the HP trend, not merely to
# satisfy the cost model. Above it, a dent is not yet worth pulling builders off
# the economy.
CORE_DEFEND_HP = 250

# Below this every spare builder heals, at the cap, regardless of what the model
# says. Losing the core loses the game outright; nothing else is worth titanium.
CORE_CRITICAL_HP = 100

# Deliberate over-estimate of enemy income (was 5.0). Over-estimating makes us
# hold MORE builders home, which costs economy; under-estimating loses the core,
# which costs the game. The errors are not symmetric, so bias to the safe side.
ENEMY_TI_OVERESTIMATE = 2.0

# --- bird4 ablation flags -----------------------------------------------------
# A: how ti_reserve() is sized.
#   "flat"         -- bird3's fixed TI_RESERVE_CAP (8 Ti).
#   "passive_tick" -- enough for RESERVE_HEALERS healers to each heal every round
#                     until the next passive titanium tick.
# The flat 8 was measured failing exactly as designed to prevent: heals are EXEMPT
# from the reserve (that is what it is FOR), so eight healers drain 8 Ti in ONE
# round and then stand there unable to act. Both glacierkeep losses in the 86-game
# panel ended with us holding 10 Ti and 0 Ti at turns 40 and 43.
#
# Passive income is PASSIVE_TITANIUM_AMOUNT (10) every PASSIVE_TITANIUM_INTERVAL
# (4) rounds -- the one income that cannot be taken away. Holding
# RESERVE_HEALERS * rounds_until_next_tick guarantees every healer can act every
# round until that tick lands, whatever happens to the harvesters.
RESERVE_MODE = "survive_rush"
RESERVE_HEALERS = 8

# B: skip the turret duel while we are winning the healing race.
# A sentinel duel costs 10 ammo a shot to remove a building that is, by
# assumption, not actually killing us -- while the same shot into a builder or
# the core advances the game. Only meaningful when we KNOW we are out-healing,
# which the core measures directly (core_net_dps() <= 0) and broadcasts.
SKIP_TURRET_DUEL = True

# --- bird5 ---------------------------------------------------------------------
# Only EIGHT tiles are cardinally adjacent to the 2x2 core, and a heal needs
# orthogonal adjacency -- so eight is the hard physical ceiling on simultaneous
# healers, and 8 x 4 = 32 HP/round is the most healing the core can ever receive.
# (Worth knowing what that does NOT cover: four enemy sentinels are 36 HP/round.)

# The enemy's sentinel price multiplier. FIXED, not read from our own
# get_sentinel_cost(): our scale reflects turrets WE built, which says nothing
# about theirs, and using it made the reserve swing with our own construction.
ENEMY_SENTINEL_SCALE = 1.2

# "survive_rush": hold back enough titanium to keep RESERVE_HEALERS healing for
# as long as the enemy can afford to keep firing the best rush their estimated
# titanium buys. Ceiling so the reserve can never freeze the whole economy.
RESERVE_MAX = 200

# Suppress attack-state TURRET CONSTRUCTION while we are out-healing. Distinct
# from SKIP_TURRET_DUEL, which only changes what existing turrets shoot at: this
# is the spend. Measured on glacierkeep: 90 Ti of gunners and a sentinel went up
# at turns 24-27, and by turn 37 we were on 0 Ti with five healers in position
# and nothing to heal with.
SKIP_TURRET_BUILD = True

