# shell-ledger

A local tool that reads public SEC insider filings, writes dated forecasts of next week's insider dollar volume per sector, and scores itself when the filings arrive. A golden-ratio shell model competes against plain controls under rules frozen in advance.

The rules are in [PREREGISTRATION.md](PREREGISTRATION.md) and `frozen.json`. The code checks `frozen.json` against `FROZEN.sha256` on every run, and refuses to run if any forecast in the ledger was written under a different hash. The one change made after the freeze, before any forecast, is recorded in [AMENDMENTS.md](AMENDMENTS.md).

This is a measuring instrument. It does not say what to buy or sell.

## Setup

1. Install [uv](https://docs.astral.sh/uv/), then run `uv sync` in this folder.
2. Copy `.env.example` to `.env` and put your name and email on the `SEC_USER_AGENT` line. The SEC requires a contact in every automated request and refuses requests without one. `.env` is never committed.

## Run

```
bash scripts/weekly.sh
```

One pass does everything: loads new filings, scores any week that has settled, writes next week's forecast, updates the event test, writes a report, then commits the ledger, timestamps the new forecast and pushes.

The first run downloads Form 4 history back to 2006 and looks up each issuer's industry code. That takes a few hours and can be stopped and restarted; it resumes where it left off. Until it finishes, nothing is written to the ledger.

Run it once a week, between Friday night and Sunday night New York time. A forecast that is not written before Monday is skipped for good.

Single steps: `uv run shell-ledger status | ingest | issue | score | historical | event | report | check`.

## Where things are

| Path | What | Public |
| --- | --- | --- |
| `frozen.json`, `FROZEN.sha256` | The rules and their hash | yes |
| `ledger/forecasts/` | One file per target week, written before the week | yes |
| `ledger/scores/` | One file per settled week | yes |
| `ledger/historical/` | The same rules run over past weeks | yes |
| `ledger/event/` | Event-test readings | yes |
| `ledger/reports/` | Plain-language weekly reports | yes |
| `data/shell_ledger.sqlite` | Filings, including per-filer rows | no |
| `.env` | Your SEC contact line | no |

## Not built yet

Forms 13D/G and 13F, congressional trade disclosures and sector fund flows are not ingested. The frozen target uses Form 4 only, because it is the one source with exact trade dates, prices and a two-day filing deadline. The others can be added later as descriptive series without touching the frozen rules.

## Tests

```
uv run pytest
```
