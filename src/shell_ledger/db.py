"""Local SQLite store. Holds per-filer rows, so it stays out of git."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    accession  TEXT NOT NULL,
    trans_date TEXT NOT NULL,
    code       TEXT NOT NULL,
    issuer_cik INTEGER NOT NULL,
    owner_cik  INTEGER,
    filed_date TEXT NOT NULL,
    dollars    REAL NOT NULL,
    shares     REAL NOT NULL,
    max_row    REAL NOT NULL,
    lots       INTEGER NOT NULL,
    src        TEXT NOT NULL,
    PRIMARY KEY (accession, trans_date, code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS issuers (
    cik INTEGER PRIMARY KEY,
    sic INTEGER,
    name TEXT
);
CREATE TABLE IF NOT EXISTS fetch_log (
    key  TEXT PRIMARY KEY,
    info TEXT
);
CREATE TABLE IF NOT EXISTS live_done (
    accession TEXT PRIMARY KEY
) WITHOUT ROWID;
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    # TRUNCATE keeps the rollback journal as a file instead of deleting it,
    # which also works in folders where deletes are not permitted.
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def log_get(conn, key: str):
    row = conn.execute("SELECT info FROM fetch_log WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def log_set(conn, key: str, info: str) -> None:
    conn.execute("INSERT OR REPLACE INTO fetch_log(key, info) VALUES (?, ?)", (key, info))


INSERT_TRADE = (
    "INSERT OR IGNORE INTO trades(accession, trans_date, code, issuer_cik, owner_cik, "
    "filed_date, dollars, shares, max_row, lots, src) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
)
