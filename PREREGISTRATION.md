# Pre-registration

Frozen 2026-11-20, before any data was fetched. `frozen.json` is the rule; this page says the same thing in plain words. `FROZEN.sha256` holds the hash of `frozen.json`, and the code refuses to run if the two disagree.

Amended once, the same day, before any forecast was written or any predictor was run: the data-error cap went from $50 billion to $5 billion a line. The reason and the evidence are in [AMENDMENTS.md](AMENDMENTS.md). No further amendment is possible once the first forecast exists.

## The model, in its author's sentence

> Shell k is the trailing window of φ^k trading days. What moves between shells is dollars traded, bought plus sold, per sector. Shells k apart couple with weight c(k), nothing fitted.

## What is measured

- **Source.** SEC Form 4 as filed on EDGAR, non-derivative table. Amendments (4/A) and Form 5 are excluded.
- **Rows that count.** Open-market purchases and sales (codes P and S) with positive shares and price. Dollars are shares times price. A single line above $5 billion is treated as a data error and dropped (originally $50 billion; see the amendment).
- **Timely only.** A row counts only if it was filed by the third trading day after the trade. This makes every day's total final three trading days later, the same way in 2006 and next week.
- **Sectors.** Twelve industry groups by the issuer's SIC code. The ranges are in `frozen.json`.
- **Daily series.** L = log10(sector dollars + 10,000).
- **Target.** For each sector and each Monday-to-Friday week: the mean of L over that week's trading days.

Direction (buys minus sells) is logged every week and never scored.

## When forecasts are written

A forecast for week W uses days up to three trading days before the last trading day preceding W. It must exist before Monday 00:00 New York time. A week with no forecast by then is skipped and never backfilled. Forecast and score files are written once.

## The predictors

Each blend is a weighted average of trailing-window means of L, with weights set by shell distance from the shell nearest the five-day period.

| | Curve weights c(k) | Plain fade e^(−k/φ) | Equal weights |
| --- | --- | --- | --- |
| **Golden-ratio windows** (1, 2, 3, 4, 7, 11, … 521 days) | **Shell model** | control | control |
| **Doubling windows** (1, 2, 4, 8, … 512 days) | control | **Primary control** | control |

Also reported: Baseline A (trailing 521-day mean), Baseline B (trailing 5-day mean), and HAR (least squares on 1-, 5- and 22-day means, refit weekly on settled weeks only).

c(k) = e^(−k/φ) / (1 + cos²(2k ln φ)).

## The verdict

- **Metric.** Absolute error in log10 units, averaged over sectors and weeks.
- **One comparison decides.** The shell model against the primary control, which keeps a sensible fade and removes both the golden ratio and the curve.
- **Statistic.** Weekly difference in mean absolute error, control minus shell model; its mean over its HAC standard error (Bartlett kernel, 4 lags).
- **Win** if t > 1.645. **Lose** if t < −1.645. Otherwise **tie**: no detectable work from the golden ratio or the curve.
- **When.** Read once, after the first 26 weeks whose forecasts were written on time. Earliest target week: 2026-11-23.

Every other predictor is diagnostic. None of them can turn a tie into a win.

## The historical reading

The same frozen predictors are run over every settled week from 2008-03-03 to 2026-11-09 and scored with the same statistic. It is retrospective, so it is a reading and not the verdict. It is worth reporting because the rules were fixed before the data was fetched and the shell model and its control contain nothing fitted.

## The event test: "the moves start now"

- **Window.** Trading days from November 20 through December 31.
- **Reference.** January 2 through November 19 of the same year.
- **Uplift.** Mean of log10(all-sector dollars + 10,000) over the window, minus the same over the reference.
- **Pass.** The 2026 uplift is the largest of the 21 years from 2006 to 2026. By luck that happens 1 time in 21.
- **Final** on 2027-01-06. Earlier readings are interim and decide nothing.
- **Same filers (reported only).** For each year, the panel is every reporting owner active in each of the three prior years; the statistic is the log ratio of the panel's participation in the window to its participation in the reference.

## What stays private

The public ledger holds sector-level numbers only. Rows about individual filers stay in the local database.
