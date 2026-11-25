"""The fake-money test: three pretend accounts that follow rules fixed in advance.

Orders are written before each week begins. Account values are then a pure replay of
(rules, orders, prices), recomputed from the first day on every run, so nothing can be
adjusted by hand afterwards. No real order is ever placed anywhere.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import secrets
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config, prices, readout, secweb
from .ledger import NY, next_target_monday
from .tradingdays import add_trading_days, trading_days

BOOKS = ("group_buys", "sector_tilt", "market")
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"


class RulesError(RuntimeError):
    pass


# ---- rules and calendar


def rules_sha256(root: Path) -> str:
    return hashlib.sha256((root / "paper_rules.json").read_bytes()).hexdigest()


def load_rules(root: Path) -> dict:
    digest = rules_sha256(root)
    if digest != (root / "PAPER_RULES.sha256").read_text().split()[0]:
        raise RulesError("paper_rules.json does not match PAPER_RULES.sha256. The fake-money rules were changed.")
    for f in sorted((root / "ledger" / "paper").glob("*.json")):
        if json.loads(f.read_text()).get("rules_sha256") != digest:
            raise RulesError(f"{f.name} was written under different rules. They cannot change once orders exist.")
    rules = json.loads((root / "paper_rules.json").read_text())
    rules["_sha256"] = digest
    return rules


def entry_weeks(rules: dict) -> list:
    first = date.fromisoformat(rules["first_entry_week"])
    return [first + timedelta(days=7 * k) for k in range(rules["rounds"])]


def entry_day(monday: date) -> date:
    return trading_days(monday, monday + timedelta(days=4))[0]


def lookback_days(rules: dict) -> list:
    first = entry_day(entry_weeks(rules)[0])
    n = rules["group_buys"]["volume_lookback_days"]
    return trading_days(first - timedelta(days=3 * n), first - timedelta(days=1))[-n:]


def needed_days(rules: dict, through: date) -> list:
    """Every day of prices the replay needs, up to `through`."""
    first = entry_day(entry_weeks(rules)[0])
    end = min(date.fromisoformat(rules["end_day"]), through)
    return [d for d in lookback_days(rules) if d <= through] + (trading_days(first, end) if end >= first else [])


# ---- what to buy


def ticker_map(payload: dict, exchanges: list) -> dict:
    """SEC company list -> {cik: ticker}, first listed ticker on an allowed exchange."""
    f = payload["fields"]
    ci, ti, ei = f.index("cik"), f.index("ticker"), f.index("exchange")
    out = {}
    for row in payload["data"]:
        if row[ei] in exchanges and row[ti] and int(row[ci]) not in out:
            out[int(row[ci])] = str(row[ti]).upper().replace("-", ".")
    return out


def sec_tickers(root: Path, rules: dict, get=secweb.get) -> dict:
    """The current list from the SEC, or last week's saved copy if the SEC cannot be reached."""
    cache = config.data_dir(root) / "paper" / "tickers.json"
    cache.parent.mkdir(exist_ok=True)
    try:
        raw = get(TICKERS_URL)
        payload = json.loads(raw)
        cache.write_text(json.dumps(payload))
    except Exception:
        if not cache.exists():
            raise
        payload = json.loads(cache.read_text())
    return ticker_map(payload, rules["group_buys"]["exchanges"])


def group_buy_candidates(conn, rules: dict, filed_through: date, tickers: dict) -> list:
    """Companies where several insiders bought on the open market, from filings of the last week."""
    g = rules["group_buys"]
    days = trading_days(filed_through - timedelta(days=45), filed_through)
    filed_lo = days[-g["filed_within_trading_days"]]
    trans_lo = days[-(g["traded_within_trading_days"] + 1)]
    q = (
        "SELECT t.issuer_cik, COUNT(DISTINCT t.owner_cik), SUM(t.dollars), i.sic, i.name "
        "FROM trades t JOIN issuers i ON i.cik = t.issuer_cik "
        "WHERE t.code = 'P' AND t.filed_date BETWEEN ? AND ? AND t.trans_date BETWEEN ? AND ? "
        "GROUP BY t.issuer_cik HAVING COUNT(DISTINCT t.owner_cik) >= ? AND SUM(t.dollars) >= ?"
    )
    args = (
        filed_lo.isoformat(),
        filed_through.isoformat(),
        trans_lo.isoformat(),
        filed_through.isoformat(),
        g["min_insiders"],
        g["min_dollars"],
    )
    rows = []
    for cik, n, dollars, sic, name in conn.execute(q, args):
        tk = tickers.get(int(cik))
        if sic is None or sic in g["excluded_sic"] or not tk:
            continue
        rows.append(
            {"cik": int(cik), "ticker": tk, "name": readout.company(name or tk), "insiders": int(n), "dollars": round(float(dollars))}
        )
    rows.sort(key=lambda r: (-r["insiders"], -r["dollars"], r["cik"]))
    rows = rows[: g["candidates_per_round"]]
    for k, r in enumerate(rows, 1):
        r["rank"] = k
    return rows


