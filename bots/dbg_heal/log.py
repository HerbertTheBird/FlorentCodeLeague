import os
DEBUG_LOGGING = False
DRAW_DEBUG = True
_PATH = os.environ.get("HB_LOG", "/tmp/hb_dbg.log")
_fh = None
def _f():
    global _fh
    if _fh is None: _fh = open(_PATH, "a", buffering=1)
    return _fh
def log(*a, **k): pass
def dbg(*a):
    try: _f().write(" ".join(str(x) for x in a) + "\n")
    except Exception: pass
