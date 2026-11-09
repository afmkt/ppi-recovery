"""Read-time staleness / carry-forward for the BOOM read surface.

The data condition in production is worse than a clean feed: gaps, lags, missing
points. This module is the *only* place that decides what a boom endpoint shows
when the current value is missing or old. It is **read-time only** — it never
writes a carried value back into a ``DailySummary``, baseline, or energy
recursion, so the algorithm stays fed by real measurements (the spec's own
"数据缺失…不得用0替代" rule extended to "no stale substitution").

Two behaviors, both keyed to the device's local timezone:

* **Current cards** (``mark_stale``): a value's ``asOf`` (the day it came from)
  and ``stale`` (True when that day is older than ``STALE_WINDOW_DAYS`` or when
  there is no data at all). The value is always returned ("mark, don't cap").
* **Trends** (``locf`` / ``locf_atlctl``): for a day with no real value, carry
  the last real value forward across a gap <= ``STALE_WINDOW_DAYS``, marked
  ``carried``; a larger gap is left as a real hole (omitted, never zeroed).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from newboom.boom.schemas import AtlCtlPoint, DatePoint
from newboom.config import STALE_WINDOW_DAYS
from newboom.model import Device

__all__ = [
    "local_today",
    "mark_stale",
    "locf",
    "locf_atlctl",
]


def local_today(device: Device | None) -> date:
    """Device-local 'today' — the server's wall-clock date.

    Mirrors ``newboom.boom_compute._today_local`` so the staleness threshold and
    the pipeline's day keying use the same clock. The system is pure local
    (single zone), so the device's ``tz`` is informational and not applied.
    """
    return datetime.now().date()


def mark_stale(
    as_of: date | None,
    now: date | None = None,
    device: Device | None = None,
    window_days: int = STALE_WINDOW_DAYS,
) -> tuple[str | None, bool]:
    """``(asOf_isoformat, stale)`` for a current-card value.

    ``asOf`` is the day the value came from (``None`` when there is no data at
    all). ``stale`` is True when there is no data, or the value's day is older
    than ``window_days`` days from device-local ``now``. The value is returned
    regardless — this only *marks* it.
    """
    if as_of is None:
        return None, True
    if now is None:
        now = local_today(device)
    stale = (now - as_of).days > window_days
    return as_of.isoformat(), stale


def _pdate(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _carry(
    real_sorted: list[tuple[date, object]],
    start: date,
    end: date,
    window_days: int,
) -> list[tuple[date, object, bool]]:
    """Core LOCF: over ``[start, end]`` emit a point per day that has a real
    value or a real value within ``window_days``. Returns ``(emit_date, source,
    carried)`` triples; a gap > ``window_days`` (or no data at all) omits the day.

    ``real_sorted`` is the real points sorted ascending by day; points earlier
    than ``start`` seed the leading carry but are not themselves emitted.
    """
    by_date: dict[date, object] = {}
    last: tuple[date, object] | None = None  # most recent real day <= current scan
    for day, point in real_sorted:
        if day < start:
            last = (day, point)  # real_sorted ascending -> last wins
        else:
            by_date[day] = point

    out: list[tuple[date, object, bool]] = []
    day = start
    while day <= end:
        real = by_date.get(day)
        if real is not None:
            out.append((day, real, False))
            last = (day, real)
        elif last is not None and (day - last[0]).days <= window_days:
            out.append((day, last[1], True))
        # else: gap larger than the window / no data -> omit (real hole)
        day += timedelta(days=1)
    return out


def locf(
    real: list[DatePoint],
    start: date,
    end: date,
    window_days: int = STALE_WINDOW_DAYS,
) -> list[DatePoint]:
    """Fill ``[start, end]`` with the real per-day points, carrying the last
    value across gaps <= ``window_days`` and marking those points ``carried``.

    ``real`` should already span at least ``[start - window_days, end]`` so early
    days can be seeded from a pre-``start`` value; extra pre-``start`` points are
    not emitted. Real days are emitted with ``carried=False``.
    """
    real_sorted = sorted(
        (( _pdate(p.date), p) for p in real), key=lambda t: t[0]
    )
    return [
        DatePoint(date=emit.isoformat(), value=src.value, carried=carried)
        for emit, src, carried in _carry(real_sorted, start, end, window_days)
    ]


def locf_atlctl(
    real: list[AtlCtlPoint],
    start: date,
    end: date,
    window_days: int = STALE_WINDOW_DAYS,
) -> list[AtlCtlPoint]:
    """LOCF for the ``atl``/``ctl`` pair trend (``/atlctl``), carrying both
    fields together across a gap <= ``window_days`` and marking ``carried``."""
    real_sorted = sorted((( _pdate(p.date), p) for p in real), key=lambda t: t[0])
    return [
        AtlCtlPoint(date=emit.isoformat(), atl=src.atl, ctl=src.ctl, carried=carried)
        for emit, src, carried in _carry(real_sorted, start, end, window_days)
    ]
