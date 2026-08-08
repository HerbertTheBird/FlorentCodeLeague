#!/bin/zsh
# Queue unrated matches against the top teams.
#
# URs use whatever submission is ACTIVE, so activate the build under test first.
# Platform limit is 5 test/unrated matches per 10 minutes.
#
# Maps are pinned per batch so successive batches are comparable rather than
# re-rolling the draw, but the SET rotates: three disjoint fives covering the
# whole 15-map pool, advancing each run. Pinning alone would just move the
# problem from noise to overfitting -- tuning until we beat five specific maps.
# Pass a set number (0-2) to force one.
cd "${0:A:h}/.."
SPORKS=57175ea4-e87f-44ed-a8c4-d5ee8ba31ecd
# Pivot dropped: we run ~90% against them and the slot was not buying
# information. Erebus replaces it -- rank #7, below us, and the team whose
# 1-4 exposed the early-rush core-death weakness that is still open.
EREBUS=9810ba35-66a9-4af3-9a2f-06651aef4109
ADGATO=fb0e7053-f8f3-4cc8-a38f-1856a518c7d2
JYTHON=8cf9b751-00d3-484a-b0ed-e3073ae1d46f
# team lazy dropped from the batch: we beat them 100% and the slot was
# wasted. Lorem Ipsum replaces it -- lower rated but we keep losing to them,
# which is exactly the signal a batch should be spending a slot on.
LOREM=017419af-711a-40b0-9826-60cc754bd840

SETS=(
  "antler jackpot saga heart moonrise"
  "hive eider nordkap lighthouse atoll"
  "drumlin snowflake archipelago meander fjordgate"
)
STATE=.ur_mapset
if [[ -n "$1" ]]; then
  IDX=$1
else
  IDX=$(cat $STATE 2>/dev/null || echo 0)
  print $(( (IDX + 1) % 3 )) > $STATE
fi
MAPS=()
for m in ${=SETS[$((IDX + 1))]}; do MAPS+=(--map $m); done
print "map set $IDX: ${=SETS[$((IDX + 1))]}"
for id in $SPORKS $EREBUS $ADGATO $JYTHON $LOREM; do
  fcode match unrated $id $MAPS 2>&1 | tail -1
done
