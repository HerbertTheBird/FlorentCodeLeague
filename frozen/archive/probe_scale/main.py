from fcode import Controller, EntityType, Position

LOG = "/private/tmp/claude-501/-Users-yufan-Desktop-Development-FlorentCodeLeague/22c273d2-ac06-4fe9-a889-7eb49dead654/scratchpad/probe.log"
NBR = [(1, 0), (-1, 0), (0, 1), (0, -1)]


def w(s):
    with open(LOG, "a") as f:
        f.write(s + "\n")


class Player:
    def run(self, c: Controller) -> None:
        try:
            self._run(c)
        except Exception as e:
            w(f"ERR {e!r}")

    def _run(self, c: Controller) -> None:
        r = c.get_current_round()
        if r > 80:
            return
        et = c.get_entity_type()
        if et == EntityType.CORE:
            p = c.get_position()
            if r <= 3:
                for dx in (-1, 0, 1, 2):
                    for dy in (-1, 0, 1, 2):
                        t = Position(p.x + dx, p.y + dy)
                        if c.can_spawn(t):
                            c.spawn_builder(t)
                            dx = dy = 99
                            break
                    if dx == 99:
                        break
            w(f"R{r} scale={c.get_scale_percent()} conv={c.get_conveyor_cost()} "
              f"barr={c.get_barrier_cost()} harv={c.get_harvester_cost()} "
              f"gun={c.get_gunner_cost()} sent={c.get_sentinel_cost()} "
              f"bot={c.get_builder_bot_cost()} ti={c.get_global_resources()}")
            return
        if et != EntityType.BUILDER_BOT:
            return
        p = c.get_position()
        for dx, dy in NBR:
            t = Position(p.x + dx, p.y + dy)
            if c.can_build_barrier(t):
                c.build_barrier(t)
                return
