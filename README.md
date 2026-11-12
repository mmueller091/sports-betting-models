# Sports Betting Models

Dashboard: https://mmueller091.github.io/sports-betting-models/

The Python models run in GitHub Actions and publish a React dashboard to GitHub Pages. The website stays available without a home computer or a continuously running Python server. It includes NFL, college football, NHL, and a pick-history audit. Baseball is paused and hidden for the offseason.

## Pick history and offseason controls

[Pick History](https://mmueller091.github.io/sports-betting-models/#pick-history) saves the original pregame selection, line, American price, bookmaker, quote/snapshot timestamps, model probability, version, and reasoning. Later updates append revisions without overwriting the original. Each selection has a stable ID. Repeat hourly exports do not create new bets. Later opposing selections remain visible but are excluded from return summaries; summaries count the first selection per game and market. Different markets on the same game are correlated and are not independent evidence.

The ledger lives in `data_store/pick_audit.json` on the persistent `model-state` branch; GitHub commits provide file version history. Tracking starts with the last retained snapshot and continues on each export. Earlier picks are not reconstructed from later model runs. The audit records saved model snapshots, not proof of a placed bet or a successful website deployment. Watchlists and selections first generated after kickoff are excluded. CSV exports contain the filtered original selections and settlements; JSON also contains all revisions and original reasoning.

NFL/CFB results use ESPN final game summaries. NHL uses official NHL final box scores, including player participation for goal predictions. Result checks use free feeds and no Odds API credits. Missing, postponed, or failed result checks remain pending; explicit cancellations are void. Grading assumes full-game markets including overtime and the NHL final scoreboard's shootout deciding goal. Verify individual bookmaker rules. No closing-line value is claimed. Profit/ROI use a hypothetical $100 stake at each original quoted price; unpriced goal predictions and voids are excluded from returns.

`ENABLE_MLB=false` is the default in both local code and the hosted workflow. It blocks MLB routes, snapshot construction and odds/prop requests, skips background baseball polling/grading/training, and omits MLB from publishing. Existing baseball history remains stored. Re-enabling later requires setting `ENABLE_MLB=true` and restoring the desired frontend tabs; the current UI stays focused on active sports.

## Weekly football dashboards

- [NFL weekly board](https://mmueller091.github.io/sports-betting-models/#nfl-week)
- [College football weekly board](https://mmueller091.github.io/sports-betting-models/#cfb-week)

Each board has **Best bets**, **Week slate**, and **Model inputs** views. It ranks qualifying spreads, totals, and moneylines at a specific bookmaker's quoted price, limits the shortlist to one bet per game, and separates rejected candidates into a watch list. It can correctly return no qualifying bets. Games disappear after kickoff, even when viewing an older published snapshot.

The core ratings adjust scoring margins and offensive/defensive scoring for opponent strength, shrink small samples toward league average, and distinguish home and neutral venues. The NFL model also fits strictly prior-game EPA per play, passing efficiency, success rate, explosive-play rate, and sack rates using [nflverse play data](https://nflfastr.com/articles/nflfastR.html). Sacks are a limited pressure proxy, not a measured pressure rate. No advanced metric is invented from ordinary yardage.

The college model uses opponent-adjusted scoring ratings. Current passing/rushing efficiency, yards per play allowed, turnovers, pace, and rest are shown as matchup context. Those current-season summaries are not fed into historical validation because historical as-of snapshots are unavailable. Reliable current college injury data, play-level EPA, and weather are explicitly marked as gaps. NFL reported quarterback uncertainty can prevent a best-bet designation; neither league assumes that a missing injury report means a healthy team.

The Model inputs view shows the measured variables, coverage, sources, chronological validation error, and selection rules. Every validation week is predicted from earlier weeks. Estimates are provisional: scoring error and a theoretical positive expected value do not establish profitable historical betting or calibrated probabilities. No actual wagers are placed.

## Historical training and testing

The offline research commands download old games, rebuild features from prior
games, refit models chronologically, and evaluate every eligible forecast.
They run independently of the live pick ledger. A date parameter on a live API
route is not a historical backtest.

The initial research corpus contains **12,963 completed games** across five
seasons: 1,424 NFL, 4,546 college football, and 6,993 NHL. NFL play metrics are
available for all 1,424 games. Source snapshots, hashes, availability conventions,
and incomplete-game records are retained. Prediction accuracy can be tested
immediately; betting returns require exact timestamped historical prices.

- [Commands and research workflow](docs/historical-replay.md)
- [Source coverage and historical odds budgets](docs/historical-data.md)
- [Frozen experiment protocol](docs/research-protocol.md)
- [Measured football results](docs/football-research-results.md)
- [Measured NHL results](docs/nhl-research-results.md)

The experimental candidates do not automatically replace production weights.
The 2025 season is a reserved chronological comparison after 2021-2024
development. Each weekly fit uses only outcomes and features available before
its first decision. A replay preserves no-bet reasons, quote rejections, and
one exact-priced selection per game. The repository contains derived aggregate
results; large source archives and licensed odds stay outside public source.

## NHL model correctness

Version `nhl-2026-11-12.correctness.1` derives moneyline, puckline, and total
probabilities from one final-score distribution. Full-game score outcomes include
overtime and the deciding shootout goal; integer lines retain push mass. The
displayed win probability is the one used in expected-value calculations at
the named bookmaker's actual payout. Unsupported price-movement probability
bonuses have been removed. The score distribution is coherent but remains a
provisional assumption until validated against historical prices and outcomes.

Goalie quality has the correct direction, confirmed starter changes preserve
player identity, missing statistics remain missing, and xG/ice-time units and
season/situation selection are explicit. Data-source failures produce warnings.
Both NHL best bets and scorer recommendations stop after puck drop. Original
audit selections remain immutable; model changes create new revisions with the
new version identifier.

## Updates and data

- Models refresh hourly, at 23 minutes past the hour. GitHub may delay scheduled jobs.
- Market odds are cached for 24 hours. With three active sports and three markets this normally uses at most about 279 credits per 31-day month. Other use of the same API key counts toward its quota too. Model refreshes do not guarantee fresh market prices; a recommendation applies only at the quoted line and payout.
- Baseball is fully paused: no baseball schedule, statistics, grading, training, game odds, or batter-hit prop requests run in the hosted job.
- Model history, grading, odds cache, trained artifacts, and the last published snapshot live on the `model-state` branch. They survive fresh runners and deploys.
- Source failures are shown on the dashboard. A failed schedule fetch preserves the last good results and their original timestamp. Old results are labeled clearly. Football uses the league's own week calendar and checks team identities and kickoff time before matching odds.
- The website's update button downloads the latest published results. It does not trigger paid API requests. Owners can run the publishing workflow manually from Actions.

## GitHub setup

1. Add `ODDS_API_KEY` under Settings → Secrets and variables → Actions → Repository secrets. Never commit `.env` or put the key in a `REACT_APP_*` variable.
2. Select **GitHub Actions** under Settings → Pages → Source.
3. Ensure the `model-state` branch exists with a README (initial model history may be imported into `data_store/`).
4. Run **Update models and publish dashboard**. The deployment job returns the public URL.

The source and generated sports-model records are public. Private credentials and unrelated fantasy-football files are excluded. No actual wagers are placed or tracked; performance uses simulated stakes.

## Local development

See `SETUP.txt`. Run `backend/run.py` for the local API and `npm start` from `frontend/`. Football API routes are `/api/nfl/weekly` and `/api/cfb/weekly`, with optional `date_str=YYYY-MM-DD`; POST `/api/nfl/refresh` or `/api/cfb/refresh` rebuilds the local model response while respecting upstream caches. To test publishing, set `ODDS_CACHE_HOURS=24` and `ENABLE_HIT_PROP_ODDS=false`, then run `python backend/export_site.py --output frontend/build/data/dashboard.json` after a static frontend build with `REACT_APP_STATIC_DATA=true` and the appropriate `PUBLIC_URL`.

Tests: from `backend/`, run `python -m unittest discover -s tests`.

GitHub can disable scheduled workflows after 60 days without repository activity. The state branch receives normal hourly model updates, but check the Actions page if updates stop. Hosting and data providers can have outages; this is not a guarantee of continuous availability.
