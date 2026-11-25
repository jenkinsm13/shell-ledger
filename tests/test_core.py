import json
import shutil
import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from shell_ledger import config, db, event, ingest, ledger, model, readout, report, series, tradingdays as td

ROOT = Path(__file__).resolve().parents[1]
FZ = json.loads((ROOT / "frozen.json").read_text())
FZ["_sha256"] = "test"


def make_series(seed=0, end=date(2026, 12, 15), constant=None):
    days = td.trading_days(date(2006, 1, 3), end)
    rng = np.random.default_rng(seed)
    n = len(FZ["sectors"]["order"])
    if constant is None:
        D = 10 ** rng.normal(7.0, 0.6, size=(n, len(days)))
    else:
        D = np.full((n, len(days)), constant)
    return series.Series(days, D, D * 0.25, D * 0.75, len(days) - 1, 10000.0)


def test_calendar():
    assert not td.is_trading_day(date(2026, 11, 26))  # Thanksgiving
    assert td.is_trading_day(date(2026, 11, 27))
    assert not td.is_trading_day(date(2026, 7, 3))  # July 4 on a Saturday
    assert not td.is_trading_day(date(2022, 6, 20))  # Juneteenth observed
    assert td.is_trading_day(date(2021, 6, 18))  # before Juneteenth was a market holiday
    assert td.is_trading_day(date(2021, 12, 31))  # New Year's on a Saturday is not observed
    assert td.easter(2026) == date(2026, 4, 5)
    assert not td.is_trading_day(date(2026, 4, 3))  # Good Friday
    assert not td.is_trading_day(date(2012, 10, 29))  # Sandy
    assert td.add_trading_days(date(2026, 12, 31), 3) == date(2027, 1, 6)
    assert len(td.trading_days(date(2025, 1, 1), date(2025, 12, 31))) == 250


def test_frozen_windows_and_weights():
    assert FZ["grids"]["phi"]["windows"] == [1, 2, 3, 4, 7, 11, 18, 29, 47, 76, 123, 199, 322, 521]
    assert FZ["grids"]["doubling"]["windows"] == [2 ** k for k in range(10)]
    assert [round(model.w_ck(m), 3) for m in range(5)] == [0.5, 0.406, 0.259, 0.081, 0.054]
    assert round(model.w_ck(2) / model.w_ck(3), 1) == 3.2
    assert round(model.w_ck(3) / model.w_ck(4), 1) == 1.5
    assert model.w_fade(0) == 1.0


def test_sectors():
    order, sector_of = series.sector_table(FZ)
    name = lambda sic: order[sector_of(sic)]
    assert name(7372) == "BusEq" and name(6021) == "Money" and name(2834) == "Hlth"
    assert name(1311) == "Enrgy" and name(4911) == "Utils" and name(5411) == "Shops"
    assert name(None) == "Other" and name(9999) == "Other"
    assert len(order) == 12


def test_cutoff_and_no_lookahead():
    s = make_series()
    m = model.Model(s, FZ)
    monday = date(2026, 11, 23)
    e = m.cutoff(monday)
    assert s.days[e] == date(2026, 11, 17)  # three trading days before Friday the 20th
    before = m.forecast(monday)
    s2 = make_series()
    s2.D[:, e + 1 :] *= 1000.0  # change everything after the cutoff
    after = model.Model(s2, FZ).forecast(monday)
    for k in before:
        assert np.allclose(before[k], after[k]), k
    assert set(before) == set(model.NAMES)


def test_constant_series_gives_constant_forecasts():
    s = make_series(constant=1e7)
    m = model.Model(s, FZ)
    f = m.forecast(date(2026, 11, 23))
    want = np.log10(1e7 + 10000)
    for k, v in f.items():
        assert np.allclose(v, want, atol=1e-6), k


