"""Comms — STUBBED for the Titan (fcode) port.

Cambridge Battlecode communicated via tile *markers* (`place_marker`), which do
not exist in Titan (it has a 16-slot `read_store`/`write_store` instead). The
postmortem notes comms was "supplementary only — bots don't depend on it," so
for this port comms is disabled: all entry points are no-ops. The callers in
`builder.py` and `map_info.py` were also stripped, so nothing here should run.

To re-enable later, reimplement over the 16-slot store (buffered, per-team u32).
"""

from cambc import Controller

# Constants still referenced by callers that read comms.* at import time.
TYPE_LAUNCHER_ORDER = 0
TYPE_SYMMETRY_BROADCAST = 1


def init(c: Controller) -> None:
    pass


def get_new_messages():
    return []


def broadcast_symmetry(corresponding_pos) -> None:
    pass


def give_launcher_order(target_idx) -> None:
    pass


# Decode helpers kept as harmless no-ops in case a caller slips through.
def decode_sym(v):
    return 0


def decode_type(v):
    return None


def decode_location(v):
    return None


def get_sym_bits() -> int:
    return 0
