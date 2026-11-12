# CFB free historical odds: priced benchmark, not a tradable backtest

**Study date:** November 12, 2026

**League and seasons:** college football, 2021 through the completed 2025 season
**Live model, website and pick history:** unchanged

## Answer

We can obtain old bookmaker prices without paying The Odds API, but the free
SportsDataverse archive does **not** contain timestamped quotes for the one-hour
pre-kickoff decision time. It provides non-opening archived prices, but does
not certify that every one was the actual closing price. The study therefore
estimates hypothetical flat-stake returns **at those archived prices**, not
returns at a verified close or executable decision time.

The strongest of the three already frozen CFB candidates remains the fixed-home
model with prior-only probability calibration. Its archived-price result is
**-4.45% on 363 selected bets in 2021-2024**, and **+7.56% on 81 bets in 2025**.
Those returns do not demonstrate a reliable edge. Removing the single largest
2025 winning wager turns the +7.56% into **-6.10%**. Its 2025 week-clustered
95% interval spans approximately **-26.34% to +40.17%**.

The archived bookmaker prices also outperform all three models on matched games.
In 2025, archived-market spread margin MAE is **11.76 points**, against **13.25**
for fixed-home and **14.74** for current. Paired-book archived moneyline Brier
is **0.185**, against **0.196** for fixed-home-calibrated and **0.226** for current.
Archived prices may incorporate later news than the model's stated T-60 cutoff,
so these are retrospective benchmarks, not synchronized contests.

## Five-year price-study results

All returns below are hypothetical at unverified archived-price availability.
Each qualifying game receives **at most one moneyline wager**, one unit staked.
The same frozen decision thresholds and 3-point adverse-projection stress are
used for each candidate, with no threshold tuning against realized returns.

| Frozen CFB candidate | 2021-24 priced bets | 2021-24 return | 2025 priced bets | 2025 return |
| --- | ---: | ---: | ---: | ---: |
| Current production formula | 508 | -7.57% | 125 | -29.28% |
| Fixed home advantage | 1,185 | -6.32% | 332 | -9.73% |
| Fixed home + prior-only calibration | 363 | -4.45% | 81 | +7.56% |

The calibrated candidate has inconsistent annual results:

| Season | Forecast-eligible games | Paired-price games | Hypothetical bets | Return at archived price |
| --- | ---: | ---: | ---: | ---: |
| 2021 | 618 | 568 | 84 | +0.6% |
| 2022 | 620 | 564 | 83 | +9.3% |
| 2023 | 636 | 624 | 95 | -7.1% |
| 2024 | 638 | 633 | 101 | -17.4% |
| 2025 | 647 | 635 | 81 | +7.6% |

The market Brier in 2025 is 0.185; even the calibrated candidate is worse at
0.196 on the same 635 games. Lower Brier is better. One illustrative 2025
longshot, Sam Houston over Oregon State at archived +1100, accounts for 11
profit units. Removing it alone flips the entire year's hypothetical return
to negative. Excluding the two largest winners gives -15.67% on the remaining
79 bets. Neither exclusion is offered as a new strategy, only sensitivity.

**A particularly important calibration failure:** on the 81 selected 2025
calibrated-model bets, the model forecast a mean **52.8% win probability**, but
only **34.6%** actually won. For its 363 selected bets in 2021-24, the same
figures were **49.3% predicted versus 32.5% realized**. Selecting only games
where the model strongly disagrees with the bookmaker appears to expose a
weakness that overall Brier improvement does not solve. The model is especially
attracted to underdogs: 68 of its 81 selected 2025 bets had positive American
odds. The weekly-clustered 95% interval for the observed-minus-predicted win
rate is **-25.8 to -11.6 percentage points** in 2025, and **-21.4 to -12.2
points** for 2021-24. These are diagnostics on already-selected subsets, not
new bet rules or validation of T-60 execution.

## Source and completeness