def test_blend_is_the_frozen_formula():
    s = make_series(3)
    m = model.Model(s, FZ)
    e = m.cutoff(date(2026, 11, 23))
    windows, j = FZ["grids"]["phi"]["windows"], 3
    w = np.array([model.w_ck(abs(j - k)) for k in range(len(windows))])
    x = np.stack([s.L[:, e + 1 - n : e + 1].mean(axis=1) for n in windows])
    assert np.allclose(m.blend(e, "phi", "ck"), (w[:, None] * x).sum(0) / w.sum())


def test_dm_stat():
    rng = np.random.default_rng(1)
    mean, t, n = model.dm_stat(0.05 + rng.normal(0, 0.1, 400))
    assert n == 400 and mean > 0 and t > 1.645
    mean, t, n = model.dm_stat(rng.normal(0, 0.1, 400))
    assert abs(t) < 3
    assert model.verdict(2.0, FZ).endswith("wins") and model.verdict(-2.0, FZ).endswith("loses")
    assert model.verdict(0.3, FZ).startswith("tie")


def _tsv(header, rows):
    return "\n".join("\t".join(map(str, r)) for r in [header] + rows) + "\n"


def make_zip(path):
    sub = _tsv(
        ["ACCESSION_NUMBER", "FILING_DATE", "PERIOD_OF_REPORT", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERNAME"],
        [
            ["A-1", "08-JAN-2024", "04-JAN-2024", "4", "320193", "APPLE"],
            ["A-2", "08-JAN-2024", "04-JAN-2024", "4/A", "320193", "APPLE"],
            ["A-3", "22-JAN-2024", "04-JAN-2024", "4", "19617", "JPM"],
            ["A-4", "05-JAN-2024", "04-JAN-2024", "5", "19617", "JPM"],
        ],
    )
    own = _tsv(
        ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME"],
        [["A-1", "900", "X"], ["A-1", "800", "Y"], ["A-3", "700", "Z"]],
    )
    tr = _tsv(
        ["ACCESSION_NUMBER", "NONDERIV_TRANS_SK", "SECURITY_TITLE", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE"],
        [
            ["A-1", 1, "Common", "04-JAN-2024", "S", "1000", "180.5"],
            ["A-1", 2, "Common", "04-JAN-2024", "S", "500", "181"],
            ["A-1", 3, "Common", "04-JAN-2024", "A", "999", "0"],
            ["A-1", 4, "Common", "04-JAN-2024", "P", "10", ""],
            ["A-2", 5, "Common", "04-JAN-2024", "S", "1000", "180.5"],
            ["A-3", 6, "Common", "04-JAN-2024", "P", "100", "170"],
            ["A-4", 7, "Common", "04-JAN-2024", "P", "100", "170"],
            ["A-1", 8, "Common", "04-JAN-2024", "S", "1", "99999999999999"],
        ],
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("SUBMISSION.tsv", sub)
        z.writestr("REPORTINGOWNER.tsv", own)
        z.writestr("NONDERIV_TRANS.tsv", tr)
        z.writestr("FORM345_readme.htm", "x")


def test_bulk_zip_and_timely_rule(tmp_path):
    z = tmp_path / "q.zip"
    make_zip(z)
    conn = db.connect(tmp_path / "t.sqlite")
    stats = ingest.load_bulk_zip(conn, z, "bulk:test", 5e10)
    conn.commit()
    rows = conn.execute("SELECT accession, code, issuer_cik, owner_cik, filed_date, dollars, lots FROM trades ORDER BY accession").fetchall()
    assert stats == {"form4_filings": 2, "trade_rows": 2}
    assert rows[0] == ("A-1", "S", 320193, 800, "2024-01-08", 1000 * 180.5 + 500 * 181, 2)
    assert rows[1][:2] == ("A-3", "P")
    conn.execute("INSERT INTO issuers VALUES (320193, 3571, 'APPLE'), (19617, 6021, 'JPM')")
    days = td.trading_days(date(2024, 1, 2), date(2024, 3, 1))
    q = list(series.qualifying(conn, FZ, days))
    # A-1 was filed two trading days after the trade and counts; A-3 was filed late and does not.
    assert len(q) == 1 and q[0][1] == "S" and q[0][4] == 3571


FORM4 = """<SEC-DOCUMENT>
<TYPE>4
<XML>
<?xml version="1.0"?>
<ownershipDocument>
 <documentType>4</documentType>
 <issuer><issuerCik>0000320193</issuerCik><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
 <reportingOwner><reportingOwnerId><rptOwnerCik>0001214156</rptOwnerCik></reportingOwnerId></reportingOwner>
 <reportingOwner><reportingOwnerId><rptOwnerCik>0000000042</rptOwnerCik></reportingOwnerId></reportingOwner>
 <nonDerivativeTable>
  <nonDerivativeTransaction>
   <transactionDate><value>2026-11-17</value></transactionDate>
   <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>S</transactionCode></transactionCoding>
   <transactionAmounts><transactionShares><value>2000</value></transactionShares>
   <transactionPricePerShare><value>250.25</value></transactionPricePerShare>
   <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
  </nonDerivativeTransaction>
  <nonDerivativeHolding><securityTitle><value>Common</value></securityTitle></nonDerivativeHolding>
 </nonDerivativeTable>
</ownershipDocument>
</XML>
</SEC-DOCUMENT>"""


def test_form4_xml_and_master_index():
    doc = ingest.parse_form4(FORM4)
    assert doc["issuer"] == 320193 and doc["owner"] == 42
    assert doc["rows"] == [("2026-11-17", "S", 2000.0, 250.25)]
    assert ingest.parse_form4(FORM4.replace("<documentType>4<", "<documentType>4/A<")) is None
    idx = "CIK|Company Name|Form Type|Date Filed|File Name\n----\n320193|APPLE INC|4|20261119|edgar/data/320193/0001-26-000001.txt\n1|X|10-K|20261119|edgar/data/1/0002-26-000002.txt\n"
    got = list(ingest.parse_master(idx))
    assert got[0] == ("4", "2026-11-19", "edgar/data/320193/0001-26-000001.txt") and len(got) == 2
    assert ingest.parse_date("31-MAR-2006") == "2006-03-31" and ingest.parse_date("") is None
    assert ingest.quarter_end(2026, 3) == date(2026, 9, 30) and ingest.quarter_end(2025, 4) == date(2025, 12, 31)
    assert list(ingest.quarters((2026, 1), date(2026, 11, 20))) == [(2026, 1), (2026, 2), (2026, 3)]


@pytest.fixture
def project(tmp_path):
    shutil.copy(ROOT / "frozen.json", tmp_path / "frozen.json")
    (tmp_path / "FROZEN.sha256").write_text(config.frozen_sha256(tmp_path) + "  frozen.json\n")
    return tmp_path


def test_frozen_file_is_guarded(project):
    fz = config.load_frozen(project)
    assert fz["_sha256"] == config.frozen_sha256(project)
    (project / "frozen.json").write_text((project / "frozen.json").read_text().replace("1.645", "1.0"))
    with pytest.raises(config.FrozenError):
        config.load_frozen(project)


def test_issue_score_and_board(project):
    fz = config.load_frozen(project)
    s = make_series(5)
    m = model.Model(s, fz)
    saturday = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)
    path = ledger.issue(project, fz, m, now=saturday)
    fc = json.loads(path.read_text())
    assert fc["target_week"] == "2026-11-23" and fc["cutoff_day"] == "2026-11-17"
    assert set(fc["predictions"]) == set(model.NAMES) and len(fc["predictions"]["phi_ck"]) == 12
    with pytest.raises(ledger.LedgerError):  # write-once
        ledger.issue(project, fz, m, now=saturday)
    # Once Monday 00:00 New York arrives (05:00 UTC) that week can no longer be written:
    # the only week the tool will write is the one after it.
    assert ledger.next_target_monday(datetime(2026, 11, 30, 4, 59, tzinfo=timezone.utc)) == date(2026, 11, 30)
    assert ledger.next_target_monday(datetime(2026, 11, 30, 5, 0, tzinfo=timezone.utc)) == date(2026, 12, 7)
    # data not settled far enough
    s_short = make_series(5)
    s_short.valid = m.cutoff(date(2026, 11, 30)) - 1
    with pytest.raises(ledger.LedgerError):
        ledger.issue(project, fz, model.Model(s_short, fz), now=datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc))
    done = ledger.score_pending(project, fz, m)
    assert [p.name for p in done] == ["2026-11-23.json"]
    sc = json.loads(done[0].read_text())
    assert sc["trading_days"] == 4 and len(sc["actual"]) == 12
    want = abs(np.array(fc["predictions"]["phi_ck"]) - np.array(sc["actual"]))
    assert np.allclose(sc["abs_error"]["phi_ck"], want, atol=2e-6)
    assert ledger.score_pending(project, fz, m) == []
    board = ledger.scoreboard(project, fz)
    assert board["weeks_scored"] == 1 and board["status"] == "interim" and "1 of 26" in board["verdict"]


def test_unsettled_week_is_not_scored(project):
    fz = config.load_frozen(project)
    s = make_series(5)
    m = model.Model(s, fz)
    ledger.issue(project, fz, m, now=datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc))
    s.valid = s.index_of(date(2026, 11, 25))  # week not finished settling
    assert ledger.score_pending(project, fz, m) == []


