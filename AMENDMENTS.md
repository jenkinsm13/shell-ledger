# Amendments

The rules were frozen on 2026-11-20 before any data was fetched. This file records every change made after that, with the rule that lost kept on the page.

## Amendment 1, 2026-11-20: the data-error cap

**Changed.** `data.drop_row_if_dollars_above`, from $50 billion to $5 billion. Nothing else.

**When.** After the first load of SEC bulk history (2006 through the first quarter of 2026). Before any forecast was written, before any predictor was run on real data, and before any event-test number was computed. The comparison this project exists to make had not been looked at.

**What the frozen rule was for.** Form 4 data contains rows that are not open-market stock trades at the stated size: debt securities entered in the stock table with principal amounts as share counts, and prices entered as totals. The cap exists to drop them. It was set at $50 billion without having seen the data.

**What the data showed.**

- Under the $50 billion cap, qualifying rows summed to $2.15 trillion for 2006 and $2.87 trillion for 2016. Real open-market insider volume is a small fraction of that.
- Among timely rows, counts stop falling with size above $5 billion: 144 rows have a largest line between $2 billion and $5 billion, 139 between $5 billion and $10 billion, and 163 between $10 billion and $50 billion. Real trade sizes thin out as they grow. These do not.
- Sampled rows above $5 billion imply prices such as $100,000, $150,000 and $1,000,000 a share, or millions of units at about $1,000 each, which is how notes are quoted.
- Sampled rows between $1 billion and $2 billion look like real block sales, for example 25,000,000 shares at $61.28.

**Why $5 billion.** It is where the row counts stop behaving like trades. It keeps the block sales that look real and removes the region that is almost entirely error. A few bad rows between $2 billion and $5 billion remain, and a genuine single line above $5 billion would be lost; both are accepted.

**Why it matters.** The series is a logarithm of daily dollars. One false $40 billion row raises a day's market total by more than a factor of ten. With several hundred such rows spread over twenty years, a single mistyped filing in December could have decided the event test.

**The rule that lost.** $50 billion. It stays in the first commit and in `frozen.json` under `amendments`, with its hash in the git history.

**The door closes.** Every forecast file records the hash of the rules it was written under, and the code refuses to run if a forecast exists under a different hash. After the first forecast there are no more amendments.

## The door is already shut in practice

The historical reading was first computed on 2026-11-20, about an hour after Amendment 1 was committed and timestamped. From that moment any further change to the rules would be a change made after seeing how the predictors perform, which is tuning. None will be made, even though the code would still allow one until the first forecast is written.
