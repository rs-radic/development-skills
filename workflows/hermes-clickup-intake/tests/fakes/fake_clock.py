"""Deterministic millisecond clock for store and engine tests."""


class FakeClock:
    """Callable returning epoch milliseconds; advance explicitly."""

    def __init__(self, start_ms=1_800_000_000_000):
        self.now_ms = int(start_ms)

    def __call__(self):
        return self.now_ms

    def advance(self, seconds=0, ms=0):
        self.now_ms += int(seconds * 1000) + int(ms)
        return self.now_ms