def test_historical_reading(project):
    fz = config.load_frozen(project)
    fz["historical"] = dict(fz["historical"], first_target_week="2025-01-06", last_target_week="2025-06-30")
    h = ledger.historical(project, fz, model.Model(make_series(7), fz))
    assert h["weeks"] == 26 and h["complete"] and set(h["board"]) == set(model.NAMES)
    assert (project / "ledger" / "historical" / "weeks.csv").exists()
    assert h["primary_contrast"]["weeks"] == 26


def test_event_uplift_detects_a_planted_surge():
    s = make_series(11, end=date(2026, 12, 31))
    for i, d in enumerate(s.days):
        if d.year == 2026 and (d.month, d.day) >= (11, 20):
            s.D[:, i] *= 30
    u = event.uplift(s, FZ)
    assert len(u) == 21 and max(u, key=lambda y: u[y]["uplift"]) == 2026
    flat = event.uplift(make_series(11, end=date(2026, 12, 31)), FZ)
    assert abs(flat[2026]["uplift"]) < 0.2


def test_event_same_filers(tmp_path):
    conn = db.connect(tmp_path / "e.sqlite")
    rows = []
    for y in range(2020, 2027):
        for owner in range(1, 41):
            r0 = td.add_trading_days(date(y, 3, 9), 1)
            w0 = td.add_trading_days(date(y, 11, 30), 1)
            rows.append((f"r{y}-{owner}", r0.isoformat(), "S", 1, owner, td.add_trading_days(r0, 1).isoformat(), 1e6, 1, 1e6, 1, "t"))
            if y == 2026 or owner <= 10:
                rows.append((f"w{y}-{owner}", w0.isoformat(), "S", 1, owner, td.add_trading_days(w0, 1).isoformat(), 1e6, 1, 1e6, 1, "t"))
    conn.executemany(db.INSERT_TRADE, rows)
    conn.commit()
    s = series.build(conn, FZ, filed_through=date(2026, 12, 31), today=date(2026, 12, 31))
    v = event.same_filers(conn, s, FZ)
    assert max(v, key=lambda y: v[y]["v"]) == 2026 and v[2026]["panel"] == 40
    out = event.run(conn, s, dict(FZ, _sha256="t"))
    assert out["same_filers"]["rank_of_this_year"] == 1 and out["status"] == "interim"


