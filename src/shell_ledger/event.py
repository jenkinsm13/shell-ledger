"""The event test: is this season's activity the largest uplift in the record?"""
from __future__ import annotations

import math
from datetime import date

import numpy as np

from .series import Series, qualifying

WINDOW_START = (11, 20)
WINDOW_END = (12, 31)
REF_START = (1, 2)
REF_END = (11, 19)


def _span(series: Series, y: int, a, b, upto: int):
    lo, hi = date(y, *a), date(y, *b)
    return [i for i, d in enumerate(series.days) if lo <= d <= hi and i <= upto]


def uplift(series: Series, fz: dict) -> dict:
    """U[y] for every year in the frozen range, using settled days only."""
    y0, y1 = fz["event_test"]["years"]
    lall = series.Lall
    out = {}
    for y in range(y0, y1 + 1):
        win = _span(series, y, WINDOW_START, WINDOW_END, series.valid)
        ref = _span(series, y, REF_START, REF_END, series.valid)
        if win and ref:
            out[y] = {
                "uplift": round(float(lall[win].mean() - lall[ref].mean()), 6),
                "window_days": len(win),
                "reference_days": len(ref),
            }
    return out


def same_filers(conn, series: Series, fz: dict) -> dict:
    """V[y]: participation of a fixed three-year panel, window against reference."""
    y1 = fz["event_test"]["years"][1]
    days = series.days
    active = {}  # year -> owners with any qualifying row
    in_win, in_ref = {}, {}
    for i, _code, _dollars, owner, _sic in qualifying(conn, fz, days):
        if owner is None or i > series.valid:
            continue
        d = days[i]
        active.setdefault(d.year, set()).add(owner)
        md = (d.month, d.day)
        if WINDOW_START <= md <= WINDOW_END:
            in_win.setdefault(d.year, set()).add(owner)
        elif REF_START <= md <= REF_END:
            in_ref.setdefault(d.year, set()).add(owner)
    out = {}
    for y in range(2009, y1 + 1):
        if not all(k in active for k in (y - 3, y - 2, y - 1)):
            continue
        panel = active[y - 3] & active[y - 2] & active[y - 1]
        a = len(panel & in_win.get(y, set()))
        b = len(panel & in_ref.get(y, set()))
        if panel and a and b:
            out[y] = {"panel": len(panel), "in_window": a, "in_reference": b, "v": round(math.log(a / b), 6)}
    return out


def _rank(values: dict, key: str, year: int):
    if year not in values:
        return None
    ordered = sorted(values, key=lambda y: values[y][key], reverse=True)
    return ordered.index(year) + 1


def run(conn, series: Series, fz: dict) -> dict:
    y1 = fz["event_test"]["years"][1]
    u = uplift(series, fz)
    v = same_filers(conn, series, fz)
    last_settled = series.days[series.valid] if series.valid >= 0 else None
    window_last = max(d for d in series.days if d <= date(y1, *WINDOW_END))
    final = last_settled is not None and last_settled >= window_last
    rank = _rank(u, "uplift", y1)
    return {
        "kind": "event_test",
        "claim": fz["event_test"]["claim"],
        "frozen_sha256": fz["_sha256"],
        "status": "final" if final else "interim",
        "settled_through": last_settled.isoformat() if last_settled else None,
        "years_compared": len(u),
        "rank_of_this_year": rank,
        "passes": (rank == 1 and len(u) == y1 - fz["event_test"]["years"][0] + 1) if final else None,
        "uplift_by_year": {str(y): u[y] for y in sorted(u)},
        "same_filers": {
            "rank_of_this_year": _rank(v, "v", y1),
            "years_compared": len(v),
            "by_year": {str(y): v[y] for y in sorted(v)},
            "decides": "nothing; reported only",
        },
    }
