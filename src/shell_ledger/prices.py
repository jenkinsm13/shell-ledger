"""Daily prices for the fake-money test, from Massive (formerly Polygon.io).

One request returns every US stock's bar for one day, so the whole market is cached a day at a time
under data/prices/. The free plan allows five requests a minute and serves finished days only.
The key is read from the environment or from data/price-key.txt. It is sent in a header and never printed.
"""
from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from . import config

HOST = "https://api.polygon.io"
GAP = 13.0  # seconds between requests: five a minute, with a margin
KEY_FILE = "price-key.txt"


class PriceError(RuntimeError):
    pass


def find_key(root: Path):
    for name in ("MASSIVE_API_KEY", "POLYGON_API_KEY"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    f = root / "data" / KEY_FILE
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return None


class Store:
    def __init__(self, root: Path, key=None, get=None, gap: float = GAP):
        self.dir = config.data_dir(root) / "prices"
        self.dir.mkdir(exist_ok=True)
        self.key = key if key is not None else find_key(root)
        self._get = get or self._http
        self._gap = gap
        self._last = 0.0
        self._days = {}
        self._actions_file = self.dir / "actions.json"
        self._actions = (
            json.loads(self._actions_file.read_text())
            if self._actions_file.exists()
            else {"through": None, "splits": {}, "dividends": {}}
        )

    # ---- reading

    def _file(self, d: date) -> Path:
        return self.dir / f"{d.isoformat()}.json.gz"

    def has(self, d: date) -> bool:
        """True when the day is cached and readable. A file cut short by an interrupted run counts as missing."""
        if d in self._days:
            return True
        if not self._file(d).exists():
            return False
        try:
            with gzip.open(self._file(d), "rt") as f:
                self._days[d] = json.load(f)
        except (OSError, EOFError, ValueError):
            return False
        return True

    def day(self, d: date) -> dict:
        return self._days[d] if self.has(d) else {}

    def bar(self, ticker: str, d: date):
        """(open, close, volume) for one ticker on one day, or None if it did not trade or the day is not cached."""
        if not self.has(d):
            return None
        r = self.day(d).get(ticker)
        return tuple(r) if r else None

    def splits(self, d: date) -> dict:
        return self._actions["splits"].get(d.isoformat(), {})

    def dividends(self, d: date) -> dict:
        return self._actions["dividends"].get(d.isoformat(), {})

    def actions_through(self):
        t = self._actions.get("through")
        return date.fromisoformat(t) if t else None

    # ---- fetching

    def _http(self, url: str) -> dict:
        wait = self._gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.key}", "User-Agent": "shell-ledger", "Accept": "application/json"}
        )
        # Short timeouts and one retry: a slow price service must never hold up the weekly ledger run.
        for attempt in range(2):
            self._last = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=12) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:300]
                if e.code == 429 and attempt == 0:
                    time.sleep(self._gap)
                    continue
                raise PriceError(f"the price service answered {e.code}: {body}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise PriceError(f"the price service could not be reached: {e}") from None
        raise PriceError("the price service kept refusing")

    def _fetch_day(self, d: date) -> bool:
        url = f"{HOST}/v2/aggs/grouped/locale/us/market/stocks/{d.isoformat()}?adjusted=false"
        rows = self._get(url).get("results") or []
        if not rows:
            return False  # not published yet
        out = {r["T"]: [r["o"], r["c"], r.get("v", 0)] for r in rows if "T" in r and "o" in r and "c" in r}
        # Written in place: some folders this runs in allow writing but not deleting or replacing files.
        with gzip.open(self._file(d), "wt") as f:
            json.dump(out, f, separators=(",", ":"))
        self._days[d] = out
        return True

    def _first_url(self, kind: str, lo: date, hi: date) -> str:
        field = {"splits": "execution_date", "dividends": "ex_dividend_date"}[kind]
        q = urllib.parse.urlencode({f"{field}.gte": lo.isoformat(), f"{field}.lte": hi.isoformat(), "limit": 1000})
        return f"{HOST}/v3/reference/{kind}?{q}"

    def _save_actions(self) -> None:
        self._actions_file.write_text(json.dumps(self._actions, separators=(",", ":")))

    def _fetch_actions(self, lo: date, hi: date, deadline: float):
        """Splits and dividends of every stock from lo to hi, a page at a time. Returns (finished, pages read).

        A week of dividends alone runs past one page. The place reached is saved after every page,
        so a run that is cut short carries on from there instead of starting the range again.
        """
        cur = self._actions.get("cursor")
        if not cur or cur["lo"] != lo.isoformat() or cur["hi"] != hi.isoformat():
            cur = {
                "lo": lo.isoformat(),
                "hi": hi.isoformat(),
                "kind": "splits",
                "url": self._first_url("splits", lo, hi),
                "found": {"splits": {}, "dividends": {}},
            }
        pages = 0
        while True:
            if time.monotonic() + self._gap > deadline:
                self._actions["cursor"] = cur
                self._save_actions()
                return False, pages
            try:
                page = self._get(cur["url"])
            except PriceError:
                self._actions.pop("cursor", None)  # the saved place may have gone stale: start the range again next time
                self._save_actions()
                raise
            pages += 1
            kind = cur["kind"]
            for r in page.get("results") or []:
                try:
                    if kind == "splits":
                        day, tk, v = r["execution_date"], r["ticker"], float(r["split_to"]) / float(r["split_from"])
                    elif r.get("currency", "USD") != "USD":
                        continue
                    else:
                        day, tk, v = r["ex_dividend_date"], r["ticker"], float(r["cash_amount"])
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    continue
                slot = cur["found"][kind].setdefault(day, {})
                slot[tk] = slot.get(tk, 0.0) + v if kind == "dividends" else slot.get(tk, 1.0) * v
            if page.get("next_url"):
                cur["url"] = page["next_url"]
            elif kind == "splits":
                cur["kind"], cur["url"] = "dividends", self._first_url("dividends", lo, hi)
            else:
                break
            self._actions["cursor"] = cur
            self._save_actions()
        for kind, by_day in cur["found"].items():
            for day, items in by_day.items():
                self._actions[kind].setdefault(day, {}).update(items)
        self._actions["through"] = hi.isoformat()
        self._actions.pop("cursor", None)
        self._save_actions()
        return True, pages

    def ensure(self, days: list, actions_from: date, max_seconds: float) -> dict:
        """Fetch what is missing, within the time allowed. Never raises: problems come back in the result."""
        out = {"key": bool(self.key), "fetched": 0, "complete": False, "error": None}
        missing = [d for d in days if not self.has(d)]
        if not self.key:
            out["complete"] = not missing and not days
            return out
        deadline = time.monotonic() + max_seconds
        try:
            for d in missing:
                if time.monotonic() + self._gap > deadline:
                    return out
                if not self._fetch_day(d):
                    out["error"] = f"prices for {d.isoformat()} are not published yet"
                    return out
                out["fetched"] += 1
            if days:
                hi = days[-1]
                lo = max(actions_from, (self.actions_through() or actions_from - timedelta(days=1)) + timedelta(days=1))
                if lo <= hi:
                    done, pages = self._fetch_actions(lo, hi, deadline)
                    out["fetched"] += pages
                    if not done:
                        return out
            out["complete"] = True
        except PriceError as e:
            out["error"] = str(e)
        return out
