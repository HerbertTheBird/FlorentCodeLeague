#!/bin/zsh
# Re-run the maps we actually LOST on, against the teams we lost them to.
#
# The rotating top-five batch in ur.sh answers "are we good"; this answers "did
# the specific thing we fixed actually fix it". Maps are the ones from real
# losses, so a before/after is directly comparable rather than a fresh draw.
#
# Platform limit is 5 test/unrated matches per 10 minutes, and URs run whatever
# submission is ACTIVE.
cd "${0:A:h}/.."
EREBUS=9810ba35-66a9-4af3-9a2f-06651aef4109
LOREM=017419af-711a-40b0-9826-60cc754bd840
SPORKS=57175ea4-e87f-44ed-a8c4-d5ee8ba31ecd

# Erebus beat v47 4-1: drumlin, atoll, antler, snowflake (we won only archipelago).
EREBUS_MAPS=(--map drumlin --map atoll --map antler --map snowflake --map archipelago)
# sporks' long-game maps -- the ones decided on titanium at turn 1000, where we
# go 0-for: lighthouse 0-3, antler 0-2, moonrise 1-6.
SPORKS_MAPS=(--map lighthouse --map antler --map moonrise --map hive --map jackpot)

print "revenge batch: Erebus on its 4 winning maps + Lorem Ipsum + sporks long-game maps"
fcode match unrated $EREBUS $EREBUS_MAPS 2>&1 | tail -1
fcode match unrated $LOREM  $EREBUS_MAPS 2>&1 | tail -1
fcode match unrated $SPORKS $SPORKS_MAPS 2>&1 | tail -1
