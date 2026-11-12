# Historical data and odds reconstruction

The research pipeline can train and test on completed seasons immediately. The
live dashboard and its pick ledger are separate from these offline data files.
No production model is changed by downloading data or running a replay.

## Sources actually backfilled

The November 12, 2026 run loaded the following completed games. Seasons are
identified by their starting year, including NHL: `2021` means 2021-22.

| Season | NFL | CFB games involving an FBS team | NHL club games |
| --- | ---: | ---: | ---: |
| 2021 | 285 | 887 | 1,401 |
| 2022 | 284 | 896 | 1,400 |
| 2023 | 285 | 910 | 1,400 |
| 2024 | 285 | 919 | 1,398 |
| 2025 | 285 | 934 | 1,394 |
| Total | 1,424 | 4,546 | 6,993 |

These are 12,963 completed games in total. NFL 2022 has 271 completed regular
season games and 13 playoff games. Each NHL season has 1,312 completed regular
season club games; the remaining NHL games are playoffs. All-Star and Four
Nations exhibitions are excluded using franchise identities.

Uncompleted source records remain visible with `completed: false`, and replay
must exclude them. The NHL archive contains 108 postponed listings across the
five seasons, including 99 during 2021-22. They do not count as completed games or
as additional training examples. CFB 2024 contains one uncompleted listing.

### NFL

