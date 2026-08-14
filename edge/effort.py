"""The effort model. See blueprint §9.1.

Detection is not observation. A tiger not photographed may be absent, or
the cameras near it may simply have been dead. This module is what lets
the alert engine tell those two things apart:

    effort(station, period)   = camera-days that station was active
    effort_coverage(ind, run) = effort this cycle, over the stations that
                                 make up the individual's historical range,
                                 divided by the same sum averaged over its
                                 previous cycles

effort_coverage ~= 1.0 means this cycle watched the individual's range
about as well as previous cycles did. Every alert carries this number.

No SQL lives here (repo.py owns all of it) -- this module only calls
repo.station_effort_days() and does arithmetic on the result.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from edge.db import repo

Period = tuple[str, str]


def station_days(station_ids, start: str, end: str) -> float:
    """Total camera-days across a set of stations within [start, end)."""
    return round(sum(repo.station_effort_days(sid, start, end) for sid in station_ids), 2)


def cycle_periods(runs: list[dict], default_cycle_days: int) -> dict[str, Period]:
    """Map run_id -> (period_start, period_end) for every run of a reserve.

    A run's period is assumed to run from its own start to the next run's
    start -- that is what "camera-days during that cycle" means. The most
    recent run has no next run to bound it, so its length is assumed equal
    to the gap before it (the best available estimate of how long a cycle
    normally runs); with only one run of history, default_cycle_days is
    the fallback.
    """
    ordered = sorted(runs, key=lambda r: r["started_at"])
    periods: dict[str, Period] = {}
    for i, r in enumerate(ordered):
        start = r["started_at"]
        if i + 1 < len(ordered):
            end = ordered[i + 1]["started_at"]
        else:
            gap = _delta_days(ordered[i - 1]["started_at"], start) if i > 0 else None
            end = _add_days(start, gap if gap and gap > 0 else default_cycle_days)
        periods[r["run_id"]] = (start, end)
    return periods


def coverage(historical_station_ids, current_period: Period,
             prior_periods: list[Period]) -> float | None:
    """effort_coverage: this period's effort over the historical range,
    divided by the same sum averaged over prior periods. None when there
    is no historical range or no prior cycle to compare against -- callers
    decide how to treat "no baseline" for their own rule."""
    if not historical_station_ids or not prior_periods:
        return None
    now = station_days(historical_station_ids, *current_period)
    priors = [station_days(historical_station_ids, *p) for p in prior_periods]
    baseline = sum(priors) / len(priors)
    if baseline <= 0:
        return None
    return round(now / baseline, 3)


def _delta_days(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400.0


def _add_days(a: str, days: float) -> str:
    return (datetime.fromisoformat(a) + timedelta(days=days)).isoformat()
