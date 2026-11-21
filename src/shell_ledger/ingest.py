"""Form 4 ingest: SEC bulk quarterly sets for history, the EDGAR daily index for recent days."""
from __future__ import annotations

import io
import json
import re
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, secweb

DATASET_PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/master.{ymd}.idx"
FILING_URL = "https://www.sec.gov/Archives/{name}"
LISTING_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/index.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

WORKERS = 8  # threads share one pacer in secweb, so the request rate stays at 8 a second
BATCH = 48

DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%d-%b-%y")
CODES = ("P", "S")


def parse_date(s: str):
    s = (s or "").strip()[:11].strip()
    if not s:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_num(s: str):
    try:
        v = float((s or "").replace(",", "").strip())
    except ValueError:
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def quarter_end(y: int, q: int) -> date:
    return date(y + (q == 4), q % 4 * 3 + 1, 1) - timedelta(days=1)


def quarters(first=(2006, 1), until: date | None = None):
    """Quarters from `first` up to the last one that ended before `until`."""
    until = until or date.today()
    y, q = first
    while quarter_end(y, q) < until:
        yield y, q
        y, q = (y, q + 1) if q < 4 else (y + 1, 1)


def fetch_many(urls):
    """Fetch several URLs at the paced rate. Order is kept."""
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(secweb.get, urls))


# ---------------------------------------------------------------- bulk sets

_LINK = re.compile(r'href="([^"]*?/(\d{4})q([1-4])_form345\.zip)"', re.I)


def parse_bulk_links(html: str) -> dict:
    """Quarter -> download URL, read from the SEC's own dataset page."""
    out = {}
    for href, y, q in _LINK.findall(html):
        out[(int(y), int(q))] = href if href.startswith("http") else "https://www.sec.gov" + href
    return out


def bulk_links() -> dict:
    page = secweb.get(DATASET_PAGE)
    if not page:
        raise secweb.SecError("The SEC dataset page was not found.")
    links = parse_bulk_links(page.decode("utf-8", "replace"))
    if not links:
        raise secweb.SecError("No quarterly files are listed on the SEC dataset page.")
    return links


def _col(header, *names):
    up = [h.strip().upper() for h in header]
    for n in names:
        if n in up:
            return up.index(n)
    raise KeyError(f"none of {names} in columns {up}")


def _rows(zf: zipfile.ZipFile, stem: str):
    """Yield (header, row) for the tab-separated member whose base name is `stem`."""
    name = next(
        (n for n in zf.namelist() if Path(n).name.upper().rsplit(".", 1)[0] == stem.upper()),
        None,
    )
    if name is None:
        raise KeyError(f"{stem} not in archive: {zf.namelist()}")
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        header = text.readline().rstrip("\r\n").split("\t")
        for line in text:
            yield header, line.rstrip("\r\n").split("\t")


def aggregate(rows, cap: float):
    """rows: (accession, trans_date, code, shares, price). Sum per accession, day and code."""
    agg = {}
    for acc, td, code, shares, price in rows:
        if code not in CODES or td is None or shares is None or price is None:
            continue
        if shares <= 0 or price <= 0:
            continue
        dollars = shares * price
        if dollars > cap:
            continue
        a = agg.setdefault((acc, td, code), [0.0, 0.0, 0.0, 0])
        a[0] += dollars
        a[1] += shares
        a[2] = max(a[2], dollars)
        a[3] += 1
    return agg


