from main import has_op
DEBUG_LOGGING = True
CHOKEPOINT_DRAW_DEBUG = True
DRAW_DEBUG = False

if DEBUG_LOGGING:
    def log(*args, **kwargs):
        print(*args, **kwargs)
else:
    def log(*args, **kwargs):
        pass
