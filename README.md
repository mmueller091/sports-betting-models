# Sports Betting Models

Dashboard: https://mmueller091.github.io/sports-betting-models/

The Python models run in GitHub Actions and publish a React dashboard to GitHub Pages. The website stays available without a home computer or a continuously running Python server. It includes weekly NFL and college football boards alongside the existing MLB and NHL analysis.

## Weekly football dashboards

- [NFL weekly board](https://mmueller091.github.io/sports-betting-models/#nfl-week)
- [College football weekly board](https://mmueller091.github.io/sports-betting-models/#cfb-week)

Each board has **Best bets**, **Week slate**, and **Model inputs** views. It ranks qualifying spreads, totals, and moneylines at a specific bookmaker's quoted price, limits the shortlist to one bet per game, and separates rejected candidates into a watch list. It can correctly return no qualifying bets. Games disappear after kickoff, even when viewing an older published snapshot.

The core ratings adjust scoring margins and offensive/defensive scoring for opponent strength, shrink small samples toward league average, and distinguish home and neutral venues. The NFL model also fits strictly prior-game EPA per play, passing efficiency, success rate, explosive-play rate, and sack rates using [nflverse play data](https://nflfastr.com/articles/nflfastR.html). Sacks are a limited pressure proxy, not a measured pressure rate. No advanced metric is invented from ordinary yardage.

The college model uses opponent-adjusted scoring ratings. Current passing/rushing efficiency, yards per play allowed, turnovers, pace, and rest are shown as matchup context. Those current-season summaries are not fed into historical validation because historical as-of snapshots are unavailable. Reliable current college injury data, play-level EPA, and weather are explicitly marked as gaps. NFL reported quarterback uncertainty can prevent a best-bet designation; neither league assumes that a missing injury report means a healthy team.

The Model inputs view shows the measured variables, coverage, sources, chronological validation error, and selection rules. Every validation week is predicted from earlier weeks. Estimates are provisional: scoring error and a theoretical positive expected value do not establish profitable historical betting or calibrated probabilities. No actual wagers are placed.

## Updates and data

- Models refresh hourly, at 23 minutes past the hour. GitHub may delay scheduled jobs.
- Market odds are cached for 24 hours. With four sports and three markets this normally uses at most about 372 credits per 31-day month. Other use of the same API key counts toward its quota too. Model refreshes do not guarantee fresh market prices; a recommendation applies only at the quoted line and payout.
- Paid batter-hit prop lookups are disabled in hosted jobs. Beat the Streak still uses the statistical model. Local runs retain the existing prop behavior.
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
