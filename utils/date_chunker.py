"""
utils/date_chunker.py

Splits a large historical date range into bounded windows so a multi-
hundred-thousand-row backfill is pulled in resumable, timeout-resistant
slices instead of one unbounded request.

Two responsibilities:
- iter_windows(): yield half-open [start, end) windows at a given
  granularity (monthly / weekly / daily / none).
- build_range_filter(): turn a (field, lower, upper) range into an OData
  $filter string, formatting the literal correctly for an Edm.Date field
  (``2024-01-01``) vs an Edm.DateTimeOffset field (``2024-01-01T00:00:00Z``).

Keeping the literal formatting here (rather than scattered through the
sync service) is deliberate: BC rejects a date column compared against a
``...Z`` datetime literal and vice-versa, and that mismatch has bitten
this pipeline before.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterator, Union

DateLike = Union[date, datetime]

VALID_GRANULARITIES = {"monthly", "weekly", "daily", "none"}


def _first_of_next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def iter_windows(start: date, end: date, granularity: str = "monthly") -> Iterator[tuple[date, date]]:
    """
    Yield consecutive half-open [window_start, window_end) windows that
    together cover [start, end). ``end`` is exclusive — pass today+1 day if
    you want today included.

    granularity:
      - "monthly": calendar-month aligned after the first (partial) window
      - "weekly":  7-day windows
      - "daily":   1-day windows
      - "none":    a single [start, end) window

    Returns nothing if start >= end.
    """
    if granularity not in VALID_GRANULARITIES:
        raise ValueError(f"Unknown granularity {granularity!r}; expected one of {sorted(VALID_GRANULARITIES)}")
    if start >= end:
        return

    if granularity == "none":
        yield (start, end)
        return

    cur = start
    while cur < end:
        if granularity == "monthly":
            nxt = _first_of_next_month(cur)
        elif granularity == "weekly":
            nxt = cur + timedelta(days=7)
        else:  # daily
            nxt = cur + timedelta(days=1)
        window_end = min(nxt, end)
        yield (cur, window_end)
        cur = window_end


def _literal(value: DateLike, is_datetime: bool) -> str:
    """Format a date/datetime as an OData filter literal."""
    if is_datetime:
        # Edm.DateTimeOffset — BC expects an explicit UTC 'Z' suffix.
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%dT%H:%M:%SZ")
        return datetime(value.year, value.month, value.day).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Edm.Date — plain YYYY-MM-DD, no quotes, no time component.
    return value.strftime("%Y-%m-%d")


def build_range_filter(
    field: str,
    lower: DateLike | None,
    upper: DateLike | None,
    is_datetime: bool = False,
) -> str | None:
    """
    Build ``field ge <lower> and field lt <upper>``. Either bound may be
    None (open-ended). Returns None if both are None.
    """
    clauses: list[str] = []
    if lower is not None:
        clauses.append(f"{field} ge {_literal(lower, is_datetime)}")
    if upper is not None:
        clauses.append(f"{field} lt {_literal(upper, is_datetime)}")
    return " and ".join(clauses) if clauses else None
