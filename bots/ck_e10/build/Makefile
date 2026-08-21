# Default is `packed`, and the reason is measured, not assumed: the engine starts
# a fresh interpreter per game and does NOT reuse __pycache__, so the pure-literal
# build's warm-import advantage never happens and every game pays its 909 ms cold
# compile instead of 124 ms. Median game time over the same 86-game suite went
# 22.3s -> 24.8s with identical turn counts.
#
# `make literal` is one command away and equally correct -- if the platform turns
# out to cache bytecode, it becomes the better choice (32 ms warm vs ~40 ms).
.PHONY: all literal packed check report cost

all: packed

literal:
	python3 render.py --literal

packed:
	python3 render.py

# A refactor must not change behaviour: the generated tables have to equal the
# ones the solver's own codec produces, byte for byte.
check:
	python3 verify.py

report:
	python3 render.py --report

cost:
	python3 importcost.py
