# MLB + NHL Betting Models

Dashboard: https://mmueller091.github.io/sports-betting-models/

The existing Python models run in GitHub Actions and publish a React dashboard to GitHub Pages. The website stays available without a home computer or a continuously running Python server.

## Updates and data

- Models refresh hourly, at 23 minutes past the hour. GitHub may delay scheduled jobs.
- Market odds are cached for 12 hours. With two sports and three markets this normally uses at most about 372 credits per 31-day month. Other use of the same API key counts toward its quota too.
- Paid batter-hit prop lookups are disabled in hosted jobs. Beat the Streak still uses the statistical model. Local runs retain the existing prop behavior.
- Model history, grading, odds cache, trained artifacts, and the last published snapshot live on the `model-state` branch. They survive fresh runners and deploys.
- Source failures are shown on the dashboard. A failed schedule fetch preserves the last good results and their original timestamp. Old results are labeled clearly.
- The website's update button downloads the latest published results. It does not trigger paid API requests. Owners can run the publishing workflow manually from Actions.

## GitHub setup

1. Add `ODDS_API_KEY` under Settings → Secrets and variables → Actions → Repository secrets. Never commit `.env` or put the key in a `REACT_APP_*` variable.
2. Select **GitHub Actions** under Settings → Pages → Source.
3. Ensure the `model-state` branch exists with a README (initial model history may be imported into `data_store/`).
4. Run **Update models and publish dashboard**. The deployment job returns the public URL.

The source and generated sports-model records are public. Private credentials and unrelated fantasy-football files are excluded. No actual wagers are placed or tracked; performance uses simulated stakes.

## Local development

See `SETUP.txt`. Run `backend/run.py` for the local API and `npm start` from `frontend/`. To test publishing, set `ODDS_CACHE_HOURS=12` and `ENABLE_HIT_PROP_ODDS=false`, then run `python backend/export_site.py --output frontend/build/data/dashboard.json` after a static frontend build with `REACT_APP_STATIC_DATA=true` and the appropriate `PUBLIC_URL`.

Tests: from `backend/`, run `python -m unittest discover -s tests`.

GitHub can disable scheduled workflows after 60 days without repository activity. The state branch receives normal hourly model updates, but check the Actions page if updates stop. Hosting and data providers can have outages; this is not a guarantee of continuous availability.
