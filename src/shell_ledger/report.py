"""A plain-language weekly report, built only from files already in the ledger."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from . import config

LABEL = {
    "phi_ck": "Shell model (golden-ratio windows, curve weights)",
    "phi_fade": "Golden-ratio windows, plain fade",
    "phi_equal": "Golden-ratio windows, equal weights",
    "dbl_ck": "Doubling windows, curve weights",
    "dbl_fade": "Primary control (doubling windows, plain fade)",
    "dbl_equal": "Doubling windows, equal weights",
    "baseline_A": "Baseline A (two-year average)",
    "baseline_B": "Baseline B (last five days)",
    "har": "HAR (fitted day, week, month)",
}


def money(log10_value: float) -> str:
    v = max(10 ** log10_value - 10000, 0)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= size:
            return f"${v / size:,.1f}{unit}"
    return f"${v:,.0f}"


def _latest(d: Path):
    files = sorted(d.glob("*.json"))
    return json.loads(files[-1].read_text()) if files else None


def _board(board: dict) -> list:
    out = ["| Predictor | Weeks | Average error |", "| --- | --- | --- |"]
    for name, v in board.items():
        out.append(f"| {LABEL.get(name, name)} | {v['weeks']} | {v['mean_abs_error']:.4f} |")
    return out


def build(root: Path, fz: dict, board: dict, today: date) -> str:
    fc = _latest(config.ledger_dir(root, "forecasts"))
    sc = _latest(config.ledger_dir(root, "scores"))
    ev = _latest(config.ledger_dir(root, "event"))
    hist_file = root / "ledger" / "historical" / "summary.json"
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else None
    L = [f"# shell-ledger report, {today.isoformat()}", ""]
    L += [
        "Errors are in log10 units of a sector's typical daily insider dollars. "
        "An error of 0.30 is a miss by a factor of two; 0.04 is a miss of about 10%.",
        "",
    ]

    L += ["## What was predicted", ""]
    if fc:
        L += [
            f"For the week of {fc['target_week']}, written {fc['issued_utc']} from data through {fc['cutoff_day']}. "
            "The shell model's forecast of typical daily dollars traded by insiders, buys plus sells:",
            "",
            "| Sector | Shell model | Primary control |",
            "| --- | --- | --- |",
        ]
        for s, a, b in zip(fc["sectors"], fc["predictions"]["phi_ck"], fc["predictions"]["dbl_fade"]):
            L.append(f"| {s} | {money(a)} | {money(b)} |")
    else:
        L.append("No forecast has been written yet.")
    L.append("")

    L += ["## What happened", ""]
    if sc:
        best = min(sc["mean_abs_error"], key=sc["mean_abs_error"].get)
        L += [
            f"The week of {sc['target_week']} is settled. Closest that week: {LABEL.get(best, best)}, "
            f"average error {sc['mean_abs_error'][best]:.4f}. The shell model's was {sc['mean_abs_error']['phi_ck']:.4f}.",
            "",
            "| Sector | Actual | Shell forecast error | Net buys minus sells (logged, not scored) |",
            "| --- | --- | --- | --- |",
        ]
        net = sc["logged_not_scored"]["net_buy_minus_sell_dollars"]
        for i, s in enumerate(sc["sectors"]):
            L.append(f"| {s} | {money(sc['actual'][i])} | {sc['abs_error']['phi_ck'][i]:.3f} | {net[i]:+,.0f} |")
    else:
        L.append("No week has been scored yet. A week settles three trading days after its last day.")
    L.append("")

    L += ["## Who is ahead", ""]
    c = board["primary_contrast"]
    if board["board"]:
        L += _board(board["board"]) + [""]
        L.append(
            f"The verdict rests on one comparison, the shell model against the primary control. "
            f"After {c['weeks']} of {board['weeks_needed']} weeks the average weekly difference is {c['mean_difference']:+.5f} "
            f"(positive favors the shell model). Verdict: {board['verdict']}."
        )
    else:
        L.append(f"Nothing scored yet. The verdict is read once, after {board['weeks_needed']} weeks.")
    L.append("")

    if hist:
        hc = hist["primary_contrast"]
        t = f"{hc['t']:.2f}" if hc["t"] == hc["t"] else "n/a"
        L += [
            "## Historical reading",
            "",
            f"The same frozen rules run over {hist['weeks']} past weeks, {hist['first_week']} to {hist['last_week']}"
            + ("" if hist["complete"] else " (incomplete: recent filings still loading)")
            + f". Shell model against the primary control: difference {hc['mean_difference']:+.5f}, t = {t}. "
            f"Reading: {hist['reading']}. This is retrospective and is not the verdict.",
            "",
        ] + _board(hist["board"]) + [""]

    L += ["## The event test", ""]
    mine = ev["uplift_by_year"].get(str(fz["event_test"]["years"][1])) if ev else None
    if ev and not mine:
        L.append(
            f"Claim: \"{ev['claim']}\". No day in this year's window has settled yet "
            f"(settled through {ev['settled_through']}). The first reading comes next week."
        )
    elif ev:
        L.append(
            f"Claim: \"{ev['claim']}\". Status: {ev['status']}, settled through {ev['settled_through']}. "
            f"This year's seasonal uplift is {mine['uplift']:+.3f} over {mine['window_days']} days, "
            f"ranked {ev['rank_of_this_year']} of {ev['years_compared']} years. "
            + ("It passes only if it ranks first when final." if ev["status"] != "final" else f"Passes: {ev['passes']}.")
        )
        sf = ev["same_filers"]
        if sf["rank_of_this_year"]:
            L.append(
                f"Same-filer participation ranks {sf['rank_of_this_year']} of {sf['years_compared']} years (reported only)."
            )
    else:
        L.append("No reading yet.")
    L.append("")
    return "\n".join(L)
