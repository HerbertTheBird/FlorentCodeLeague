DEBUG_LOGGING = False
DRAW_DEBUG = True

if DEBUG_LOGGING:
    def log(*args, **kwargs):
        print(*args, **kwargs)
else:
    def log(*args, **kwargs):
        pass


# Always-on, low-volume per-unit status line (role / state / target). Shows up in
# the unit's stdout (bot output) in the visualiser.
STATUS_LOGGING = True

if STATUS_LOGGING:
    def status(*args, **kwargs):
        print(*args, **kwargs)
else:
    def status(*args, **kwargs):
        pass