def test_bulk_links_are_read_from_the_page():
    html = (
        '<a href="/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip">2026 Q1</a>'
        '<a href="/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip">2026 Q2</a>'
        '<a href="/other/thing.zip">x</a>'
    )
    links = ingest.parse_bulk_links(html)
    assert set(links) == {(2026, 1), (2026, 2)}
    assert links[(2026, 2)] == "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip"


def test_rules_cannot_change_once_a_forecast_exists(project):
    fz = config.load_frozen(project)
    ledger.issue(project, fz, model.Model(make_series(5), fz), now=datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc))
    config.load_frozen(project)  # still fine
    text = (project / "frozen.json").read_text().replace("1.645", "1.0")
    (project / "frozen.json").write_text(text)
    (project / "FROZEN.sha256").write_text(config.frozen_sha256(project) + "  frozen.json\n")
    with pytest.raises(config.FrozenError):
        config.load_frozen(project)


def test_frozen_cap_is_the_amended_one():
    assert FZ["data"]["drop_row_if_dollars_above"] == 5e9
    assert FZ["amendments"][0]["was"] == 5e10 and len(FZ["amendments"]) == 1


def test_directory_listing_gives_posted_days():
    raw = json.dumps({"directory": {"item": [
        {"name": "master.20261009.idx"}, {"name": "master.20261013.idx"},
        {"name": "form.20261009.idx"}, {"name": "sitemap.20261009.xml"}]}}).encode()
    assert ingest.parse_listing(raw) == {date(2026, 10, 9), date(2026, 10, 13)}


