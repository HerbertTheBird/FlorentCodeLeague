#!/bin/zsh
# Queue unrated matches against the top teams. URs use the ACTIVE submission,
# so activate the build under test first. Platform limit: 5 URs / 10 minutes.
SPORKS=57175ea4-e87f-44ed-a8c4-d5ee8ba31ecd
PIVOT=20064efa-c6a9-4c46-acb7-3f7ea9f9b1c9
ADGATO=fb0e7053-f8f3-4cc8-a38f-1856a518c7d2
JYTHON=8cf9b751-00d3-484a-b0ed-e3073ae1d46f
LAZY=648d1d5b-5443-4257-a0aa-7048661b612d
for id in $SPORKS $PIVOT $ADGATO $JYTHON $LAZY; do
  fcode match unrated $id 2>&1 | tail -1
done
