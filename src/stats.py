"""Statistical flake analysis with Wilson score confidence intervals.

"3 fails out of 5 runs" and "300 fails out of 500 runs" are both a 60% flake
rate — but only one of them means anything. Wilson intervals quantify the
uncertainty so the verdict (stable / flaky / broken) is statistically honest.
"""

import math


def wilson_interval(fails: int, runs: int, z: float = 1.96) -> tuple:
    """95% confidence interval for the true failure rate."""
    if runs == 0:
        return (0.0, 1.0)
    p = fails / runs
    denom = 1 + z * z / runs
    center = p + z * z / (2 * runs)
    margin = z * math.sqrt(p * (1 - p) / runs + z * z / (4 * runs * runs))
    return (max(0.0, (center - margin) / denom),
            min(1.0, (center + margin) / denom))


def flake_verdict(passes: int, fails: int) -> dict:
    """Classify a test's health from its run history.

    Verdicts:
      stable            — confidently passing (interval below 5%)
      flaky             — confidently intermittent (interval excludes 0 and 1)
      broken            — confidently failing (interval above 50%)
      suspect           — some failures but too few runs to be sure
      insufficient-data — fewer than 5 runs
    """
    runs = passes + fails
    rate = fails / runs if runs else 0.0
    lo, hi = wilson_interval(fails, runs)
    if runs < 5:
        verdict = "insufficient-data"
    elif hi < 0.05:
        verdict = "stable"
    elif lo > 0.5:
        verdict = "broken"
    elif lo > 0.0 and hi < 1.0:
        verdict = "flaky"
    else:
        verdict = "suspect"
    return {"passes": passes, "fails": fails, "runs": runs,
            "flake_rate": round(rate, 3),
            "ci95": (round(lo, 3), round(hi, 3)),
            "verdict": verdict}


def verdicts_from_history(history: dict) -> dict:
    """Apply flake_verdict to a {test_id: ['pass','fail',...]} mapping."""
    out = {}
    for test_id, runs in history.items():
        passes = sum(1 for r in runs if r == "pass")
        fails = sum(1 for r in runs if r == "fail")
        out[test_id] = flake_verdict(passes, fails)
    return out
