"""The public ledger: forecasts written before the week, scores written after. Write-once."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from . import config
from .model import NAMES, Model, dm_stat, verdict
from .tradingdays import add_trading_days, monday_of

NY = ZoneInfo("America/New_York")
UNITS = "mean over the week's trading days of log10(sector dollars + 10000)"


class LedgerError(RuntimeError):
    pass


def _r(a):
    return [round(float(x), 6) for x in a]


def _write_once(path: Path, obj: dict) -> None:
    if path.exists():
        raise LedgerError(f"{path.name} already exists. Ledger files are never rewritten.")
    path.write_text(json.dumps(obj, indent=1) + "\n")


def next_target_monday(now: datetime) -> date:
    local = now.astimezone(NY)
    return monday_of(local.date()) + timedelta(days=7)


def issue(root: Path, fz: dict, model: Model, now: datetime | None = None) -> Path:
    """Write the forecast for next week. Refuses if late or if the data is not in."""
    now = now or datetime.now(timezone.utc)
    monday = next_target_monday(now)
    if monday < date.fromisoformat(fz["prospective"]["earliest_target_week"]):
        raise LedgerError("Before the first prospective week.")
    deadline = datetime(monday.year, monday.month, monday.day, tzinfo=NY)
    if now >= deadline:
        raise LedgerError("Past the deadline for that week. It is skipped, not backfilled.")
    path = config.ledger_dir(root, "forecasts") / f"{monday.isoformat()}.json"
    if path.exists():
        raise LedgerError(f"The forecast for the week of {monday} is already written.")
    e = model.cutoff(monday)
    if e is None or model.s.valid < e:
        last = model.s.days[model.s.valid] if model.s.valid >= 0 else "nothing"
        need = model.s.days[e] if e is not None else "?"
        raise LedgerError(f"Data is settled through {last}; this forecast needs {need}. Run the ingest first.")
    preds = model.forecast(monday)
    if preds is None:
        raise LedgerError("Not enough history for the longest window.")
    window = model.s.D[:, e + 1 - model.longest : e + 1]
    obj = {
        "kind": "forecast",
        "target_week": monday.isoformat(),
        "issued_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cutoff_day": model.s.days[e].isoformat(),
        "frozen_sha256": fz["_sha256"],
        "inputs_sha256": hashlib.sha256(np.round(window, 2).tobytes()).hexdigest(),
        "units": UNITS,
        "sectors": fz["sectors"]["order"],
        "predictions": {k: _r(v) for k, v in preds.items()},
    }
    _write_once(path, obj)
    return path


def score_one(fz: dict, model: Model, monday: date, preds: dict) -> dict:
    idx = model.week_idx(monday)
    actual = model.target(monday)
    err = {k: np.abs(np.asarray(v) - actual) for k, v in preds.items()}
    s = model.s
    return {
        "target_week": monday.isoformat(),
        "trading_days": len(idx),
        "actual": _r(actual),
        "abs_error": {k: _r(v) for k, v in err.items()},
        "mean_abs_error": {k: round(float(v.mean()), 6) for k, v in err.items()},
        "logged_not_scored": {
            "total_dollars": [round(float(x)) for x in s.D[:, idx].sum(axis=1)],
            "net_buy_minus_sell_dollars": [round(float(x)) for x in (s.BUY[:, idx] - s.SELL[:, idx]).sum(axis=1)],
        },
    }


def score_pending(root: Path, fz: dict, model: Model, now: datetime | None = None) -> list:
    """Score every forecast whose week has settled and has no score yet."""
    now = now or datetime.now(timezone.utc)
    done = []
    fdir, sdir = config.ledger_dir(root, "forecasts"), config.ledger_dir(root, "scores")
    for f in sorted(fdir.glob("*.json")):
        out = sdir / f.name
        if out.exists():
            continue
        fc = json.loads(f.read_text())
        monday = date.fromisoformat(fc["target_week"])
        if not model.settled(monday):
            continue
        obj = {"kind": "score", "scored_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "frozen_sha256": fz["_sha256"]}
        obj.update(score_one(fz, model, monday, fc["predictions"]))
        obj["sectors"] = fc["sectors"]
        obj["units"] = UNITS
        _write_once(out, obj)
        done.append(out)
    return done


def contrast(scores: list, fz: dict) -> dict:
    """The primary contrast over a list of score dicts."""
    pc = fz["primary_contrast"]
    d = [
        float(np.mean(np.array(s["abs_error"][pc["control"]]) - np.array(s["abs_error"][pc["model"]])))
        for s in scores
        if pc["model"] in s["abs_error"] and pc["control"] in s["abs_error"]
    ]
    mean, t, n = dm_stat(d, pc["hac_lags"])
    return {"weeks": n, "mean_difference": mean, "t": t}


def scoreboard(root: Path, fz: dict) -> dict:
    scores = [json.loads(f.read_text()) for f in sorted(config.ledger_dir(root, "scores").glob("*.json"))]
    need = fz["prospective"]["weeks"]
    counted = scores[:need]
    board = {}
    for name in NAMES:
        v = [s["mean_abs_error"][name] for s in counted if name in s["mean_abs_error"]]
        if v:
            board[name] = {"weeks": len(v), "mean_abs_error": round(float(np.mean(v)), 6)}
    c = contrast(counted, fz)
    final = len(counted) >= need
    return {
        "weeks_scored": len(counted),
        "weeks_needed": need,
        "board": dict(sorted(board.items(), key=lambda kv: kv[1]["mean_abs_error"])),
        "primary_contrast": c,
        "status": "final" if final else "interim",
        "verdict": verdict(c["t"], fz) if final else f"none yet: {len(counted)} of {need} weeks scored",
    }


def historical(root: Path, fz: dict, model: Model) -> dict:
    """Apply the same frozen predictors to every settled week in the historical span."""
    h = fz["historical"]
    monday = date.fromisoformat(h["first_target_week"])
    last = date.fromisoformat(h["last_target_week"])
    rows, scores = [], []
    while monday <= last:
        if model.settled(monday):
            preds = model.forecast(monday)
            if preds is not None:
                s = score_one(fz, model, monday, preds)
                scores.append(s)
                rows.append([monday.isoformat()] + [s["mean_abs_error"].get(n, "") for n in NAMES])
        monday += timedelta(days=7)
    out = config.ledger_dir(root, "historical")
    with open(out / "weeks.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["target_week"] + NAMES)
        w.writerows(rows)
    board = {}
    for n in NAMES:
        v = [s["mean_abs_error"][n] for s in scores if n in s["mean_abs_error"]]
        if v:
            board[n] = {"weeks": len(v), "mean_abs_error": round(float(np.mean(v)), 6)}
    pc = fz["primary_contrast"]
    c = contrast(scores, fz)
    versus = {}
    for n in NAMES:
        if n == pc["model"]:
            continue
        d = [
            float(np.mean(np.array(s["abs_error"][n]) - np.array(s["abs_error"][pc["model"]])))
            for s in scores
            if n in s["abs_error"]
        ]
        mean, t, k = dm_stat(d, pc["hac_lags"])
        versus[n] = {"weeks": k, "mean_difference": round(mean, 6), "t": round(t, 3) if t == t else None}
    complete = bool(scores) and scores[-1]["target_week"] == h["last_target_week"]
    summary = {
        "kind": "historical",
        "role": h["role"],
        "frozen_sha256": fz["_sha256"],
        "first_week": scores[0]["target_week"] if scores else None,
        "last_week": scores[-1]["target_week"] if scores else None,
        "complete": complete,
        "weeks": len(scores),
        "units": UNITS,
        "board": dict(sorted(board.items(), key=lambda kv: kv[1]["mean_abs_error"])),
        "primary_contrast": c,
        "reading": verdict(c["t"], fz),
        "shell_model_versus_each": versus,
        "note": "Positive differences mean the shell model had the smaller error.",
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    return summary


def event_final_day(fz: dict) -> date:
    return add_trading_days(date(2026, 12, 31), fz["data"]["timely_trading_days"])
