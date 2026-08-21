DEBUG_STATE = True   # per-turn builder state printing (local dev; stripped for ladder)
DEBUG_LOGGING = False
DRAW_DEBUG = True

if DEBUG_LOGGING:
    def log(*args, **kwargs):
        print(*args, **kwargs)
else:
    def log(*args, **kwargs):
        pass