def load_bulk_zip(conn, zip_path, src: str, cap: float) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        sub = {}
        ia = it = ii = ifd = None
        for header, r in _rows(zf, "SUBMISSION"):
            if ia is None:
                ia = _col(header, "ACCESSION_NUMBER")
                it = _col(header, "DOCUMENT_TYPE")
                ii = _col(header, "ISSUERCIK", "ISSUER_CIK")
                ifd = _col(header, "FILING_DATE")
            if len(r) <= max(ia, it, ii, ifd) or r[it].strip() != "4":
                continue
            filed = parse_date(r[ifd])
            cik = parse_num(r[ii])
            if filed and cik:
                sub[r[ia].strip()] = (filed, int(cik))
        own = {}
        ia = io_ = None
        for header, r in _rows(zf, "REPORTINGOWNER"):
            if ia is None:
                ia = _col(header, "ACCESSION_NUMBER")
                io_ = _col(header, "RPTOWNERCIK", "RPTOWNER_CIK")
            if len(r) <= max(ia, io_):
                continue
            acc = r[ia].strip()
            c = parse_num(r[io_])
            if acc in sub and c:
                own[acc] = min(int(c), own.get(acc, int(c)))

        def trans():
            ia = None
            for header, r in _rows(zf, "NONDERIV_TRANS"):
                if ia is None:
                    ia = _col(header, "ACCESSION_NUMBER")
                    idt = _col(header, "TRANS_DATE")
                    ic = _col(header, "TRANS_CODE")
                    ish = _col(header, "TRANS_SHARES")
                    ip = _col(header, "TRANS_PRICEPERSHARE", "TRANS_PRICE_PER_SHARE")
                    need = max(ia, idt, ic, ish, ip)
                if len(r) <= need:
                    continue
                acc = r[ia].strip()
                if acc in sub:
                    yield acc, parse_date(r[idt]), r[ic].strip().upper(), parse_num(r[ish]), parse_num(r[ip])

        agg = aggregate(trans(), cap)
    conn.executemany(
        db.INSERT_TRADE,
        [
            (acc, td, code, sub[acc][1], own.get(acc), sub[acc][0], a[0], a[1], a[2], a[3], src)
            for (acc, td, code), a in agg.items()
        ],
    )
    return {"form4_filings": len(sub), "trade_rows": len(agg)}


def ingest_bulk(conn, cap: float, max_seconds: float = 150, log=print) -> bool:
    """Load every posted quarter not yet loaded. Returns True when nothing is left."""
    t0 = time.monotonic()
    links = None
    for y, q in quarters():
        key = f"bulk:{y}q{q}"
        if db.log_get(conn, key):
            continue
        if time.monotonic() - t0 > max_seconds:
            return False
        links = links or bulk_links()
        data = secweb.get(links[(y, q)]) if (y, q) in links else None
        if data is None:
            log(f"{y}q{q}: not posted yet")
            return True  # later quarters cannot be posted either
        with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
            tmp.write(data)
            tmp.flush()
            stats = load_bulk_zip(conn, tmp.name, f"bulk:{y}q{q}", cap)
        stats["bytes"] = len(data)
        db.log_set(conn, key, json.dumps(stats))
        conn.commit()
        log(f"{y}q{q}: {stats}")
    return True


def bulk_through(conn):
    """Last filing date covered by the bulk sets already loaded, as a date."""
    last = None
    for y, q in quarters():
        if not db.log_get(conn, f"bulk:{y}q{q}"):
            break
        last = quarter_end(y, q)
    return last


# ---------------------------------------------------------------- live index

_MASTER = re.compile(r"master\.(\d{8})\.idx$")


def parse_listing(raw: bytes) -> set:
    """The dates that have a master index, from a quarter's directory listing."""
    out = set()
    for item in json.loads(raw)["directory"]["item"]:
        m = _MASTER.match(item.get("name", ""))
        if m:
            out.add(datetime.strptime(m.group(1), "%Y%m%d").date())
    return out


def parse_master(text: str):
    """Yield (form, filed_iso, file_name) from an EDGAR master index."""
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) == 5 and parts[4].strip().endswith(".txt"):
            filed = parse_date(parts[3])
            if filed:
                yield parts[2].strip(), filed, parts[4].strip()


_XML = re.compile(r"<XML>\s*(.*?)\s*</XML>", re.S | re.I)


def _text(node, path):
    el = node.find(path)
    return el.text.strip() if el is not None and el.text else None


def parse_form4(text: str):
    """Parse a full-submission text file. Returns None unless it is a plain Form 4."""
    m = _XML.search(text)
    if not m:
        return None
    try:
        root = ET.fromstring(m.group(1).strip())
    except ET.ParseError:
        return None
    if root.tag != "ownershipDocument" or (_text(root, "documentType") or "") != "4":
        return None
    issuer = parse_num(_text(root, "issuer/issuerCik") or "")
    owners = [parse_num(e.text or "") for e in root.findall("reportingOwner/reportingOwnerId/rptOwnerCik")]
    owners = [int(o) for o in owners if o]
    rows = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        rows.append(
            (
                parse_date(_text(t, "transactionDate/value") or ""),
                (_text(t, "transactionCoding/transactionCode") or "").upper(),
                parse_num(_text(t, "transactionAmounts/transactionShares/value") or ""),
                parse_num(_text(t, "transactionAmounts/transactionPricePerShare/value") or ""),
            )
        )
    if not issuer:
        return None
    return {"issuer": int(issuer), "owner": min(owners) if owners else None, "rows": rows}