- Source: [nflverse schedules](https://github.com/nflverse/nfldata/blob/master/data/games.csv).
- Kickoff dates and times are interpreted in `America/New_York`, then converted
  to UTC with daylight-saving rules. Missing kickoff times are rejected.
- [nflverse play-by-play](https://github.com/nflverse/nflverse-data/releases/tag/pbp)
  was downloaded for all five seasons, about 95.8 MB compressed in total.
- All 2,848 team-game observations matched a scheduled game. Per-game neutral
  EPA, pass/rush efficiency, success, explosives, sack rates and denominators
  are attached as **postgame observations**. Replay may use them only for later
  games. Actual starter identities and closing market fields are not copied into
  those feature dictionaries.
- Source closing spreads and prices are retained as separate labels. They have
  no historical quote timestamp or verified book identity, so they support a
  closing-line forecast comparison, not a claim of executable T-60 betting ROI.

### College football

- Source: [sportsdataverse/cfbfastR-data archived CSV schedules](https://github.com/sportsdataverse/cfbfastR-data/tree/main/schedules/csv).
- FBS versus FCS games remain in the ratings history. FCS-only games are excluded.
- The 2021 and 2022 CSV archives omitted bowls. The adapter supplements only
  missing FBS postseason games from ESPN's December and January scoreboards,
  checking both teams against the season's FBS identities. It recovered 38 and
  42 completed postseason games respectively.
- Published postgame Elo, postgame win probabilities and excitement columns are
  discarded. They are not historical pregame features.
- CFB play-level PPA/EPA is not backfilled in this implementation. Its initial
  historical experiments learn from the score and schedule record.

### NHL

- Source: ESPN's public monthly NHL scoreboard archive. Monthly requests stay
  below the provider's 1,000-result cap; a capped response is rejected.
- These public site feeds are not a contracted, versioned data service. Download
  failures and schema changes are reported; they do not become fabricated games.
- Scores are the final scoreboard, including overtime and the shootout deciding
  goal. Where period data exists, separate regulation scores are retained.
- Historical season selection converts ESPN's ending-year convention to a
  starting year. Preseason events and non-club exhibitions are excluded.
- An optional parser for the official NHL club-season endpoint is included, but
  that host returned HTTP 403 in the execution environment. The successful
  backfill used ESPN, not that endpoint.
- MoneyPuck's published game-level data would support xG and goalie extensions.
  Its downloads returned HTTP 403 here, so no historical MoneyPuck xG or confirmed
  goalie data was incorporated or claimed.

## The information boundary

These files are **reconstructed prior-game history**, not exact replicas of what
every source published that day. Downloaded historical statistics can include
later corrections. Source bytes, retrieval time and content hash are preserved
so the reconstruction is reproducible.

For completed games, the current policy sets `available_at` to scheduled start
plus 12 hours. The explicit label is
`reconstructed_conservative_12h_after_start`. NFL `metrics_available_at` uses the
same convention. This is a conservative availability assumption for the study,
not evidence of the original publication time. `retrieved_at` remains separate
and is never used as historical feature availability.

Every normalized game includes:

| Field | Meaning |
| --- | --- |
| `league`, `season`, `game_id` | Stable research identity and season start year |
| `source_game_id`, `provider_ids` where available | Cross-provider provenance |
| `game_time` | Timezone-qualified scheduled start in UTC |
| `home_team_id`, `away_team_id` | Provider-scoped stable team identities |
| `home_team`, `away_team` | Display labels, which are not model identifiers |
| `home_score`, `away_score`, `completed` | Outcomes, never same-game pregame inputs |
| `home_metrics`, `away_metrics` when available | Postgame observations for subsequent games |
| `source_url`, `retrieved_at`, `data_revision` | Source request and SHA256 of immutable downloaded bytes |
| `available_at`, `availability_basis` | Declared research availability rule |
| `score_semantics` | Final versus regulation and overtime/shootout convention |

Archived injury reports, confirmed lineups/goalies and weather forecasts need
their own publication-time provenance. Do not insert the player who eventually
started, the final season's rating, or the weather eventually observed and call
it a pregame prediction. Where those records are unavailable, the replay omits
them and exposes that feature coverage.

## Local data layout

```text
research-data/
  cache/raw/<sha256>
  cache/metadata/<request_hash>_<sha256>.json
  cache/index/<request_hash>.json
  normalized/nfl_2021.jsonl
  normalized/cfb_2021.jsonl
  normalized/nhl_2021.jsonl
  normalized_versions/<name>_<sha256>.jsonl
  coverage_nfl.json
  coverage_cfb.json
  coverage_nhl.json
```

Raw content is immutable. `--refresh` fetches a new revision and changes the
request index; the original bytes remain. When a normalized file changes, its
preceding edition is retained by hash. Network calls use bounded timeouts, byte
limits and at most two retries. HTTP 403 is not retried. Authentication values
are redacted from stored URLs and never appear in filenames or exceptions.

## Commands

Run these from the repository's `backend` directory. The paths below keep data
outside the source package; use any private research-data directory you prefer.

```bash
python -m research backfill --league all --seasons 2021 2022 2023 2024 2025 --output ../research-data
python -m research nfl-plays --games ../research-data/normalized --seasons 2021 2022 2023 2024 2025 --output ../research-data
```

Both commands use free public sources. `nfl-plays` writes enriched normalized NFL
files itself; it does not require a separate save step. Repeated calls reuse
the immutable download cache. Add `--refresh` only when a newer source revision
is intended.

Create a moneyline-only NHL 2025-26 price plan, offline:

```bash
python -m research odds-plan --games ../research-data/normalized --league nhl --seasons 2025 --markets moneyline --output ../research-data/nhl_2025_manifest.json
```

The next command is the explicit paid operation, to run only with a configured
`ODDS_API_KEY` and a chosen quota budget. It was not executed during this work:

```bash
python -m research odds-fetch --manifest ../research-data/nhl_2025_manifest.json --output ../research-data/odds --max-credits 16600 --allow-paid
```

After any successful downloads, convert cached responses to reproducible quote
files without spending further credits:

```bash
python -m research odds-normalize --manifest ../research-data/nhl_2025_manifest.json --games ../research-data/normalized --output ../research-data/odds
```

`odds-normalize --aliases verified_aliases.json` accepts a mapping such as
`{"Georgia Bulldogs": "Georgia"}` after the identity has been verified. Its
entry and closing JSONL files can then be passed to `replay --quotes` and
`--closing-quotes`. Read its coverage and match diagnostics before treating the
priced subset as representative of all historical games.

## Historical bookmaker prices

The Odds API is already the project's live odds provider. Its historical
featured-market endpoint supports timestamped moneyline, spread and total
snapshots. NFL and CFB coverage begins June 6, 2020; NHL begins June 29, 2020.
Snapshots are ten minutes apart before September 2022 and five minutes apart
afterward. Coverage still depends on the individual book and market.

Official sources:

- [Historical coverage and cost](https://the-odds-api.com/historical-odds-data/)
- [Historical API contract](https://the-odds-api.com/liveapi/guides/v4/)
- [Current plans](https://the-odds-api.com/)
- [Storage and model-training terms](https://the-odds-api.com/terms-and-conditions.html)

The implemented planner requests T-60 entry and T-5 comparison snapshots, groups
all games sharing one league and timestamp, and calculates the quota requirement
without a key or network call. T-5 is a declared closing benchmark, not proof of
the final tradable closing price. The provider returns the nearest available
snapshot at or before the requested time.

### Exact quota estimates from the downloaded schedule

One US-region request for all three full-game markets costs at most the documented
30 historical credits. The saved manifests generated on November 12 are:

| League | 2021-2025 requests | 2021-2025 credits | 2025-only credits |
| --- | ---: | ---: | ---: |
| NFL | 1,344 | 40,320 | 8,340 |
| CFB | 3,450 | 103,500 | 22,860 |
| NHL | 8,548 | 256,440 | 49,800 |
| Total | 13,342 | 400,260 | 81,000 |

Incomplete games and unconfirmed kickoff times are excluded. A 2025 NHL
moneyline-only plan requires 16,600 credits at the same two cutoffs. Extra regions,
extra forecast horizons and props change the budget. These are quota estimates,
not a promise that every requested book will have a quote.

The current provider plans list $30/20,000 credits, $59/100,000 and $119/5 million
per month. No subscription was purchased and no paid historical API request was
made during this implementation.

### Paid fetch and offline normalization

`odds-fetch` requires both `--allow-paid` and a positive `--max-credits`, plus
`ODDS_API_KEY` in the environment. It does not activate from a replay or a free
schedule backfill. It validates the manifest hash, provider host, path, query and
cost before sending credentials. It reserves credits persistently **before** each
request, makes no automatic retry, and skips earlier attempts when resuming. A
failed request retains its reservation because the client cannot assume it was
not charged. A per-manifest lock prevents simultaneous runs from overrunning the
same credit ledger. After a crash, confirm that no fetch is active before removing
a stale lock directory; the reserved-credit ledger must be preserved.

`odds-normalize` works entirely offline. It checks saved response hashes, then
emits `entry_quotes.jsonl`, `closing_quotes.jsonl`, match diagnostics and coverage.
Each quote retains game, sport, exact market, side, line, American price, book,
book update time, provider snapshot time, request cutoff and source hash.

Both teams must match verified names/aliases and kickoff times must agree within
five minutes, unless a verified Odds API event ID is already supplied. Ambiguous
or unmatched games are reported rather than fuzzy-matched. CFB schedules often
use school-only names; supply a verified team-name alias JSON or explicit event
crosswalk before expecting complete bookmaker matching.

Exact-contract selection first finds the newest eligible snapshot for the same
game, book and market. Only then does it match the requested line. It cannot
revive an earlier +3.5 after the book moved to +2.5, substitute another book's
price, invent -110, or use a quote/snapshot from after the decision cutoff.
Replay should operate on one declared decision policy at a time and report all
missing quotes. A closing quote is not a pregame feature for an earlier entry.

## Access and licensing notes

- nflverse's main data releases are broadly CC-BY 4.0; FTN subsets have distinct
  terms and availability. The implementation uses the public schedule/PBP files.
  See [nflverse licensing](https://github.com/nflverse).
- [MoneyPuck's data page](https://www.moneypuck.com/data.htm) allows noncommercial
  use with attribution and asks users to inquire for other purposes. Use its
  published downloads, not unapproved scraping.
- [CFBD availability](https://api.collegefootballdata.com/data-availability)
  and [betting schema](https://api.collegefootballdata.com/api/betting) describe
  additional CFB history. Opening/closing lines without timestamps do not prove
  a specific earlier execution price.
- [Open-Meteo Single Runs](https://open-meteo.com/en/docs/single-runs-api) can
  preserve a forecast issue time for later weather experiments. Its ordinary
  historical weather series is not a substitute for a forecast known beforehand.
- The Odds API permits stored data and model training, but not redistribution as
  a standalone raw-data product. Keep raw bookmaker archives out of the public
  repository and publish derived research results instead.

## Verification scope

Focused source tests cover UTC conversion, postgame-field exclusion, exhibition
filtering, immutable downloads, authentication redaction, exact game matching,
future timestamps, moved lines, missing prices, manifest deduplication, explicit
paid opt-in, budget reservation and offline quote normalization. The tests use
local fixtures and mock transports, so they do not spend API credits.
