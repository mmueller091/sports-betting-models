# Historical football research results

Recorded November 12, 2026. The 2026 audit was discovery work. Candidate formulas
were fixed before the 2025 comparison, according to the
[research protocol](research-protocol.md). The production football configuration
was not changed by these experiments.

## Main findings

The CFB candidate that holds the existing 2.5-point home prior fixed improves
margin forecasts and win probabilities across every evaluated season. Applying
moneyline calibration fitted only to earlier out-of-fold forecasts improves
probability accuracy further. These are forecasting improvements. The CFB
dataset has no corresponding historical bookmaker benchmark or executable
quote archive, so profitable betting has not been established.

The NFL scoring-only candidate improves point error slightly, but its 2025
paired uncertainty intervals include no improvement. The tested NFL calibration
candidate performs worse. The available closing market beats every tested NFL
candidate on 2025 score errors and probability accuracy.

## Data and evaluation coverage

NFL play-by-play features were matched for every one of the 1,424 completed NFL
games. CFB includes FBS games and their FCS opponents, with 2021 and 2022 bowl
results supplemented from archived ESPN schedules. Incomplete or canceled
events do not become completed training observations.

| Season | Completed NFL games | Eligible NFL forecasts | Completed CFB games | Eligible CFB forecasts |
|---|---:|---:|---:|---:|
| 2021 | 285 | 237 | 887 | 618 |
| 2022 | 284 | 236 | 896 | 620 |
| 2023 | 285 | 237 | 910 | 636 |
| 2024 | 285 | 237 | 919 | 638 |
| 2025 final comparison | 285 | 237 | 934 | 647 |
| Total | 1,424 | 1,184 | 4,546 | 3,159 |

An eligible forecast has the predeclared league-wide training sample and at
least three earlier games for both teams. Earlier warm-up games remain in the
decision output, but do not inflate reported forecast accuracy. Betting has
additional sample, price, and uncertainty gates. These are the full eligible
forecast universes, not selected betting picks. Multiple candidates evaluated
on the same game are not additional independent observations.

All 237 eligible 2025 NFL forecasts used the advanced features in the current
candidate. The scoring-only comparison actually removed those features; it is
not an experiment where both candidates silently used the same fallback.

Coverage and season-level metrics are recorded in the
[NFL development report](research-results/nfl-2021-2024-current.json) and
[CFB development report](research-results/cfb-2021-2024-current.json), with
2025 in the final reports linked below.

## Development results, 2021 through 2024

Lower error and Brier scores are better. Margin and total errors are mean
absolute errors in points. Brier measures the squared error of a probability.

| League and candidate | Eligible games | Margin MAE | Total MAE | Win-probability Brier |
|---|---:|---:|---:|---:|
| NFL current | 947 | 10.481281 | 10.845927 | 0.228810 |
| NFL scoring-only | 947 | 10.310741 | 10.760801 | 0.227008 |
| NFL current plus calibration | 947 | 10.481281 | 10.845927 | 0.230339 |
| CFB current | 2,512 | 14.469187 | 13.095212 | 0.220082 |
| CFB fixed home prior | 2,512 | 13.285199 | 13.095212 | 0.208310 |
| CFB fixed home prior plus calibration | 2,512 | 13.285199 | 13.095212 | 0.198204 |

In the latest development season, 2024, CFB margin MAE improved from 13.987945
to 13.026318; Brier improved from 0.216379 to 0.208262, then to 0.197202 with
calibration. NFL scoring-only improved margin MAE from 10.521557 to 10.272872,
but its 2024 Brier was slightly worse, 0.217487 versus 0.215597.

## Frozen 2025 final comparison

The three candidates for each league were evaluated once with the predeclared
parameters. Later report calculations reused the recorded predictions. No
candidate parameters were changed in response to these results.

### NFL

| Candidate | Margin MAE | Total MAE | Brier | Log loss |
|---|---:|---:|---:|---:|
| [Current](research-results/nfl-2025-current.json) | 10.375324 | 10.514004 | 0.225861 | 0.641579 |
| [Scoring-only](research-results/nfl-2025-scoring_only.json) | 10.279479 | 10.377390 | 0.224931 | 0.640051 |
| [Current plus calibration](research-results/nfl-2025-current_calibrated.json) | 10.375324 | 10.514004 | 0.228159 | 0.646664 |
| Closing-market benchmark | 9.850211 | 9.997890 | 0.214113 | See current report |

Score metrics use 237 games. Binary probability metrics use 236 games, excluding
one actual tie. The closing probability benchmark removes vig from the paired
closing moneyline prices. These closing fields have no usable decision-time
quote timestamp and are used only as benchmarks.

The [paired comparison](research-results/nfl-2025-scoring_only-vs-current.json)
resamples 19 complete league weeks. Differences are scoring-only minus current:

| Metric | Mean difference | 95% interval |
|---|---:|---:|
| Margin MAE | -0.095845 | [-0.386736, 0.166490] |
| Total MAE | -0.136614 | [-0.438375, 0.127611] |
| Brier | -0.000931 | [-0.007449, 0.004768] |

These small point estimates do not support promoting a replacement NFL model.

### College football

| Candidate | Margin MAE | Total MAE | Brier | Log loss |
|---|---:|---:|---:|---:|
| [Current](research-results/cfb-2025-current.json) | 14.738082 | 12.608155 | 0.221801 | 0.630409 |
| [Fixed home prior](research-results/cfb-2025-cfb_fixed_home.json) | 13.250189 | 12.608155 | 0.205496 | 0.599166 |
| [Fixed home prior plus calibration](research-results/cfb-2025-cfb_fixed_home_calibrated.json) | 13.250189 | 12.608155 | 0.192830 | 0.563329 |

All metrics use 647 games. The fixed prior improves margin MAE by 1.487893
points, approximately 10.1%. Totals do not change because these candidates
modify the margin and moneyline paths only.

The paired intervals resample 18 complete league weeks:

| Comparison with current | Metric | Mean difference | 95% interval |
|---|---|---:|---:|
| [Fixed prior](research-results/cfb-2025-cfb_fixed_home-vs-current.json) | Margin MAE | -1.487893 | [-2.439473, -0.732625] |
| Fixed prior | Brier | -0.016305 | [-0.026684, -0.005722] |
| [Fixed prior plus calibration](research-results/cfb-2025-cfb_fixed_home_calibrated-vs-current.json) | Brier | -0.028971 | [-0.039017, -0.018168] |

The simple home-field-only margin baseline scored 15.341577 MAE in 2025.
No CFB closing-market accuracy comparison is available from these inputs.

## What this proves and what remains untested

This work demonstrates an operational historical training and replay process,
and a reproducible CFB improvement on a previously unexamined season. It does
not establish historical returns. Every report has zero executable-price bets
and null ROI because timestamped historical entry quotes were not supplied.
No missing prices were replaced by assumed -110 odds.

Each model is fit before the first decision in its weekly fold. All outcomes in
that fold remain outside training until later folds. Residual uncertainty and
moneyline calibration use only earlier available out-of-fold predictions.
Calibration requires at least 100 prior non-tied forecasts and at least 20 of
each outcome, with fixed logistic regularization and a positive slope.

Historical data uses revised source editions with an explicit reconstructed
availability convention of kickoff plus 12 hours. This enforces a conservative
information boundary for prior-game statistics but is not a claim that the
source bytes or injury reports were archived at the original prediction time.
Source hashes and preserved normalized editions identify the inputs used.

The next independent validation requires exact timestamped bookmaker quotes
and the complete predeclared decision strategy. The 2025 season is now observed
evidence. Further tuning needs another independent test period.
