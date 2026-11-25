# The fake-money test

Three pretend accounts start with $100,000 each and follow the rules below from Monday, November 30, 2026 to Friday, February 26, 2027: thirteen weeks. No real money moves and no real order is placed anywhere. The point is to see whether trading on insider filings would have beaten simply holding the market.

The exact numbers are in `paper_rules.json`. Its hash is in `PAPER_RULES.sha256`, the code refuses to run if the two disagree, and the rules cannot change once the first week's orders exist.

## The three accounts

**Group buys.** Each week it looks at the filings of the last five trading days for companies where at least three different insiders bought their own stock on the open market, for at least $100,000 in total, in trades no more than ten trading days old. The company must be listed on the NYSE or Nasdaq, and funds and blank-check shells are left out. Candidates are ranked by how many insiders bought, then by dollars, and up to eight are written down. At the next open the account buys $5,000 of each of the first five that pass three checks made on what was known before the open: the stock closed at $2 or more, it typically trades at least $500,000 a day, and it actually trades that morning. It holds at most twenty stocks and sells each one at the open twenty trading days later.

**Sector tilt.** Each week it scores eleven sectors on the last two complete weeks of filings: how unusual the insider buying was, minus how unusual the insider selling was, each measured against the two years before. It puts a third of the account into the fund of each of the top three sectors and resets to equal thirds at every week's first open.

**Just holding the market.** It buys one broad market fund (SPY) at the first open and holds it. This is the yardstick.

## When things happen

- Orders are written before the week begins, New York time, from filings loaded through the Friday before. A week whose orders are not written in time is skipped. It is never filled in afterwards.
- Buys and sells happen at the opening price of the week's first trading day. Accounts are valued at closing prices.
- A stock trade costs 0.25% of its value each way, and a fund trade 0.05%.
- Cash dividends are added on the ex-dividend day. Share counts follow stock splits.
- On February 26, 2027 everything still held is sold at the close.

## What keeps it honest

Each week's order list is saved on this computer, and a sealed fingerprint of it (a salted SHA-256 hash) is committed to the public ledger under `ledger/paper/` and timestamped before the week begins. Company names stay private; the fingerprint proves the list was not changed later. The sector funds are published in the clear.

The account values are not kept as a running tally that could be edited. Every run recomputes all three accounts from the first day, from the rules, the saved orders and the price history.

## What counts as a result

The reading is given once, after February 26. For each of the two strategy accounts, take its weekly return minus the market account's, for all thirteen weeks.

- **Ahead of the market:** the account ended above the market account, and the average weekly difference is at least 1.645 times its standard error.
- **Behind the market:** the account ended below it, and the average is at most -1.645 standard errors.
- **No clear difference:** anything else.

## What this test can and cannot show

Thirteen weeks is short. It can show that an idea is plainly bad, and it can show that the machinery works. It cannot prove an edge: a strategy with no real advantage ends "ahead" about one time in twenty by luck, and a strategy with a modest real advantage will usually read "no clear difference". A result worth acting on needs many more weeks than this.

The test also leaves things out. A real order moves the price a little, small stocks can be hard to buy at the open, and taxes are ignored. Prices come from Massive (formerly Polygon.io) and are used as given.

This is a measuring exercise. It does not say what to buy or sell.
