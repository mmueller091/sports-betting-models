# Historical training and evaluation

Run commands from `backend/` after installing `requirements.txt`. The commands
use a separate research directory, require no live server, and do not edit the
published dashboard or the pick-history ledger. No historical odds are fetched
unless the paid-fetch command is explicitly invoked.

## Rebuild the free training corpus

```bash
python -m research backfill --league all --seasons 2021 2022 2023 2024 2025 --output ../research-data
python -m research nfl-plays --games ../research-data --seasons 2021 2022 2023 2024 2025 --output ../research-data
```

The second command adds per-game NFL play features and persists the enriched
normalized game files. Keep the cache and source manifests with the normalized
files. Repeated downloads reuse cached bytes unless `--refresh` is given.
NHL seasons use their starting year: 2025 means 2025-26. Postponed and canceled
source records remain visible but are excluded from training and evaluation.

The provided research archive already contains the normalized inputs used in
the reported results. It can be extracted and supplied directly to `--games`
without repeating the larger raw downloads.

## Fit and test football

```bash
python -m research replay --league nfl --candidate current --games ../research-data --start-season 2021 --end-season 2024 --output ../research-output/nfl-current-development
python -m research replay --league nfl --candidate scoring_only --games ../research-data --start-season 2021 --end-season 2024 --output ../research-output/nfl-scoring-development
python -m research replay --league cfb --candidate cfb_fixed_home_calibrated --games ../research-data --start-season 2025 --end-season 2025 --output ../research-output/cfb-final
```

Each run writes `forecasts.jsonl`, `metrics.json`, and `run.json`. Forecasts include
the information cutoff, training sample, probabilities, source provenance,
actual outcome, optional exact-price bet and settlement, and no-bet reasons.
Metrics include margin/total error, Brier score, log loss, calibration bins,
season breakdowns, and week-cluster uncertainty. Lower errors, Brier scores,
and log loss are better. Closing NFL lines are outcome benchmarks, not input
features or evidence of a tradable T-60 price.

Football candidates are `current`, `scoring_only`, `current_calibrated`,
`cfb_fixed_home`, and `cfb_fixed_home_calibrated`. Fixed-home candidates are
restricted to CFB. `--training-scope expanding` allows a separately declared
multi-season training experiment. The reported football comparisons preserve
the existing season-scoped formula, with calibration learned from prior
out-of-fold predictions across seasons.

The experiment protocol was frozen before examining the reserved 2025 results.
That test has now been observed. Future tuning requires another independent
evaluation period; repeatedly tuning against the same 2025 scores does not
create new holdout evidence.

## Fit and test NHL

```bash
python -m research replay --league nhl --candidate nhl_baseline --games ../research-data --start-season 2025 --end-season 2025 --output ../research-output/nhl-baseline
python -m research replay --league nhl --candidate nhl_poisson --games ../research-data --start-season 2025 --end-season 2025 --output ../research-output/nhl-poisson
python -m research compare --left ../research-output/nhl-poisson/forecasts.jsonl --right ../research-output/nhl-baseline/forecasts.jsonl --output ../research-output/nhl-comparison.json
python -m research train-nhl --games ../research-data --cutoff 2026-07-01T00:00:00Z --output ../research-output/nhl-trained.json
```

The learned NHL candidate estimates team attack and defense with regularized
Poisson regression and recency weights. It uses final scores, not reconstructed
historical starting goalies or xG that are absent from this corpus. Its JSON
artifact contains the cutoff, source hash, team ordering, coefficients,
intercept, configuration and sample counts. It remains a research artifact.
It does not automatically replace the live NHL model.

## Prepare exact-price betting tests

```bash
python -m research odds-plan --games ../research-data --league nfl --seasons 2025 --output ../research-output/nfl-2025-odds-plan.json
```

Planning makes no API call and prints the deduplicated request count and credit
budget. Default requests collect all three full-game markets in the US region
at T-60 and a T-5 closing benchmark. A request returns a league slate, so games
sharing a request are not charged separately in the estimate. Review the
manifest and provider entitlement before invoking a paid fetch.

Once an explicit budget is authorized and `ODDS_API_KEY` is set in the local
environment, the executor accepts this form:

```text
python -m research odds-fetch --manifest PATH_TO_REVIEWED_PLAN --output PRIVATE_ODDS_DIRECTORY --max-credits AUTHORIZED_INTEGER_BUDGET --allow-paid
```

The downloader reserves credits before each request, has no automatic paid
retries, and resumes successful cached responses. Errors never print the API
key. The manifest remains readable without credentials. Then normalize and
evaluate offline:

```bash
python -m research odds-normalize --manifest ../research-output/nfl-2025-odds-plan.json --games ../research-data --output ../research-output/nfl-odds
python -m research replay --league nfl --candidate current --games ../research-data --start-season 2025 --end-season 2025 --quotes ../research-output/nfl-odds/entry_quotes.jsonl --closing-quotes ../research-output/nfl-odds/closing_quotes.jsonl --output ../research-output/nfl-priced-final
```

Use the same directory for the paid fetch and normalization. Normalization
checks both team identities and kickoff time; unresolved aliases are recorded
as diagnostics rather than fuzzy-matched. Supply `--aliases verified-names.json`
only after verifying the cross-provider name mapping. Quotes preserve market,
side, line, payout, book, quote time, snapshot time, and source event ID.

`--books` restricts the replay to accessible bookmakers. Set
`--max-quote-age-minutes` to the declared execution policy. The default replay
uses one qualifying bet per game and flat one-unit stakes. ROI remains null
when there are no qualifying exact-price bets. CLV is calculated only for the
same book and contract with a matched opposite-side price.

## Verification

```bash
python -m unittest discover -s tests
```

Regression tests cover future-outcome and late-feature exclusion, whole-fold
holdouts, prior-only calibration, exact-price settlement, superseded lines,
closing-only quote separation, push probability, probability consistency,
identity matching, credit caps, credential redaction, and immutable audit
history. The pull-request workflow runs these without paid data credentials.