def sector_picks(s, fz: dict, rules: dict):
    """The sectors where insider buying is most unusual compared with insider selling."""
    m = readout.moves(s, fz)
    if m is None:
        return None
    funds = rules["sector_tilt"]["funds"]
    scored = []
    for i, k in enumerate(fz["sectors"]["order"]):
        d = m["sectors"][k]
        if k in funds and d["bought"]["beats"] is not None:
            scored.append((round(d["bought"]["beats"] - d["sold"]["beats"], 6), -i, k))
    scored.sort(reverse=True)
    top = [k for _, _, k in scored[: rules["sector_tilt"]["hold"]]]
    return {
        "sectors": top,
        "funds": [funds[k] for k in top],
        "scores": {k: sc for sc, _, k in scored},
        "window": [m["from"].isoformat(), m["to"].isoformat()],
    }


# ---- orders: private in full, public as a sealed commitment


def commit_hash(salt: str, candidates: list) -> str:
    body = json.dumps(candidates, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((salt + body).encode()).hexdigest()


def order_dir(root: Path) -> Path:
    d = config.data_dir(root) / "paper" / "orders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_orders(root: Path) -> dict:
    return {f.stem: json.loads(f.read_text()) for f in sorted(order_dir(root).glob("*.json"))}


def write_orders(root: Path, rules: dict, conn, s, fz: dict, filed_through: date, tickers: dict, now=None):
    """Write next week's orders, once. Returns (path, None) or (None, reason)."""
    now = now or datetime.now(timezone.utc)
    monday = next_target_monday(now)
    if monday not in entry_weeks(rules):
        return None, "outside the test window"
    private = order_dir(root) / f"{monday.isoformat()}.json"
    public = config.ledger_dir(root, "paper") / f"{monday.isoformat()}.json"
    if private.exists() or public.exists():
        return None, "already written"
    eday = entry_day(monday)
    if add_trading_days(filed_through, rules["orders"]["max_stale_trading_days"]) < eday:
        return None, f"filings are loaded only through {filed_through}: too old to write orders from"
    candidates = group_buy_candidates(conn, rules, filed_through, tickers)
    picks = sector_picks(s, fz, rules)
    salt = secrets.token_hex(16)
    head = {
        "entry_week": monday.isoformat(),
        "entry_day": eday.isoformat(),
        "written_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "filed_through": filed_through.isoformat(),
        "rules_sha256": rules["_sha256"],
    }
    order = {"kind": "paper_orders", **head, "salt": salt, "group_buys": candidates, "sector_tilt": picks}
    commitment = {
        "kind": "paper_commitment",
        **head,
        "group_buys": {
            "candidates": len(candidates),
            "sha256": commit_hash(salt, candidates),
            "note": "company names are sealed; the hash proves the list was fixed before the week began",
        },
        "sector_tilt": {"funds": picks["funds"] if picks else []},
    }
    private.write_text(json.dumps(order, indent=1) + "\n")
    public.write_text(json.dumps(commitment, indent=1) + "\n")
    return private, None


# ---- the replay


def replay(rules: dict, orders: dict, px, through: date) -> dict:
    """Run all three accounts from the first day to `through`, using only cached prices."""
    start = float(rules["start_cash"])
    weeks = entry_weeks(rules)
    entry = {entry_day(w): w for w in weeks}
    first, end = entry_day(weeks[0]), date.fromisoformat(rules["end_day"])
    g = rules["group_buys"]
    cs, cf = rules["cost"]["stock"], rules["cost"]["fund"]
    spy = rules["market"]["fund"]
    cash = {b: start for b in BOOKS}
    pos = {b: {} for b in BOOKS}
    trades, skipped, daily = [], [], []
    final = False

    def buy(book, tk, dollars, price, d, cost, exit_day=None):
        cash[book] -= dollars * (1 + cost)
        pos[book][tk] = {"shares": dollars / price, "entry_day": d, "entry_px": price, "last": price, "exit_day": exit_day}
        trades.append({"day": d, "book": book, "side": "buy", "ticker": tk, "price": price})

    def sell(book, tk, price, d, cost, why):
        p = pos[book].pop(tk)
        cash[book] += p["shares"] * price * (1 - cost)
        trades.append(
            {"day": d, "book": book, "side": "sell", "ticker": tk, "price": price, "gain": price / p["entry_px"] - 1, "why": why}
        )

    def refuse(tk, d):
        """Why a group-buy order cannot fill today, judged only on what was known before the open."""
        if tk in pos["group_buys"]:
            return "already held"
        if len(pos["group_buys"]) >= g["max_positions"]:
            return "account full"
        if cash["group_buys"] < g["position_dollars"] * (1 + cs):
            return "not enough cash"
        bar = px.bar(tk, d)
        if not bar or not bar[0] > 0:
            return "did not trade that day"
        before = trading_days(d - timedelta(days=3 * g["volume_lookback_days"]), d - timedelta(days=1))
        prior = [b for b in (px.bar(tk, p) for p in before[-g["volume_lookback_days"] :]) if b]
        if len(prior) < g["min_price_days"]:
            return "too little price history"
        if prior[-1][1] < g["min_prior_close"]:
            return f"priced under ${g['min_prior_close']:.0f}"
        if statistics.median(c * v for _, c, v in prior) < g["min_median_dollar_volume"]:
            return "too thinly traded"
        return None

    def rebalance(funds, d):
        book = "sector_tilt"
        opens = {tk: px.bar(tk, d) for tk in set(funds) | set(pos[book])}
        if not all(opens.values()):
            skipped.append({"day": d, "book": book, "ticker": ",".join(funds), "why": "a fund did not trade; holdings left as they were"})
            return
        cur = {tk: p["shares"] * opens[tk][0] for tk, p in pos[book].items()}
        total = cash[book] + sum(cur.values())
        target = {tk: total / len(funds) for tk in funds}
        traded = sum(abs(target.get(tk, 0.0) - cur.get(tk, 0.0)) for tk in set(target) | set(cur))
        scale = (total - traded * cf) / total
        new = {}
        for tk in funds:
            old = pos[book].get(tk)
            price = opens[tk][0]
            new[tk] = {
                "shares": target[tk] * scale / price,
                "entry_day": old["entry_day"] if old else d,
                "entry_px": old["entry_px"] if old else price,
                "last": price,
                "exit_day": None,
            }
            if not old:
                trades.append({"day": d, "book": book, "side": "buy", "ticker": tk, "price": price})
        for tk, old in pos[book].items():
            if tk not in new:
                price = opens[tk][0]
                trades.append(
                    {"day": d, "book": book, "side": "sell", "ticker": tk, "price": price, "gain": price / old["entry_px"] - 1, "why": "dropped out of the top three"}
                )
        pos[book], cash[book] = new, 0.0

    ready = all(px.has(d) for d in lookback_days(rules))
    for d in trading_days(first, min(end, through)) if ready else []:
        if not px.has(d):
            break
        sp, dv = px.splits(d), px.dividends(d)
        for b in BOOKS:
            for tk, p in pos[b].items():
                if tk in sp:
                    p["shares"] *= sp[tk]
                    p["entry_px"] /= sp[tk]
                    p["last"] /= sp[tk]
                if tk in dv:
                    cash[b] += p["shares"] * dv[tk]
        for tk in [t for t, p in pos["group_buys"].items() if p["exit_day"] <= d]:
            bar = px.bar(tk, d)
            if bar and bar[0] > 0:
                sell("group_buys", tk, bar[0], d, cs, "held four weeks")
        order = orders.get(entry[d].isoformat()) if d in entry else None
        if order:
            filled = 0
            for c in order.get("group_buys") or []:
                if filled >= g["new_positions_per_round"]:
                    break
                why = refuse(c["ticker"], d)
                if why:
                    skipped.append({"day": d, "book": "group_buys", "ticker": c["ticker"], "why": why})
                    continue
                buy("group_buys", c["ticker"], g["position_dollars"], px.bar(c["ticker"], d)[0], d, cs, add_trading_days(d, g["hold_trading_days"]))
                filled += 1
            funds = (order.get("sector_tilt") or {}).get("funds") or []
            if funds:
                rebalance(funds, d)
        if not pos["market"] and cash["market"] > 1:
            bar = px.bar(spy, d)
            if bar and bar[0] > 0:
                buy("market", spy, cash["market"] / (1 + cf), bar[0], d, cf)
        for b in BOOKS:
            for tk, p in pos[b].items():
                bar = px.bar(tk, d)
                if bar:
                    p["last"] = bar[1]
        if d == end:
            for b in BOOKS:
                for tk in list(pos[b]):
                    sell(b, tk, pos[b][tk]["last"], d, cs if b == "group_buys" else cf, "end of the test")
            final = True
        daily.append((d, {b: cash[b] + sum(p["shares"] * p["last"] for p in pos[b].values()) for b in BOOKS}))

    by_day = dict(daily)
    weekly, prev = [], {b: start for b in BOOKS}
    for k, w in enumerate(weeks, 1):
        last = trading_days(w, w + timedelta(days=4))[-1]
        if last not in by_day:
            break
        v = by_day[last]
        weekly.append({"week": k, "day": last, "values": v, "returns": {b: v[b] / prev[b] - 1 for b in BOOKS}})
        prev = v

    def read(book):
        diffs = [w["returns"][book] - w["returns"]["market"] for w in weekly]
        t = None
        if len(diffs) >= 4 and statistics.stdev(diffs) > 0:
            t = statistics.mean(diffs) / (statistics.stdev(diffs) / math.sqrt(len(diffs)))
        verdict = "too early to say"
        if final:
            mine, market, bar = daily[-1][1][book], daily[-1][1]["market"], rules["reading"]["t"]
            if t is not None and mine > market and t >= bar:
                verdict = "ahead of the market"
            elif t is not None and mine < market and t <= -bar:
                verdict = "behind the market"
            else:
                verdict = "no clear difference from the market"
        return {"weeks": len(diffs), "t": t, "verdict": verdict}

    values = daily[-1][1] if daily else {b: start for b in BOOKS}
    return {
        "status": "final" if final else "running" if daily else "waiting",
        "as_of": daily[-1][0] if daily else None,
        "rounds": len(weeks),
        "start": start,
        "values": values,
        "positions": {
            b: [
                {"ticker": tk, "entry_day": p["entry_day"], "entry_px": p["entry_px"], "last": p["last"], "gain": p["last"] / p["entry_px"] - 1}
                for tk, p in pos[b].items()
            ]
            for b in BOOKS
        },
        "trades": trades,
        "skipped": skipped,
        "weekly": weekly,
        "reading": {b: read(b) for b in ("group_buys", "sector_tilt")},
    }


# ---- plain words


def money(v: float) -> str:
    return f"${v:,.0f}"


def moved(r: float) -> str:
    return f"{'up' if r >= 0 else 'down'} {abs(r):.1%}"


def lines(rules: dict, summary: dict, orders: dict, private: bool, today: date, note: str | None = None) -> list:
    """The fake-money section of the weekly message. The public version names no company."""
    weeks = entry_weeks(rules)
    end = date.fromisoformat(rules["end_day"])
    names = {c["ticker"]: c["name"] for o in orders.values() for c in (o.get("group_buys") or [])}
    sector_of = {v: k for k, v in rules["sector_tilt"]["funds"].items()}

    def fund(tk):
        return readout.SECTOR.get(sector_of.get(tk), tk)

    def listed(items):
        items = list(items)
        return ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else "".join(items)

    as_of, start = summary["as_of"], summary["start"]
    if summary["status"] == "final":
        when = f"finished {readout.day(end)}"
    elif as_of:
        when = f"week {sum(w <= as_of for w in weeks)} of {summary['rounds']}, through {readout.day(as_of)}; ends {readout.day(end)}"
    else:
        when = f"first buys {readout.day(entry_day(weeks[0]))}; ends {readout.day(end)}"
    L = [f"**Fake-money test (three pretend accounts, {money(start)} each; {when})**", ""]

    if as_of:
        v = summary["values"]
        held = summary["positions"]["group_buys"]
        line = f"- Group buys: {money(v['group_buys'])} ({moved(v['group_buys'] / start - 1)})."
        if held and private:
            line += " Holding " + ", ".join(f"{names.get(p['ticker'], p['ticker'])} ({p['gain']:+.1%})" for p in held) + "."
        elif held:
            line += f" Holding {len(held)} stock{'s' if len(held) != 1 else ''}."
        L.append(line)
        line = f"- Sector tilt: {money(v['sector_tilt'])} ({moved(v['sector_tilt'] / start - 1)})."
        if summary["positions"]["sector_tilt"]:
            line += f" In the {listed(fund(p['ticker']) for p in summary['positions']['sector_tilt'])} funds."
        L.append(line)
        L.append(f"- Just holding the market: {money(v['market'])} ({moved(v['market'] / start - 1)}).")
        since = as_of - timedelta(days=7)
        recent = [t for t in summary["trades"] if t["book"] == "group_buys" and t["day"] > since]
        skips = [x for x in summary["skipped"] if x.get("book") == "group_buys" and x["day"] > since]
        bought = [t for t in recent if t["side"] == "buy"]
        sold = [t for t in recent if t["side"] == "sell"]
        if private and (bought or sold or skips):
            parts = []
            if bought:
                parts.append("bought " + ", ".join(names.get(t["ticker"], t["ticker"]) for t in bought))
            if sold:
                parts.append("sold " + ", ".join(f"{names.get(t['ticker'], t['ticker'])} ({t['gain']:+.1%})" for t in sold))
            if skips:
                parts.append("passed on " + ", ".join(f"{names.get(x['ticker'], x['ticker'])} ({x['why']})" for x in skips))
            L.append("- This week in Group buys: " + "; ".join(parts) + ".")
        elif bought or sold or skips:
            L.append(f"- This week in Group buys: {len(bought)} bought, {len(sold)} sold, {len(skips)} passed on.")
        if summary["status"] == "final":
            r = summary["reading"]
            L.append(
                f"- Final reading: Group buys ended {r['group_buys']['verdict']}; "
                f"Sector tilt ended {r['sector_tilt']['verdict']}."
            )
        else:
            L.append(f"- Reading: too early to say. It is read once, after {readout.day(end)}.")
    if note:
        L.append(f"- {note}")

    upcoming = [o for o in orders.values() if date.fromisoformat(o["entry_day"]) >= today]
    if upcoming and summary["status"] != "final":
        o = upcoming[0]
        cands = o.get("group_buys") or []
        n = rules["group_buys"]["new_positions_per_round"]
        if not cands:
            text = "Group buys: nothing to buy, no company had enough insiders buying."
        elif private:
            text = "Group buys will try, in this order: " + ", ".join(c["name"] for c in cands[:n])
            if cands[n:]:
                text += "; backups " + ", ".join(c["name"] for c in cands[n:])
            text += "."
        else:
            text = f"Group buys: {len(cands)} companies, names sealed."
        funds = (o.get("sector_tilt") or {}).get("funds") or []
        if funds:
            text += f" Sector tilt: {listed(fund(tk) for tk in funds)} funds."
        L.append(f"- Orders for {readout.day(date.fromisoformat(o['entry_day']))}, written and sealed in advance. {text}")
    L.append("")
    return L


# ---- one weekly step


def _notes(rules: dict, summary: dict, res: dict, today: date):
    """(private note, public note) about why values are missing or behind, or (None, None)."""
    first = entry_day(entry_weeks(rules)[0])
    if not res["key"]:
        return (
            "No account values yet: the price key is not in place. Paste it into data/price-key.txt "
            "and everything fills in from the first day. Nothing is lost in the meantime.",
            "No account values yet: prices are not connected.",
        )
    if res["error"]:
        text = f"Prices could not be brought up to date ({res['error']})."
        return text, "Prices could not be brought up to date."
    if not res["complete"]:
        return ("Prices are still loading.",) * 2
    if summary["status"] == "waiting":
        text = f"No account values yet: the first buys happen at the open on {readout.day(first)}."
        return text, text
    return None, None


def view(root: Path, now=None, store=None, res=None, info=None) -> dict:
    """Replay from what is already on disk and put it into words. Fetches nothing."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(NY).date()
    rules = load_rules(root)
    store = store or prices.Store(root)
    through = today - timedelta(days=1)
    if res is None:
        days = needed_days(rules, through)
        res = {"key": bool(store.key), "fetched": 0, "error": None, "complete": all(store.has(d) for d in days)}
    orders = load_orders(root)
    summary = replay(rules, orders, store, through)
    private_note, public_note = _notes(rules, summary, res, today)
    return {
        "rules": rules,
        "summary": summary,
        "orders": orders,
        "prices": res,
        "info": info or {},
        "private": lines(rules, summary, orders, True, today, private_note),
        "public": lines(rules, summary, orders, False, today, public_note),
        "more": bool(res["key"] and not res["complete"] and not res["error"] and res["fetched"] > 0),
    }


def step(root: Path, conn, fz: dict, s, filed_through: date, max_seconds: float = 60.0, now=None, store=None, tickers=None) -> dict:
    """Write next week's orders if they are due, bring prices up to date, replay, and report."""
    now = now or datetime.now(timezone.utc)
    rules = load_rules(root)
    info = {}
    try:
        if tickers is None:
            tickers = sec_tickers(root, rules)
        path, why = write_orders(root, rules, conn, s, fz, filed_through, tickers, now)
        info["orders"] = f"wrote {path.name}" if path else why
    except Exception as e:  # the fake-money test must never stop the real ledger
        info["orders"] = f"not written: {e}"
    store = store or prices.Store(root)
    through = now.astimezone(NY).date() - timedelta(days=1)
    res = store.ensure(needed_days(rules, through), entry_day(entry_weeks(rules)[0]), max_seconds)
    return view(root, now, store, res, info)


def rehearse(root: Path, conn, fz: dict, s, first_week: date, max_seconds: float, now=None, store=None, tickers=None) -> dict:
    """The same rules over thirteen past weeks. A check of the machinery; it decides nothing."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(NY).date()
    rules = dict(load_rules(root))
    rules["first_entry_week"] = first_week.isoformat()
    weeks = entry_weeks(rules)
    rules["end_day"] = trading_days(weeks[-1], weeks[-1] + timedelta(days=4))[-1].isoformat()
    if tickers is None:
        tickers = sec_tickers(root, rules)
    k = fz["data"]["timely_trading_days"]
    orders = {}
    for w in weeks:
        f = trading_days(w - timedelta(days=10), w - timedelta(days=1))[-1]  # the last trading day before the week
        asof = copy.copy(s)
        asof.valid = max((i for i in range(len(s.days) - k) if s.days[i + k] <= f), default=-1)
        orders[w.isoformat()] = {
            "entry_week": w.isoformat(),
            "entry_day": entry_day(w).isoformat(),
            "filed_through": f.isoformat(),
            "group_buys": group_buy_candidates(conn, rules, f, tickers),
            "sector_tilt": sector_picks(asof, fz, rules),
        }
    store = store or prices.Store(root)
    through = today - timedelta(days=1)
    res = store.ensure(needed_days(rules, through), entry_day(weeks[0]), max_seconds)
    summary = replay(rules, orders, store, through)
    note = _notes(rules, summary, res, today)[0]
    text = lines(rules, summary, orders, True, date.max, note)
    text[0] = text[0].replace("Fake-money test", "Rehearsal on past weeks, not the test")
    return {"summary": summary, "orders": orders, "prices": res, "private": text, "more": bool(res["key"] and not res["complete"] and not res["error"] and res["fetched"] > 0)}