def test_readout_words():
    assert readout.usd(9.38e9) == "$9.38 billion" and readout.usd(12.3e9) == "$12.3 billion"
    assert readout.usd(686e6) == "$686 million" and readout.usd(73_000) == "$73 thousand"
    assert readout.usd(999.7e6) == "$1.00 billion" and readout.usd(12) == "$12"
    assert readout.company("PRIMO BRANDS CORP") == "Primo Brands Corp" and readout.company("IBM LLC") == "IBM LLC"
    a = np.ones(600)
    a[590:] = 3
    x = readout.stretch(a, 590, 599)
    assert x["now"] == 30 and x["usual"] == 10 and x["beats"] == 1.0


def test_readout_texts(project, tmp_path):
    fz = config.load_frozen(project)
    s = make_series(5)
    hl = fz["sectors"]["order"].index("Hlth")
    s.SELL[hl, s.valid - 9 : s.valid + 1] *= 50  # plant heavy selling in one sector
    m = model.Model(s, fz)
    ledger.issue(project, fz, m, now=datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc))
    ledger.score_pending(project, fz, m)
    conn = db.connect(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO issuers(cik, sic, name) VALUES (1, 2834, 'ACME ROCKETS INC')")
    conn.execute(db.INSERT_TRADE, ("A-1", "2026-12-10", "S", 1, 9, "2026-12-11", 5e7, 1e6, 5e7, 1, "test"))
    conn.execute(db.INSERT_TRADE, ("A-2", "2026-12-10", "P", 1, 9, "2026-12-11", 2e6, 1e5, 1e6, 2, "test"))
    board = ledger.scoreboard(project, fz)
    pub, msg = readout.build(conn, project, fz, s, board, date(2026, 12, 15), date(2026, 12, 15))
    assert "Heavier selling than usual: Health and pharma" in pub
    assert "Expected for the week of Nov 23" in pub and "Week of Nov 23: the prediction landed" in pub
    assert "counted so far (through Dec 15)" in pub and "| Health and pharma |" in pub
    assert "Acme" not in pub  # company names never reach the public text
    assert "Sold: Acme Rockets Inc $50 million (one entry is most of this" in msg
    assert "Bought: Acme Rockets Inc $2 million." in msg
    text = report.build(project, fz, board, pub)
    assert text.startswith("# Insider money moves") and "For the record" in text and "Acme" not in text


def test_readout_before_anything_is_written(project, tmp_path):
    fz = config.load_frozen(project)
    conn = db.connect(tmp_path / "t.sqlite")
    board = ledger.scoreboard(project, fz)
    pub, msg = readout.build(conn, project, fz, make_series(3), board, date(2026, 12, 15), date(2026, 12, 15))
    assert "No predicted week has begun yet." in pub and "Sold: none." in msg