- Source: [SportsDataverse cfbfastR betting archive](https://github.com/sportsdataverse/cfbfastR-data/blob/main/betting/csv/cfb_line_odds.csv.gz), `cfb_line_odds.csv.gz`.
- Direct download: `https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/betting/csv/cfb_line_odds.csv.gz`.
- Download SHA-256 for this exact analysis:
  `7ae899d9035750dccf22470d7923087a2659052aeb37a44204c35fbf6bf38d4d`.
- Normalized completed 2021-25 CFB games: **4,546**.
- Exact ESPN game IDs matched with archive rows: **4,524**.
- Valid paired home/away spread lines: **4,516 games**.
- Paired two-sided moneylines after price-quality filters: **3,891 games**,
  **7,575 book-game pairs**.
- 2025: **635/647** forecast-eligible games have paired moneyline odds.
- The archive contains **no priced spread or total contracts** in 2021-25.
  Spreads support comparison with the market, **not** a spread-return calculation.
- Archive `date_time` is game kickoff, not a price observation timestamp.
  Neither its `lines` nor `odds` columns should be presented as a historical
  T-60 snapshot, or as proof a specific book offered a line simultaneously
  with another book.

The importer checks both teams' ESPN IDs, matches each archived book's home
and away contract, rejects conflicting duplicates and extreme paired vig,
and quarantines moneyline favorites that strongly contradict the archived
spread consensus. On this import, it rejected 42 suspicious paired moneylines
for favorite/spread inconsistency. Those filters are applied before outcomes
or wager profitability are consulted. All rejected-count definitions are in
the machine-readable `report.json`.

The archives also contain 126 rows with home/away ESPN IDs that do not match
the normalized game record. Those rows are rejected rather than joined by
fuzzy team names. The full game-level reference contains no fabricated odds.

## Study procedure and leakage controls

1. Reuse the **frozen** 2021-2024 development forecasts and the previously
   evaluated 2025 final forecasts, all created by chronological weekly replay.
   Do not refit predictions using archived odds.
2. Match bookmaker contracts using exact ESPN game IDs and both team IDs.
   For moneylines, require one valid home and away price from the **same book**.
3. For each historical week, reconstruct the prior-only calibrator exclusively
   from forecast-eligible games whose recorded final result was available before
   the weekly training cutoff. Verify reconstructed probabilities match the
   archived frozen probabilities, otherwise abort.
4. Reconstruct same-season prior forecast errors and verify their counts match
   the frozen weekly input. Preserve the original minimum team/history/held-out
   requirements, model-better-than-simple-baseline check, minimum 6-percentage-
   point edge, minimum $10 expected profit per $100, and positive expected
   profit after a 3-point adverse projection shift.
5. Among qualifying two-sided book moneylines, retain only the highest
   conservative expected-value wager per game. Settle against the verified
   historical final. Resample **entire weeks**, not individual bets, for the
   descriptive 95% intervals.
6. For paired game benchmarks, use the median archived home margin and the
   median book-level no-vig moneyline probability. Calculate spread margin
   MAE and binary Brier scores on exactly matched eligible games.
7. Explicitly set `asof_or_t60_returns_calculated=false` and
   `roi_is_executable=false`. The real historical time a bookmaker quote became
   available is unknown; do not populate synthetic quote timestamps.

**Important:** The 2025 forecast season was already examined in earlier model
comparison work. These priced results are incremental evidence on that same
evaluated season, **not** a newly untouched holdout. The archived schedules
and prices may themselves reflect revised retrospective editions.

## Reproduction

From repository root, with the original normalized games and frozen forecasts:

```bash
curl -fL 'https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/betting/csv/cfb_line_odds.csv.gz' -o cfb_line_odds.csv.gz
sha256sum cfb_line_odds.csv.gz
PYTHONPATH=backend python -m research cfb-free-prices \
  --archive cfb_line_odds.csv.gz \
  --games historical-data/normalized/cfb_2021.jsonl historical-data/normalized/cfb_2022.jsonl historical-data/normalized/cfb_2023.jsonl historical-data/normalized/cfb_2024.jsonl historical-data/normalized/cfb_2025.jsonl \
  --development-dir historical-results/football-development \
  --final-dir historical-results/football-final \
  --output historical-results/cfb-free-archived-prices
```

`historical-data` and `historical-results` above refer to the separate
downloaded research datasets, not folders committed to the website. The CLI
prints exact source and output checksums and writes:

- `report.json`: price coverage, rejected rows, period and yearly market/model
  comparisons, hypothetical returns, and week-resampled uncertainty.
- `selected_bets.jsonl`: game-by-game hypothetical decisions, realized outcomes,
  unverified price-observation time, book/side and explicit non-executable basis.
- `market_reference.jsonl`: per-game paired book prices and spread reference,
  suitable for descriptive historical comparison only.

No API key, historical API credits or paid subscription are required.

## Where a genuine edge might come from next

This study does **not** justify deploying any of the three candidates for real
betting or training against retrospective archived prices. The clearest research
priorities, in order:

1. **Acquire true point-in-time moneyline/spread/total quotes.** A forward
   collector can start retaining observed-at timestamps for free current odds,
   with book, market, side and line/version. Historical T-60 trading evidence
   still requires old timestamped archives or a provider that preserves them.
2. **Fix calibration before estimating edge.** The fixed-home candidate closes
   part of the margin gap, and prior-only calibration closes part of the Brier
   gap. Both still trail the market on matched games, and the selected bets
   exhibit substantial overconfidence. Compare residual errors by conference,
   opponent strength, season, and favorite/underdog probability, strictly out
   of sample, before changing any weights.
3. **Model market agreement, with availability proven.** When an actual T-60
   price exists, compare the model estimate against same-time no-vig market
   probabilities or a market-derived margin. Do not inject closing lines as
   purportedly known T-60 training features.
4. **Walk forward to a new, untouched season.** Freeze a candidate and thresholds
   using 2021-25 data, then evaluate 2026+ on truly recorded quote timestamps,
   with closing-line value versus later same-book snapshots and no selective
   removal of losing games.

**Decision:** keep production, the live picks and all published odds logic
unchanged. Archive this as a five-year free-price baseline. The numerical
evidence is currently against claiming a reliable, executable advantage.
