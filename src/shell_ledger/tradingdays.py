"""NYSE trading days by rule. The rule set is frozen (see frozen.json, "calendar")."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

ONE_OFF_CLOSURES = frozenset(
    {
        date(2007, 1, 2),
        date(2012, 10, 29),
        date(2012, 10, 30),
        date(2018, 12, 5),
        date(2025, 1, 9),
    }
)


def easter(y: int) -> date:
    """Easter Sunday, anonymous Gregorian algorithm."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, m: int, weekday: int) -> date:
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date, saturday_to_friday: bool = True):
    if d.weekday() == 5:
        return d - timedelta(days=1) if saturday_to_friday else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def holidays(y: int) -> frozenset:
    h = set()
    ny = _observed(date(y, 1, 1), saturday_to_friday=False)
    if ny:
        h.add(ny)
    h.add(_nth_weekday(y, 1, 0, 3))  # Martin Luther King Jr. Day
    h.add(_nth_weekday(y, 2, 0, 3))  # Presidents Day
    h.add(easter(y) - timedelta(days=2))  # Good Friday
    h.add(_last_weekday(y, 5, 0))  # Memorial Day
    if y >= 2022:
        h.add(_observed(date(y, 6, 19)))  # Juneteenth
    h.add(_observed(date(y, 7, 4)))
    h.add(_nth_weekday(y, 9, 0, 1))  # Labor Day
    h.add(_nth_weekday(y, 11, 3, 4))  # Thanksgiving
    h.add(_observed(date(y, 12, 25)))
    return frozenset(h)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year) and d not in ONE_OFF_CLOSURES


def trading_days(start: date, end: date) -> list:
    """All trading days from start to end, inclusive."""
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def add_trading_days(d: date, n: int) -> date:
    """The n-th trading day after d (n > 0)."""
    while n > 0:
        d += timedelta(days=1)
        if is_trading_day(d):
            n -= 1
    return d


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())
