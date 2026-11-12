# Historical research protocol

Protocol frozen before inspecting the 2025 season comparison, November 12, 2026.
The 2026 production audit motivated the candidates, so 2026 is discovery data.
2021 through 2024 are development seasons; 2025 is the final comparison season.
This is a one-time chronological holdout, not a claim that historical data can
remain untouched after results have been inspected.

## Candidates

| League | Candidate | Fixed specification |
|---|---|---|
| NFL | current | Existing opponent-adjusted Ridge models with prior-game play features |
| NFL | scoring_only | Same models, with advanced play features removed |
| NFL | current_calibrated | Existing models plus positive-slope logistic moneyline calibration trained on earlier out-of-fold predictions |
| CFB | current | Existing production scoring model |
| CFB | cfb_fixed_home | Refit team ratings while holding the existing 2.5-point home prior fixed |
| CFB | cfb_fixed_home_calibrated | Fixed home prior plus earlier-fold moneyline calibration |
| NHL | nhl_baseline | Recency-weighted league average home and away goals |
| NHL | nhl_poisson | Poisson attack/defense team indicators and home indicator; alpha 0.05, 180-day exponential recency, 730-day history |

Football uses its existing season-scoped formula. Expanding history is an
available separate experiment, not silently substituted for the current model.
Moneyline calibration uses at least 100 prior non-tied forecasts, at least 20 of
each outcome, and logistic C=1. It does not calibrate spread or total forecasts.
NHL requires 200 prior games and 10 observations of each target team. All
candidate parameters above are fixed for the reported final comparison.

## Information boundary

Each game has a decision time 60 minutes before kickoff. A weekly model is fit
before the earliest decision in its week; every game in that week is withheld
from the fit. Only completed results and features with availability strictly
before the fit can enter training. Historical archives are revised editions:
the manifest labels them as reconstruction, not contemporaneously saved feeds.
The default reconstructed result/feature availability is kickoff plus 12 hours.
That convention prevents same-day results entering too early but cannot prove
exact historical source publication or eliminate later statistical corrections.

All games are retained, including insufficient-history forecasts and no-bet
reasons. Accuracy scores use the declared eligible forecast universe. Candidate
comparisons use the same eligible games. Uncertainty intervals resample entire
league weeks. A confidence interval does not repair repeated model selection.

## Betting boundary

Forecast training is possible without historical bookmaker prices. A betting
simulation additionally requires exact market, side, line, American payout,
bookmaker and quote timestamp at or before the decision. The latest snapshot
supersedes older lines from the same book and market. Future and stale quotes
are rejected. Missing prices never become assumed -110 bets.

The research strategy selects at most one qualifying bet per game. It does not
claim to replay the website's hourly top-five slate, which needs actual hourly
snapshots. Football retains its existing conservative three-point sensitivity
and prior-validation gates. NHL uses a declared 2% expected return threshold,
120-minute quote-age limit and one unit per bet. This NHL rule is an experiment,
not a threshold tuned to the reported outcomes or a new production policy.

Profit, pushes and losses use the exact quoted price. Closing benchmarks use
the same bookmaker and exact contract with a paired opposite-side quote, so
moving from -3.5 to -4 is not treated as the same bet. Closing no-vig return is
conditional on no push. If the exact closing contract is missing, CLV is null.

## Promotion criteria

Keep production model correctness fixes separate from experimental weights.
Promote an experimental model only after it beats the declared prediction
baseline across independent seasons, improves probability calibration, and
survives exact-price betting tests with realistic quote availability. Record
the selected configuration and an untouched future evaluation period. Feature
and strategy changes after inspecting 2025 require another independent test.

The current free-data runs establish forecasting evidence. They cannot
establish profitable betting without the historical quote archive.
