from fcode import Controller


class Player:
    """No-op bot: every unit does nothing on every turn (never moves, builds,
    fires, or acts). Useful as a passive baseline / control opponent."""

    def run(self, c: Controller) -> None:
        return
