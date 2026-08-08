#!/bin/zsh
# A/B two of our submissions against the SAME opponent versions on the SAME maps.
#
# Why this exists: per-version UR records are not comparable across time. The
# field iterates while we do -- sporks went v2 to v7 in an hour, Erebus is on
# v55, Lorem Ipsum v23 -- so "v50 scored 80% and v51 scores 74%" compares two of
# our bots against two different fields and says nothing about the change.
#
# Two controls make it a real experiment:
#   1. `fcode match unrated --match <id>` pins the OPPONENT to the submission
#      they used in that match, so both of our versions face identical code.
#   2. --map pins the maps, so both play the same draw.
# What cannot be controlled is which of ours is active: URs always run the
# active submission, so this activates each in turn. THAT MOVES THE LADDER BOT
# while the batch runs, and ladder matches queued against us in that window are
# played by whichever version is up. Cost of measuring honestly.
#
# Usage:  ./tools/ur_ab.sh <versionA> <versionB> <teamId:matchId> [more...]
# Example: ./tools/ur_ab.sh 50 51 57175ea4-...:9294d4ee-... 017419af-...:51c21783-...
#
# Note `--match` does NOT replace the positional OPPONENT_ID; it narrows which
# of that team's submissions to play. Passing it alone fails with "Missing
# argument 'OPPONENT_ID'", and because the loop swallowed stderr that failure
# was silent the first time -- the whole A round queued nothing and the numbers
# that came back were ambient traffic. Hence the explicit check below.
#
# Read results with:  python3 tools/ur_summary.py --limit 200
# Platform limit is 5 unrated matches per 10 minutes, so each version's round
# takes ~10 minutes per 5 opponents.
set -e
cd "${0:A:h}/.."

VA=$1; VB=$2; shift 2
if [[ -z "$VA" || -z "$VB" || $# -eq 0 ]]; then
    print -u2 "usage: $0 <versionA> <versionB> <pinned-match-id> [more-ids...]"
    exit 2
fi
PAIRS=("$@")

# Same map draw for both sides of the experiment.
MAPS=(--map antler --map moonrise --map saga --map drumlin --map heart)

run_round() {
    local ver=$1
    print "=== activating v$ver ==="
    fcode submission activate "$ver" 2>&1 | tail -1
    sleep 5
    local queued=0
    for pair in $PAIRS; do
        local team="${pair%%:*}" mid="${pair##*:}"
        local out
        out=$(fcode match unrated "$team" --match "$mid" $MAPS 2>&1 | tail -1)
        print -r -- "  $out"
        # NOT `(( queued++ ))`: zsh arithmetic returns the pre-increment value as
        # its exit status, so the first success exits 1 and `set -e` kills the run
        # after a single queued match -- leaving the WRONG version active.
        [[ "$out" == *florent.vc* ]] && queued=$(( queued + 1 ))
    done
    if (( queued == 0 )); then
        print -u2 "ur_ab: v$ver queued NOTHING -- aborting rather than reporting a half-run experiment"
        exit 1
    fi
    print "  v$ver: $queued queued"
}

# Whatever happens, do not leave the ladder on the A version.
restore() { fcode submission activate "$VB" >/dev/null 2>&1 || true }
trap restore EXIT INT TERM

run_round $VA
print "waiting out the rate limit before the second half..."
sleep 620
run_round $VB

print ""
print "queued. give the matches a few minutes, then:"
print "  python3 tools/ur_summary.py --limit 200"
print "both halves faced the same opponent submissions on the same five maps."
