"""The predictors. Everything here follows frozen.json; nothing is tuned."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np

PHI = (1 + 5 ** 0.5) / 2
LN_PHI = math.log(PHI)


def w_ck(m: int) -> float:
    return math.exp(-m / PHI) / (1 + math.cos(2 * m * LN_PHI) ** 2)


def w_fade(m: int) -> float:
    return math.exp(-m / PHI)


def w_equal(m: int) -> float:
    return 1.0


WEIGHTS = {"ck": w_ck, "fade": w_fade, "equal": w_equal}
BLENDS = [(g, w) for g in ("phi", "dbl") for w in ("ck", "fade", "equal")]
NAMES = [f"{g}_{w}" for g, w in BLENDS] + ["baseline_A", "baseline_B", "har"]
HAR_WINDOWS = (1, 5, 22)
HAR_MIN_WEEKS = 52


class Model:
    def __init__(self, series, fz: dict):
        self.s = series
        self.fz = fz
        self.L = series.L
        self.cs = np.concatenate([np.zeros((self.L.shape[0], 1)), np.cumsum(self.L, axis=1)], axis=1)
        self.grids = {
            "phi": (fz["grids"]["phi"]["windows"], fz["grids"]["phi"]["target_shell"]),
            "dbl": (fz["grids"]["doubling"]["windows"], fz["grids"]["doubling"]["target_shell"]),
        }
        self.longest = max(self.grids["phi"][0])
        self._har = {}

    # ---- calendar helpers -------------------------------------------------
    def week_idx(self, monday: date) -> list:
        """Indices of the trading days in the week starting `monday`."""
        out = []
        for k in range(5):
            i = self.s.index_of(monday + timedelta(days=k))
            if i is not None:
                out.append(i)
        return out

    def cutoff(self, monday: date):
        """Index of the last day the forecast for this week may use, or None."""
        d = monday - timedelta(days=1)
        first = self.s.days[0]
        while d >= first and self.s.index_of(d) is None:
            d -= timedelta(days=1)
        p = self.s.index_of(d)
        if p is None:
            return None
        e = p - self.fz["data"]["timely_trading_days"]
        return e if e >= 0 else None

    def settled(self, monday: date) -> bool:
        idx = self.week_idx(monday)
        return bool(idx) and idx[-1] <= self.s.valid

    # ---- pieces -----------------------------------------------------------
    def wmean(self, e: int, w: int):
        return (self.cs[:, e + 1] - self.cs[:, e + 1 - w]) / w

    def target(self, monday: date):
        idx = self.week_idx(monday)
        return self.L[:, idx].mean(axis=1)

    def blend(self, e: int, grid: str, weight: str):
        windows, j = self.grids[grid]
        wf = WEIGHTS[weight]
        num, den = 0.0, 0.0
        for k, w in enumerate(windows):
            c = wf(abs(j - k))
            num = num + c * self.wmean(e, w)
            den += c
        return num / den

    def _har_row(self, monday: date):
        """(features, target, last index of the week) at the week's own cutoff."""
        if monday not in self._har:
            e, idx = self.cutoff(monday), self.week_idx(monday)
            if e is None or e + 1 < max(HAR_WINDOWS) or not idx:
                self._har[monday] = None
            else:
                x = np.stack([np.ones(self.L.shape[0])] + [self.wmean(e, w) for w in HAR_WINDOWS], axis=1)
                self._har[monday] = (x, idx[-1], self.L[:, idx].mean(axis=1))
        return self._har[monday]

    def har(self, monday: date):
        """OLS per sector on every earlier week whose target was settled at issue time."""
        row = self._har_row(monday)
        if row is None:
            return None
        d = monday - timedelta(days=1)
        while self.s.index_of(d) is None:
            d -= timedelta(days=1)
        p = self.s.index_of(d)
        k = self.fz["data"]["timely_trading_days"]
        xs, ys = [], []
        m = monday - timedelta(days=14)
        first = self.s.days[0]
        while m >= first:
            r = self._har_row(m)
            if r is not None and r[1] + k <= p:
                xs.append(r[0])
                ys.append(r[2])
            m -= timedelta(days=7)
        if len(xs) < HAR_MIN_WEEKS:
            return None
        X, Y = np.stack(xs), np.stack(ys)  # weeks x sectors x 4, weeks x sectors
        out = np.empty(self.L.shape[0])
        for s in range(self.L.shape[0]):
            beta, *_ = np.linalg.lstsq(X[:, s, :], Y[:, s], rcond=None)
            out[s] = row[0][s] @ beta
        return out

    # ---- the forecast -----------------------------------------------------
    def forecast(self, monday: date, with_har: bool = True):
        """All predictors for the week starting `monday`. None if history is too short."""
        e = self.cutoff(monday)
        if e is None or e + 1 < self.longest:
            return None
        out = {f"{g}_{w}": self.blend(e, g, w) for g, w in BLENDS}
        out["baseline_A"] = self.wmean(e, self.longest)
        out["baseline_B"] = self.wmean(e, 5)
        if with_har:
            h = self.har(monday)
            if h is not None:
                out["har"] = h
        return out


def dm_stat(d, lags: int = 4):
    """Mean of d over its HAC standard error (Bartlett kernel). Returns (mean, t, n)."""
    d = np.asarray(d, dtype=float)
    n = len(d)
    if n < 3:
        return (float(d.mean()) if n else float("nan"), float("nan"), n)
    u = d - d.mean()
    var = float(u @ u) / n
    for l in range(1, min(lags, n - 1) + 1):
        var += 2 * (1 - l / (lags + 1)) * float(u[l:] @ u[:-l]) / n
    if var <= 0:
        return float(d.mean()), float("nan"), n
    return float(d.mean()), float(d.mean() / math.sqrt(var / n)), n


def verdict(t: float, fz: dict) -> str:
    if t != t:
        return "not enough weeks"
    if t > 1.645:
        return "the shell model wins"
    if t < -1.645:
        return "the shell model loses"
    return "tie: no detectable work from the golden ratio or the curve"
