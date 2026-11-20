"""Daily sector series built from the local store, under the frozen counting rules."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from .tradingdays import trading_days


def sector_table(fz: dict):
    order = fz["sectors"]["order"]
    spans = [(lo, hi, order.index(name)) for name, rs in fz["sectors"]["ranges"].items() for lo, hi in rs]
    other = order.index("Other")

    def sector_of(sic):
        if sic is None:
            return other
        for lo, hi, i in spans:
            if lo <= sic <= hi:
                return i
        return other

    return order, sector_of


@dataclass
class Series:
    days: list  # trading days, ascending
    D: np.ndarray  # dollars, sectors x days
    BUY: np.ndarray
    SELL: np.ndarray
    valid: int  # index of the last settled day, or -1
    floor: float

    @property
    def L(self):
        return np.log10(self.D + self.floor)

    @property
    def Lall(self):
        return np.log10(self.D.sum(axis=0) + self.floor)

    def index_of(self, d: date):
        return self._idx.get(d)

    def __post_init__(self):
        self._idx = {d: i for i, d in enumerate(self.days)}


def qualifying(conn, fz: dict, days: list):
    """Yield (day_index, code, dollars, owner_cik, sic) for rows that count."""
    k = fz["data"]["timely_trading_days"]
    iso = [d.isoformat() for d in days]
    idx = {s: i for i, s in enumerate(iso)}
    q = (
        "SELECT t.trans_date, t.filed_date, t.code, t.dollars, t.owner_cik, i.sic "
        "FROM trades t LEFT JOIN issuers i ON i.cik = t.issuer_cik"
    )
    for td, fd, code, dollars, owner, sic in conn.execute(q):
        i = idx.get(td)
        if i is None or i + k >= len(iso):
            continue  # not a trading day, or its filing window is not on the calendar yet
        if fd < td or fd > iso[i + k]:
            continue  # filed before the trade, or not timely
        yield i, code, dollars, owner, sic


def build(conn, fz: dict, filed_through: date, today: date | None = None) -> Series:
    """Build the series. `filed_through` is the last filing date fully loaded."""
    today = today or date.today()
    start = date.fromisoformat(fz["data"]["history_start"])
    days = trading_days(start, today + timedelta(days=14))
    order, sector_of = sector_table(fz)
    n, m = len(order), len(days)
    D, BUY, SELL = np.zeros((n, m)), np.zeros((n, m)), np.zeros((n, m))
    for i, code, dollars, _owner, sic in qualifying(conn, fz, days):
        s = sector_of(sic)
        D[s, i] += dollars
        (BUY if code == "P" else SELL)[s, i] += dollars
    k = fz["data"]["timely_trading_days"]
    valid = -1
    for i in range(m - k):
        if days[i + k] <= filed_through:
            valid = i
        else:
            break
    return Series(days, D, BUY, SELL, valid, float(fz["series"]["floor_dollars"]))
