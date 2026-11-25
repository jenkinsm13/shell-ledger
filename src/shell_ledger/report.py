"""The weekly report: the money-moves readout first, then the record of which method is ahead."""
from __future__ import annotations

import json
from pathlib import Path

from .readout import event_reading

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


def _board(board: dict) -> list:
    out = ["| Predictor | Weeks | Average error |", "| --- | --- | --- |"]
    for name, v in board.items():
        out.append(f"| {LABEL.get(name, name)} | {v['weeks']} | {v['mean_abs_error']:.4f} |")
    return out


def record(root: Path, fz: dict, board: dict) -> list:
    """Every method's score, winners and losers alike, and the formal event-test reading."""
    ev = event_reading(root)
    hist_file = root / "ledger" / "historical" / "summary.json"
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else None
    L = [
        "## For the record: which prediction method is ahead",
        "",
        "Nine methods each write a prediction every week, under rules frozen in advance "
        "(see PREREGISTRATION.md). All of them are listed, the ones behind as well as the ones ahead. "
        "Errors are in log10 units of a sector's typical daily insider dollars: "
        "0.30 is a miss by a factor of two, 0.04 a miss of about 10%.",
        "",
    ]

    L += ["### Scored weeks so far", ""]
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
            "### The same rules run over past weeks",
            "",
            f"The same frozen rules run over {hist['weeks']} past weeks, {hist['first_week']} to {hist['last_week']}"
            + ("" if hist["complete"] else " (incomplete: recent filings still loading)")
            + f". Shell model against the primary control: difference {hc['mean_difference']:+.5f}, t = {t}. "
            f"Reading: {hist['reading']}. This is retrospective and is not the verdict.",
            "",
        ] + _board(hist["board"]) + [""]

    L += ["### The event test, formal reading", ""]
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
    return L


def build(root: Path, fz: dict, board: dict, readout_text: str) -> str:
    return readout_text.rstrip("\n") + "\n\n" + "\n".join(record(root, fz, board))
