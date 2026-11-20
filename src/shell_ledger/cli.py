"""Command line. `shell-ledger weekly` is the whole loop; the rest are its steps."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone

from . import config, db, event, ingest, ledger, report, secweb
from .model import Model
from .series import build


def _open():
    root = config.home()
    config.load_env(root)
    fz = config.load_frozen(root)
    conn = db.connect(config.data_dir(root) / "shell_ledger.sqlite")
    return root, fz, conn


def _model(conn, fz):
    through = ingest.filed_through(conn)
    if through is None:
        raise ledger.LedgerError("No data yet. Run: shell-ledger ingest")
    series = build(conn, fz, through)
    return Model(series, fz)


def _cap(fz):
    return float(fz["data"]["drop_row_if_dollars_above"])


def cmd_check(a):
    root, fz, conn = _open()
    print(f"frozen.json matches FROZEN.sha256: {fz['_sha256']}")


def cmd_ingest(a):
    root, fz, conn = _open()
    done = ingest.ingest_bulk(conn, _cap(fz), a.max_seconds)
    if done:
        done = ingest.ingest_live(conn, _cap(fz), a.max_seconds)
    if done:
        done = ingest.ingest_sic(conn, a.max_seconds)
    print("INGEST: caught up" if done else "INGEST: more to do, run again")
    return 0 if done else 3


def cmd_status(a):
    root, fz, conn = _open()
    n = conn.execute("SELECT COUNT(*), MIN(trans_date), MAX(filed_date) FROM trades").fetchone()
    iss = conn.execute("SELECT COUNT(*), SUM(sic IS NOT NULL) FROM issuers").fetchone()
    need = conn.execute(
        "SELECT COUNT(DISTINCT issuer_cik) FROM trades WHERE issuer_cik NOT IN (SELECT cik FROM issuers)"
    ).fetchone()[0]
    out = {
        "trade_rows": n[0],
        "first_transaction": n[1],
        "last_filing": n[2],
        "bulk_through": str(ingest.bulk_through(conn)),
        "filed_through": str(ingest.filed_through(conn)),
        "issuers_looked_up": iss[0],
        "issuers_with_sic": iss[1],
        "issuers_waiting": need,
        "forecasts": len(list(config.ledger_dir(root, "forecasts").glob("*.json"))),
        "scores": len(list(config.ledger_dir(root, "scores").glob("*.json"))),
        "next_target_week": str(ledger.next_target_monday(datetime.now(timezone.utc))),
    }
    print(json.dumps(out, indent=1))


def cmd_issue(a):
    root, fz, conn = _open()
    print("wrote", ledger.issue(root, fz, _model(conn, fz)))


def cmd_score(a):
    root, fz, conn = _open()
    for p in ledger.score_pending(root, fz, _model(conn, fz)):
        print("wrote", p)
    print(json.dumps(ledger.scoreboard(root, fz), indent=1))


def cmd_historical(a):
    root, fz, conn = _open()
    print(json.dumps(ledger.historical(root, fz, _model(conn, fz)), indent=1))


def _event(root, fz, conn, model):
    ev = event.run(conn, model.s, fz)
    name = "final.json" if ev["status"] == "final" else f"interim-{ev['settled_through']}.json"
    path = config.ledger_dir(root, "event") / name
    if not path.exists():
        path.write_text(json.dumps(ev, indent=1) + "\n")
    return ev


def cmd_event(a):
    root, fz, conn = _open()
    ev = _event(root, fz, conn, _model(conn, fz))
    print(json.dumps({k: ev[k] for k in ("status", "settled_through", "rank_of_this_year", "years_compared", "passes")}))


def cmd_report(a):
    root, fz, conn = _open()
    text = report.build(root, fz, ledger.scoreboard(root, fz), date.today())
    path = config.ledger_dir(root, "reports") / f"{date.today().isoformat()}.md"
    path.write_text(text)
    print(text)


def cmd_weekly(a):
    """Ingest, score what has settled, write next week's forecast, event test, report."""
    root, fz, conn = _open()
    t0 = time.monotonic()
    code = cmd_ingest(a)
    if not code and a.max_seconds < 1000 and time.monotonic() - t0 > 30:
        # Time-limited runners: do the scoring and forecasting in a fresh call.
        print("WEEKLY: ingest caught up; run again to score and forecast.")
        return 3
    waiting = conn.execute(
        "SELECT COUNT(DISTINCT issuer_cik) FROM trades WHERE issuer_cik NOT IN (SELECT cik FROM issuers)"
    ).fetchone()[0]
    if code or waiting:
        print("WEEKLY: ingest not finished; nothing written. Run again.")
        return 3
    model = _model(conn, fz)
    for p in ledger.score_pending(root, fz, model):
        print("scored", p.name)
    try:
        print("forecast", ledger.issue(root, fz, model).name)
    except ledger.LedgerError as e:
        print("no forecast:", e)
    if not (root / "ledger" / "historical" / "summary.json").exists() or a.historical:
        h = ledger.historical(root, fz, model)
        print("historical:", h["weeks"], "weeks,", h["reading"])
    ev = _event(root, fz, conn, model)
    print("event:", ev["status"], "rank", ev["rank_of_this_year"], "of", ev["years_compared"])
    text = report.build(root, fz, ledger.scoreboard(root, fz), date.today())
    (config.ledger_dir(root, "reports") / f"{date.today().isoformat()}.md").write_text(text)
    print("WEEKLY: done")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="shell-ledger", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in [
        ("check", cmd_check),
        ("ingest", cmd_ingest),
        ("status", cmd_status),
        ("issue", cmd_issue),
        ("score", cmd_score),
        ("historical", cmd_historical),
        ("event", cmd_event),
        ("report", cmd_report),
        ("weekly", cmd_weekly),
    ]:
        sp = sub.add_parser(name, help=(fn.__doc__ or "").strip() or None)
        sp.set_defaults(fn=fn)
        if name in ("ingest", "weekly"):
            sp.add_argument("--max-seconds", type=float, default=36000, help="time budget per ingest step")
        if name == "weekly":
            sp.add_argument("--historical", action="store_true", help="rerun the historical reading")
    a = p.parse_args(argv)
    try:
        return a.fn(a) or 0
    except (secweb.SecError, ledger.LedgerError, config.FrozenError) as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
