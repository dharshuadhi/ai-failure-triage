"""Failure analytics backed by Redis (with in-memory fallback).

Data model — per test, per day:
  * bitmap  triage:{test_id}:{date}:fail  — bit N = run N failed (1) or passed (0)
  * counter triage:{test_id}:{date}:runs  — total runs that day
  * HLL     triage:failing-tests:{date}   — unique failing tests that day

Why this shape: bitmaps make flake-rate a couple of BITCOUNT ops, and
HyperLogLog answers "how many distinct tests failed today" in ~12KB no
matter how big the suite gets. Without a Redis server, everything falls
back to an in-memory store with the same API so tests and demos just work.
"""

import datetime

try:
    import redis
except ImportError:
    redis = None


class MemoryStore:
    """Minimal Redis-compatible store for bitmaps, counters and HLLs."""

    def __init__(self):
        self.bitmaps = {}
        self.counters = {}
        self.sets = {}

    # bitmaps
    def setbit(self, key, offset, value):
        bits = self.bitmaps.setdefault(key, set())
        if value:
            bits.add(offset)
        else:
            bits.discard(offset)

    def bitcount(self, key):
        return len(self.bitmaps.get(key, set()))

    # counters
    def incr(self, key, amount=1):
        self.counters[key] = self.counters.get(key, 0) + amount
        return self.counters[key]

    def get(self, key):
        return self.counters.get(key)

    # hyperloglog (exact in memory; approximate in Redis — same API)
    def pfadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    def pfcount(self, key):
        return len(self.sets.get(key, set()))


class FailureAnalytics:
    """Record runs and answer flake-rate / trend questions."""

    def __init__(self, redis_url=None):
        if redis and redis_url:
            self.store = redis.from_url(redis_url, decode_responses=False)
            self.backend = "redis"
        else:
            self.store = MemoryStore()
            self.backend = "memory"

    @staticmethod
    def _day(dt=None):
        return (dt or datetime.date.today()).isoformat()

    def _keys(self, test_id, day):
        safe = test_id.replace("::", "__")
        return (f"triage:{safe}:{day}:fail", f"triage:{safe}:{day}:runs",
                f"triage:failing-tests:{day}")

    def record(self, test_id, passed: bool, day=None):
        """Record one run outcome."""
        day = day or self._day()
        fail_key, runs_key, hll_key = self._keys(test_id, day)
        run_no = self.store.get(runs_key) or 0
        self.store.setbit(fail_key, run_no, 0 if passed else 1)
        self.store.incr(runs_key)
        if not passed:
            self.store.pfadd(hll_key, test_id)

    def flake_rate(self, test_id, day=None) -> dict:
        """Fraction of runs that failed on a given day (0.0 - 1.0)."""
        day = day or self._day()
        fail_key, runs_key, _ = self._keys(test_id, day)
        runs = self.store.get(runs_key) or 0
        fails = self.store.bitcount(fail_key)
        return {"test_id": test_id, "day": day, "runs": runs, "fails": fails,
                "flake_rate": (fails / runs) if runs else 0.0}

    def failing_tests_today(self, day=None) -> int:
        """Distinct failing tests (HyperLogLog)."""
        day = day or self._day()
        return self.store.pfcount(f"triage:failing-tests:{day}")

    def leaderboard(self, test_ids, day=None, top=10) -> list:
        """Rank tests by flake rate — the 'most flaky' list."""
        rows = [self.flake_rate(t, day) for t in test_ids]
        rows = [r for r in rows if r["runs"] >= 2]
        return sorted(rows, key=lambda r: -r["flake_rate"])[:top]

    def trend(self, test_id, days: int) -> list:
        """Per-day fail counts for the last N days."""
        today = datetime.date.today()
        out = []
        for i in range(days):
            day = (today - datetime.timedelta(days=i)).isoformat()
            fail_key, runs_key, _ = self._keys(test_id, day)
            out.append({"day": day,
                        "runs": self.store.get(runs_key) or 0,
                        "fails": self.store.bitcount(fail_key)})
        return list(reversed(out))
