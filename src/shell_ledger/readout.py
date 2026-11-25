"""The money-moves readout: where insider dollars went, and what is expected next week.

Two texts come out of it. The public one carries sector totals only and goes in the ledger.
The weekly message adds the largest moves by company and stays under data/, which is never committed.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from . import config
from .series import Series
from .tradingdays import add_trading_days, trading_days

SECTOR = {
    "NoDur": "Consumer staples",
    "Durbl": "Cars and appliances",
    "Manuf": "Manufacturing",
    "Enrgy": "Energy",
    "Chems": "Chemicals",
    "BusEq": "Tech",
    "Telcm": "Telecom",
    "Utils": "Utilities",
    "Shops": "Retail",
    "Hlth": "Health and pharma",
    "Money": "Finance",
    "Other": "Everything else",
}
WINDOW = 10  # trading days: "the last two weeks"
LOOKBACK = 504  # trading days: the two years that define "usual"
HEAVY, LIGHT = 0.85, 0.15  # share of past stretches this one beats
MORE, LESS = 1.33, 0.75  # expected level against usual
SHORT_WORDS = {"INC", "CORP", "CO", "LTD", "PLC", "THE", "AND", "OF", "FOR", "NEW"}


def usd(x) -> str:
    x = float(x)
    if x >= 9.995e9:
        return f"${x / 1e9:,.1f} billion"
    if x >= 999.5e6:
        return f"${x / 1e9:.2f} billion"
    if x >= 999.5e3:
        return f"${x / 1e6:,.0f} million"
    if x >= 1e3:
        return f"${x / 1e3:,.0f} thousand"
    return f"${x:,.0f}"


def day(d: date) -> str:
    return f"{d:%b} {d.day}"


def times(x: float) -> str:
    return f"{x:.1f}" if x >= 1 else f"{x:.2f}"


def company(name: str) -> str:
    """SEC names are in capitals. Lower the long words, leave short ones (IBM, LLC) alone."""
    words = []
    for w in str(name).split():
        core = w.strip(".,")
        words.append(w.capitalize() if w.isupper() and (len(core) > 3 or core in SHORT_WORDS) else w)
    return " ".join(words)


def _roll(a, w: int):
    c = np.cumsum(np.concatenate([[0.0], a]))
    return c[w:] - c[:-w]


def stretch(a, lo: int, hi: int) -> dict:
    """Total of a[lo..hi], against stretches of the same length in the two years before."""
    now = float(a[lo : hi + 1].sum())
    past = _roll(a[max(lo - LOOKBACK, 0) : lo], hi - lo + 1)
    if len(past) == 0:
        return {"now": now, "usual": None, "beats": None}
    return {"now": now, "usual": float(np.median(past)), "beats": float((past < now).mean())}


def moves(s: Series, fz: dict):
    """Buys and sells over the last two settled weeks, overall and per sector."""
    v = s.valid
    lo = v - WINDOW + 1
    if lo < WINDOW:
        return None

    def both(buy, sell):
        return {"sold": stretch(sell, lo, v), "bought": stretch(buy, lo, v)}

    return {
        "from": s.days[lo],
        "to": s.days[v],
        "all": both(s.BUY.sum(axis=0), s.SELL.sum(axis=0)),
        "sectors": {k: both(s.BUY[i], s.SELL[i]) for i, k in enumerate(fz["sectors"]["order"])},
    }


def standouts(m: dict, side: str, heavy: bool) -> list:
    rows = []
    for k, d in m["sectors"].items():
        x = d[side]
        if x["usual"] is None:
            continue
        if heavy and x["beats"] >= HEAVY and x["now"] > x["usual"]:
            rows.append((k, x))
        elif not heavy and x["beats"] <= LIGHT and x["now"] < x["usual"]:
            rows.append((k, x))
    rows.sort(key=lambda r: abs(r[1]["now"] - r[1]["usual"]), reverse=True)
    return rows[:4]


def best_method(root: Path, board: dict):
    """The method with the best record: the live scoreboard once it has eight weeks, else the look-back."""
    if board and board.get("weeks_scored", 0) >= 8 and board.get("board"):
        b = board["board"]
        name = min(b, key=lambda k: b[k]["mean_abs_error"])
        return name, b[name]["mean_abs_error"], f"{board['weeks_scored']} scored weeks"
    f = root / "ledger" / "historical" / "summary.json"
    if f.exists():
        h = json.loads(f.read_text())
        if h.get("board"):
            b = h["board"]
            name = min(b, key=lambda k: b[k]["mean_abs_error"])
            return name, b[name]["mean_abs_error"], f"{h['weeks']} past weeks"
    return "dbl_fade", None, None


def _forecasts(root: Path) -> list:
    return sorted(config.ledger_dir(root, "forecasts").glob("*.json"))


def outlook(root: Path, fz: dict, board: dict):
    """The newest written forecast, as dollars a day and as a multiple of the usual level."""
    files = _forecasts(root)
    if not files:
        return None
    fc = json.loads(files[-1].read_text())
    name, err, basis = best_method(root, board)
    if name not in fc["predictions"]:
        name, err, basis = "dbl_fade", None, None
    floor = float(fz["series"]["floor_dollars"])
    rows = [
        {"sector": k, "per_day": max(10**h - floor, 0.0), "times": round(10 ** (h - a), 2)}
        for k, h, a in zip(fc["sectors"], fc["predictions"][name], fc["predictions"]["baseline_A"])
    ]
    return {
        "week": date.fromisoformat(fc["target_week"]),
        "issued": date.fromisoformat(fc["issued_utc"][:10]),
        "method": name,
        "miss": 10**err if err else None,
        "basis": basis,
        "rows": rows,
    }


def settles_on(monday: date, fz: dict) -> date:
    last = trading_days(monday, monday + timedelta(days=4))[-1]
    return add_trading_days(last, fz["data"]["timely_trading_days"])


def last_result(root: Path, fz: dict, method: str):
    """The newest scored week: what was expected against what was filed."""
    files = sorted(config.ledger_dir(root, "scores").glob("*.json"))
    if not files:
        return None
    sc = json.loads(files[-1].read_text())
    ff = config.ledger_dir(root, "forecasts") / files[-1].name
    if not ff.exists():
        return None
    fc = json.loads(ff.read_text())
    if method not in fc["predictions"]:
        return None
    floor = float(fz["series"]["floor_dollars"])
    rows = [
        {
            "sector": k,
            "expected": max(10**p - floor, 0.0),
            "actual": max(10**a - floor, 0.0),
            "factor": 10 ** abs(p - a),
        }
        for k, p, a in zip(sc["sectors"], fc["predictions"][method], sc["actual"])
    ]
    return {"week": date.fromisoformat(sc["target_week"]), "rows": rows}


def waiting(root: Path, today: date) -> list:
    """Weeks that have begun and have a forecast but no score yet."""
    scored = {f.name for f in config.ledger_dir(root, "scores").glob("*.json")}
    out = []
    for f in _forecasts(root):
        monday = date.fromisoformat(f.stem)
        if f.name not in scored and monday <= today:
            out.append(monday)
    return out


def season(s: Series, fz: dict) -> dict:
    """Settled days so far inside the event-test window."""
    y = fz["event_test"]["years"][1]
    start, end = date(y, 11, 20), date(y, 12, 31)
    idx = [i for i, d in enumerate(s.days) if start <= d <= end and i <= s.valid]
    out = {"claim": fz["event_test"]["claim"], "start": start, "end": end, "days": len(idx)}
    if idx:
        lo, hi = idx[0], idx[-1]
        out["through"] = s.days[hi]
        out["bought"] = stretch(s.BUY.sum(axis=0), lo, hi)
        out["sold"] = stretch(s.SELL.sum(axis=0), lo, hi)
    return out


def event_reading(root: Path):
    d = config.ledger_dir(root, "event")
    final = d / "final.json"
    if final.exists():
        return json.loads(final.read_text())
    files = sorted(d.glob("interim-*.json"))
    return json.loads(files[-1].read_text()) if files else None


def companies(conn, since: date, through: date, top: int = 6) -> dict:
    """Largest sells and buys by company, as filed. Local use only."""
    q = (
        "SELECT COALESCE(i.name, 'CIK ' || t.issuer_cik), SUM(t.dollars), COUNT(*), MAX(t.max_row) "
        "FROM trades t LEFT JOIN issuers i ON i.cik = t.issuer_cik "
        "WHERE t.trans_date BETWEEN ? AND ? AND t.code = ? "
        "GROUP BY t.issuer_cik ORDER BY 2 DESC LIMIT ?"
    )
    out = {}
    for code, side in (("S", "sold"), ("P", "bought")):
        out[side] = [
            {"name": company(n), "dollars": d, "entries": k, "largest": mx}
            for n, d, k, mx in conn.execute(q, (since.isoformat(), through.isoformat(), code, top))
        ]
    return out


def short_version(s: Series, fz: dict, root: Path, board: dict, today: date, ev: dict | None) -> list:
    L = []
    m = moves(s, fz)
    if m is None:
        L += ["Not enough settled history yet for a reading.", ""]
    else:
        a = m["all"]
        L += [f"**Last two weeks ({day(m['from'])} to {day(m['to'])})**", ""]
        L.append(
            f"- Overall: insiders sold {usd(a['sold']['now'])} and bought {usd(a['bought']['now'])}. "
            f"A usual two weeks is {usd(a['sold']['usual'])} sold and {usd(a['bought']['usual'])} bought."
        )
        found = False
        for side, word in (("sold", "selling"), ("bought", "buying")):
            for heavy, label in ((True, "Heavier"), (False, "Lighter")):
                rows = standouts(m, side, heavy)
                if rows:
                    found = True
                    items = "; ".join(
                        f"{SECTOR.get(k, k)} {usd(x['now'])} (usual {usd(x['usual'])})" for k, x in rows
                    )
                    L.append(f"- {label} {word} than usual: {items}.")
        if not found:
            L.append("- No sector stands out from its usual range.")
        L.append("")

    o = outlook(root, fz, board)
    if o:
        L += [f"**Expected for the week of {day(o['week'])}** (written {day(o['issued'])}, before the week began)", ""]

        def gap(r):  # dollars a day above or below the usual level
            return abs(r["per_day"] - r["per_day"] / r["times"]) if r["times"] > 0 else 0.0

        # The largest dollar moves first: a small sector at three times usual is still a small move.
        more = sorted((r for r in o["rows"] if r["times"] >= MORE), key=gap, reverse=True)[:4]
        less = sorted((r for r in o["rows"] if r["times"] <= LESS), key=gap, reverse=True)[:4]

        def items(rows):
            return "; ".join(
                f"{SECTOR.get(r['sector'], r['sector'])} ({times(r['times'])} times its usual level, "
                f"about {usd(r['per_day'])} a day)"
                for r in rows
            )

        if more:
            L.append(f"- More insider trading than usual, buys plus sells: {items(more)}.")
        if less:
            L.append(f"- Less than usual: {items(less)}.")
        rest = len(o["rows"]) - len(more) - len(less)
        if not more and not less:
            L.append("- Every sector is expected near its usual level.")
        elif rest:
            L.append(f"- The other {rest} sector{'s' if rest != 1 else ''}: near usual, or smaller moves.")
        if o["miss"]:
            L.append(
                f"- How rough this is: over {o['basis']}, this method's typical miss was a factor of about {o['miss']:.1f}."
            )
        L.append("")

    L += ["**How the last prediction did**", ""]
    res = last_result(root, fz, o["method"]) if o else None
    if res:
        within = sum(r["factor"] <= 2 for r in res["rows"])
        worst = max(res["rows"], key=lambda r: r["factor"])
        L.append(
            f"- Week of {day(res['week'])}: the prediction landed within a factor of two in "
            f"{within} of {len(res['rows'])} sectors."
        )
        L.append(
            f"- Biggest miss: {SECTOR.get(worst['sector'], worst['sector'])}, expected about "
            f"{usd(worst['expected'])} a day, came in at {usd(worst['actual'])}."
        )
    pending = waiting(root, today)
    for monday in pending[:2]:
        L.append(
            f"- The week of {day(monday)} cannot be checked until filings through "
            f"{day(settles_on(monday, fz))} are in."
        )
    if not res and not pending:
        L.append("- No predicted week has begun yet.")
    L.append("")

    sea = season(s, fz)
    k = fz["data"]["timely_trading_days"]
    claim = sea["claim"][:1].upper() + sea["claim"][1:]
    L += [f"**\"{claim}\" ({day(sea['start'])} to {day(sea['end'])})**", ""]
    if sea["days"] == 0:
        L.append(
            f"- No day in this window is complete yet. Filings for a trade keep arriving for {k} trading days after it."
        )
    else:
        n, b, se = sea["days"], sea["bought"], sea["sold"]
        L.append(
            f"- {n} day{'s' if n != 1 else ''} counted so far (through {day(sea['through'])}): insiders bought "
            f"{usd(b['now'])} (usual for that long: {usd(b['usual'])}) and sold {usd(se['now'])} "
            f"(usual: {usd(se['usual'])})."
        )
    ev = ev or event_reading(root)
    if ev and ev.get("rank_of_this_year"):
        if ev["status"] == "final":
            L.append(
                f"- Final: this year's year-end surge ranked {ev['rank_of_this_year']} of {ev['years_compared']} years. "
                f"The claim {'passes' if ev['passes'] else 'does not pass'}."
            )
        else:
            L.append(
                f"- So far this year's year-end surge ranks {ev['rank_of_this_year']} of {ev['years_compared']} years. "
                "The claim passes only if it ranks first when the window closes."
            )
    L += [
        "",
        "**Limits**",
        "",
        "- This tracks how much insiders trade, not prices. Numbers are as filed with the SEC, and one wrong entry "
        "in one filing can move a sector. Company insiders only. It is not a call on what to buy or sell.",
        "",
    ]
    return L


def tables(s: Series, fz: dict, root: Path, board: dict) -> list:
    L = []
    m = moves(s, fz)
    if m:
        L += [
            f"## All sectors, {day(m['from'])} to {day(m['to'])}",
            "",
            "| Sector | Sold | Usual | Bought | Usual |",
            "| --- | --- | --- | --- | --- |",
        ]
        for k, d in m["sectors"].items():
            L.append(
                f"| {SECTOR.get(k, k)} | {usd(d['sold']['now'])} | {usd(d['sold']['usual'])} | "
                f"{usd(d['bought']['now'])} | {usd(d['bought']['usual'])} |"
            )
        L.append("")
    o = outlook(root, fz, board)
    if o:
        L += [
            f"## All sectors, expected for the week of {day(o['week'])}",
            "",
            "Buys plus sells on a typical day of that week.",
            "",
            "| Sector | Expected a day | Times its usual level |",
            "| --- | --- | --- |",
        ]
        for r in o["rows"]:
            L.append(f"| {SECTOR.get(r['sector'], r['sector'])} | {usd(r['per_day'])} | {times(r['times'])} |")
        L.append("")
    return L


def build(conn, root: Path, fz: dict, s: Series, board: dict, today: date, filed_through: date, ev=None):
    """Return (public report text, weekly message text)."""
    k = fz["data"]["timely_trading_days"]
    short = short_version(s, fz, root, board, today, ev)
    complete = s.days[s.valid].isoformat() if s.valid >= 0 else "none yet"
    pub = [
        f"# Insider money moves: report of {today.isoformat()}",
        "",
        f"Built from SEC Form 4 filings through {filed_through.isoformat()}. \"Insiders\" are executives, directors "
        "and large shareholders trading their own company's stock. A day counts once its filings have had "
        f"{k} trading days to arrive, so the latest complete day is {complete}. \"Usual\" is the middle value of "
        "all stretches of the same length in the two years before.",
        "",
        "## The short version",
        "",
    ]
    pub += short + tables(s, fz, root, board)

    msg = [
        f"# Insider money moves, {today.isoformat()}",
        "",
        f"From SEC filings through {filed_through.isoformat()}. \"Insiders\" are executives, directors and large "
        "shareholders trading their own company's stock. This copy names companies and stays on this computer; "
        "the public report has sector totals only.",
        "",
    ]
    msg += short
    m = moves(s, fz)
    if m:
        c = companies(conn, m["from"], filed_through)

        def items(rows):
            out = []
            for r in rows:
                flag = (
                    " (one entry is most of this; worth opening the filing)"
                    if r["dollars"] >= 25e6 and r["largest"] >= 0.8 * r["dollars"]
                    else ""
                )
                out.append(f"{r['name']} {usd(r['dollars'])}{flag}")
            return "; ".join(out) if out else "none"

        msg += [
            f"**Biggest by company (trades dated {day(m['from'])} to {day(filed_through)}, as filed; "
            "the last few days are still filling in)**",
            "",
            f"- Sold: {items(c['sold'])}.",
            f"- Bought: {items(c['bought'])}.",
            "",
        ]
    return "\n".join(pub), "\n".join(msg)