def ingest_live(conn, cap: float, max_seconds: float = 150, today: date | None = None, log=print) -> bool:
    """Walk the daily index from the day after the bulk sets end. True when caught up."""
    t0 = time.monotonic()
    today = today or date.today()
    start = bulk_through(conn)
    if start is None:
        raise secweb.SecError("Load the bulk history first (ingest-bulk).")
    d = start
    listings = {}
    while True:
        d += timedelta(days=1)
        if d > today:
            return True
        if d.weekday() >= 5:
            continue
        key = f"idx:{d.isoformat()}"
        if db.log_get(conn, key):
            continue
        q = (d.month - 1) // 3 + 1
        if (d.year, q) not in listings:
            raw = secweb.get(LISTING_URL.format(y=d.year, q=q))
            listings[(d.year, q)] = parse_listing(raw) if raw else set()
        posted = listings[(d.year, q)]
        if d not in posted:
            if posted and max(posted) > d:  # a later day is posted, so this one never will be: a holiday
                db.log_set(conn, key, "none")
                conn.commit()
                continue
            return True  # not posted yet
        raw = secweb.get(INDEX_URL.format(y=d.year, q=q, ymd=d.strftime("%Y%m%d")))
        if raw is None:
            return True
        todo = {}
        for form, filed, name in parse_master(raw.decode("latin-1")):
            if form == "4":
                todo[Path(name).stem] = (filed, name)
        pending = [
            (acc, filed, name)
            for acc, (filed, name) in todo.items()
            if not conn.execute("SELECT 1 FROM live_done WHERE accession=?", (acc,)).fetchone()
        ]
        for i in range(0, len(pending), BATCH):
            if time.monotonic() - t0 > max_seconds:
                log(f"{d}: paused, {len(pending) - i} of {len(todo)} filings left")
                return False
            part = pending[i : i + BATCH]
            bodies = fetch_many([FILING_URL.format(name=name) for _, _, name in part])
            for (acc, filed, _), body in zip(part, bodies):
                doc = parse_form4(body.decode("latin-1")) if body else None
                if doc:
                    agg = aggregate(((acc, *r) for r in doc["rows"]), cap)
                    conn.executemany(
                        db.INSERT_TRADE,
                        [
                            (a, td, code, doc["issuer"], doc["owner"], filed, v[0], v[1], v[2], v[3], "live")
                            for (a, td, code), v in agg.items()
                        ],
                    )
                conn.execute("INSERT OR IGNORE INTO live_done(accession) VALUES (?)", (acc,))
            conn.commit()
        db.log_set(conn, key, str(len(todo)))
        conn.commit()
        log(f"{d}: {len(todo)} Form 4 filings")


def filed_through(conn, today: date | None = None):
    """The last filing date for which every Form 4 is loaded."""
    today = today or date.today()
    d = bulk_through(conn)
    if d is None:
        return None
    while True:
        nxt = d + timedelta(days=1)
        if nxt > today:
            return d
        if nxt.weekday() < 5 and not db.log_get(conn, f"idx:{nxt.isoformat()}"):
            return d
        d = nxt


# ---------------------------------------------------------------- issuer SIC

def ingest_sic(conn, max_seconds: float = 150, log=print) -> bool:
    """Look up the SIC code of every issuer not yet looked up, largest first."""
    t0 = time.monotonic()
    todo = [
        r[0]
        for r in conn.execute(
            "SELECT issuer_cik FROM trades WHERE issuer_cik NOT IN (SELECT cik FROM issuers) "
            "GROUP BY issuer_cik ORDER BY SUM(dollars) DESC"
        )
    ]
    for i in range(0, len(todo), BATCH):
        if time.monotonic() - t0 > max_seconds:
            log(f"sic: paused, {len(todo) - i} issuers left")
            return False
        part = todo[i : i + BATCH]
        for cik, raw in zip(part, fetch_many([SUBMISSIONS_URL.format(cik=c) for c in part])):
            sic, name = None, None
            if raw:
                try:
                    j = json.loads(raw)
                    sic = int(j["sic"]) if str(j.get("sic") or "").strip().isdigit() else None
                    name = j.get("name")
                except (ValueError, KeyError):
                    pass
            conn.execute("INSERT OR IGNORE INTO issuers(cik, sic, name) VALUES (?,?,?)", (cik, sic, name))
        conn.commit()
    conn.commit()
    return True
