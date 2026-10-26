"""Dynamic AI frame rate shared by the cameras that are running a module.

    1 camera   -> AI_FPS_MAX (the camera's own frame rate if that is lower): the maximum
    2 cameras  -> AI_FPS_BUDGET / 2 each, clamped to [AI_FPS_MIN, AI_FPS_MAX]

At most MAX_AI_CAMERAS cameras may run modules at the same time (SessionManager.start_module refuses more).
When a camera joins or leaves, the cameras that are already running move to their new rate by at most
AI_FPS_RAMP fps per second, so the change is gradual; a camera that has just started begins at its target.

`target(n)` is the rate for n cameras; `Rate` is the per-camera eased value. Pure arithmetic - no camera, no model.
"""
from backend import config


def target(n_cameras, ceiling=None, budget=None, floor=None):
    """Analysed frames per second for each of `n_cameras` running cameras."""
    ceiling = float(config.AI_FPS_MAX if ceiling is None else ceiling)
    budget = float(config.AI_FPS_BUDGET if budget is None else budget)
    floor = float(config.AI_FPS_MIN if floor is None else floor)
    n = max(1, int(n_cameras))
    if n == 1:
        return ceiling
    return max(min(floor, ceiling), min(ceiling, budget / n))


class Rate:
    """One camera's current rate: eases towards `goal` by at most `ramp` fps per second."""

    def __init__(self, start):
        self.value = float(start)
        self.goal = float(start)
        self._t = None

    def set_goal(self, goal, immediate=False):
        self.goal = float(goal)
        if immediate:
            self.value = self.goal
            self._t = None

    def step(self, now, ramp=None):
        ramp = float(config.AI_FPS_RAMP if ramp is None else ramp)
        dt = 0.0 if self._t is None else max(0.0, min(5.0, now - self._t))
        self._t = now
        d = self.goal - self.value
        if abs(d) <= ramp * dt or dt == 0.0 and abs(d) < 1e-9:
            self.value = self.goal
        elif dt > 0.0:
            self.value += ramp * dt * (1 if d > 0 else -1)
        return self.value
