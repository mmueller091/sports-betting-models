# NHL historical forecasting results

These are measured chronological forecasts on reconstructed final-score history.
The learned candidate is separate from the corrected live NHL formula.
No historical NHL bookmaker prices were fetched, so these results do not
establish an edge over market prices or profitable betting.

## Development: 2021-22 through 2024-25

Both candidates were evaluated on the same 5,358 eligible forecasts from 5,599
completed games. Early games without 200 prior league games and 10 observations
of each team were retained but excluded from accuracy metrics.

| Candidate | Margin MAE, goals | Total MAE, goals | Brier score | Log loss |
| --- | ---: | ---: | ---: | ---: |
| Recency-weighted league average | 2.27436 | 1.86944 | 0.24858 | 0.69031 |
| Learned team attack/defense Poisson | 2.15566 | 1.85782 | 0.23601 | 0.66472 |

Sources: recorded [baseline](research-results/nhl-2021-2024-baseline.json) and
[learned candidate](research-results/nhl-2021-2024-poisson.json) evaluations.

## Reserved final comparison: 2025-26

All 1,394 completed games were eligible. The weekly refits used at most 730
days of earlier results, 180-day exponential recency weights and Poisson
regularization alpha 0.05. The candidate was not retuned after this test.

| Candidate | Margin MAE, goals | Total MAE, goals | Brier score | Log loss |
| --- | ---: | ---: | ---: | ---: |
| Recency-weighted league average | 2.18970 | 1.83767 | 0.25005 | 0.69326 |
| Learned team attack/defense Poisson | 2.14715 | 1.83304 | 0.24703 | 0.68716 |

Sources: recorded [baseline](research-results/nhl-2025-baseline.json) and
[learned candidate](research-results/nhl-2025-poisson.json) evaluations.

The paired margin improvement was 0.04255 goals per game. Its 95% week-cluster
bootstrap interval was 0.02253 to 0.06245 goals. The total improvement was
0.00463 goals, with an interval that includes zero. The Brier improvement was
0.00302, also with an interval that includes zero. These are useful baseline
results, not evidence sufficient to promote an NHL betting strategy.

The [paired comparison](research-results/nhl-2025-poisson-vs-baseline.json)
contains the exact 500-draw, 34-week cluster intervals and fixed random seed.
The home-win probability average was 53.07%, versus an observed home-win rate
of 51.94%. Calibration remains a concrete area for improvement.

## Saved trained artifact

The [research artifact](research-results/nhl-poisson-2026-07-01-trained.json)
contains coefficients fit to 2,792 prior games at a July 1, 2026 cutoff. It
records its source hash, team ordering, intercept, feature specification and
sample counts. It is not a live deployment or a forecast using the 2026-27
season. Team attack and defense are learned from earlier game outcomes.

The model predicts goal rates, derives a home-win probability, and maps them
into the shared coherent full-game score distribution. That transformation
preserves the final total and supplied win probability, not each individual
raw Poisson team mean exactly. Completed tied final scores are rejected.

## What to test next

The next informative additions are historical goalie starts and goalie quality,
team xG and special-team strength, then market-relative probability learning.
Those features need their own availability timestamps. A player's eventual
appearance in a box score cannot stand in for an earlier announcement.

Historical prices are required before evaluating betting value, closing-line
quality, quote timing, selection thresholds or bankroll policy. The corrected
live model's numerical consistency should not be confused with calibration
learned from these historical outcomes.
