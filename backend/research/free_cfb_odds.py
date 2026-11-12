"""Free, *untimestamped* CFB archived-price reference study.

No archived observation times are present in the SportsDataverse line CSV. Its
``date_time`` column is kickoff, NOT an odds quote timestamp. Consequently this
module intentionally does not call ``run_replay`` with fabricated quote times,
and its hypothetical closing-price profit is NEVER called executable T-60 ROI.

It reuses frozen, chronological forecast rows, exact ESPN IDs, the existing
probability/EV equations, and the previously specified one-bet selection gates.
No production model or pick ledger is imported or changed.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import mean, median

from models.football_bets import CONFIG, expected_value, implied_probability
from research.evaluation import cluster_interval
from research.models import CANDIDATES, PriorOnlyCalibrator
from research.replay import _quote_probabilities, timestamp


ARCHIVE_URL = 'https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/betting/csv/cfb_line_odds.csv.gz'
PRICING_BASIS = 'retrospective_archive_nonopening_prices_unverified_close_not_executable'
CANDIDATE_NAMES = ('current', 'cfb_fixed_home', 'cfb_fixed_home_calibrated')
PERIODS = {'development_2021_2024': tuple(range(2021, 2025)), 'evaluated_2025': (2025,)}
REQUIRED = {'game_id', 'season', 'game_desc', 'market_type', 'abbr', 'lines',
            'odds', 'book', 'home_team_id', 'away_team_id', 'date_time'}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _id(value):
    text = str(value)
    if not re.fullmatch(r'\d+(?:\.0+)?', text):
        return None
    return text.split('.')[0]


def _book(value):
    return re.sub(r'[^a-z0-9]', '', value.casefold())


def _valid_american(value):
    n = _number(value)
    return n is not None and abs(n) >= 100 and n == round(n) and abs(n) < 1_000_000


def _valid_spread(value):
    n = _number(value)
    return n is not None and abs(n) <= 100 and math.isclose(n * 2, round(n * 2))


def load_normalized_games(paths):
    """Strictly join the archived prices to prior normalized ESPN game IDs."""
    result = {}
    for path in paths:
        with open(path, encoding='utf-8') as source:
            for line in source:
                if not line.strip():
                    continue
                game = json.loads(line)
                if game['league'] != 'cfb' or not game['completed']:
                    continue
                if not str(game['game_id']).startswith('cfb:'):
                    raise ValueError('CFB games must carry canonical ESPN ID prefix.')
                key = game['game_id']
                if key in result:
                    raise ValueError(f'Duplicate normalized game: {key}')
                result[key] = game
    return result


@dataclass
class ArchivedPrices:
    quotes: dict
    spread: dict
    coverage: dict
    rejections: dict
    books: dict
    source_sha256: str


def load_archive(path, games):
    """Parse paired book contracts with hard identity and data-quality checks.

    Favors no book or outcome: all paired books with a genuine price are tested.
    A contradictory favorite versus a >=3.5-point archived spread is rejected
    before outcomes or model profits are examined. Unpriced spreads are retained
    for benchmark MAE only; no standard -110 price is imputed.
    """
    by_espn = {key.split(':', 1)[1]: (key, game) for key, game in games.items()}
    contracts = defaultdict(set)
    seen = Counter()
    rejected = Counter()
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as source:
        reader = csv.DictReader(source)
        if not REQUIRED.issubset(reader.fieldnames or []):
            raise ValueError('Archive columns changed; verify the new schema before proceeding.')
        for row in reader:
            parsed = by_espn.get(_id(row['game_id']))
            if parsed is None:
                continue
            game_id, game = parsed
            seen[game_id] += 1
            if (_id(row['home_team_id']) != _id(str(game['home_team_id']).split(':')[-1])
                    or _id(row['away_team_id']) != _id(str(game['away_team_id']).split(':')[-1])):
                rejected['identity_mismatch'] += 1
                continue
            descriptor = row['game_desc'].split('@')
            if len(descriptor) != 2:
                rejected['invalid_game_descriptor'] += 1
                continue
            away, home = descriptor
            market = row['market_type']
            if market not in ('money_line', 'spread'):
                continue
            side = 'home' if row['abbr'] == home else 'away' if row['abbr'] == away else None
            if side is None:
                rejected['unresolved_market_side'] += 1
                continue
            book = _book(row['book'])
            if not book:
                rejected['missing_book'] += 1
                continue
            if market == 'money_line':
                odds = _number(row['odds'])
                if not _valid_american(odds):
                    rejected['invalid_moneyline_price'] += 1
                    continue
                value = float(odds)
            else:
                value = _number(row['lines'])
                if not _valid_spread(value):
                    rejected['invalid_spread_line'] += 1
                    continue
            contracts[(game_id, book, market, side)].add(value)

    unique = {}
    for key, values in contracts.items():
        if len(values) == 1:
            unique[key] = next(iter(values))
        else:
            rejected['conflicting_duplicate_contract'] += 1
    by_game_book = defaultdict(dict)
    for (game_id, book, market, side), value in unique.items():
        by_game_book[(game_id, book)][(market, side)] = value

    paired_spreads = defaultdict(list)
    possible_ml = defaultdict(list)
    for (game_id, book), entries in sorted(by_game_book.items()):
        hs, as_ = entries.get(('spread', 'home')), entries.get(('spread', 'away'))
        if hs is not None and as_ is not None:
            if math.isclose(hs + as_, 0, abs_tol=1e-8):
                paired_spreads[game_id].append((book, float(-hs)))
            else:
                rejected['nonopposite_spreads'] += 1
        hm, am = entries.get(('money_line', 'home')), entries.get(('money_line', 'away'))
        if hm is None or am is None:
            continue
        overround = implied_probability(hm) + implied_probability(am)
        if not 0.98 <= overround <= 1.25:
            rejected['implausible_paired_moneyline_vig'] += 1
            continue
        possible_ml[game_id].append((book, hm, am))

    spread = {game_id: median(line for _, line in lines)
              for game_id, lines in paired_spreads.items()}
    quotes = defaultdict(list)
    by_book = Counter()
    for game_id, entries in possible_ml.items():
        closing_margin = spread.get(game_id)
        for book, hm, am in entries:
            # The archive has a small but real set of reversed-price assignments.
            # A closing spread of >=3.5 points cannot favor the opposite ML side.
            if (closing_margin is not None and abs(closing_margin) >= 3.5
                    and (closing_margin > 0) != (hm < am)):
                rejected['ml_favorite_contradicts_spread'] += 1
                continue
            for side, odds in (('home', hm), ('away', am)):
                quotes[game_id].append({'game_id': game_id, 'book_key': book,
                                        'market': 'moneyline', 'side': side,
                                        'line': None, 'odds': odds})
            by_book[book] += 1
    return ArchivedPrices(dict(quotes), spread, {
        'matched_games_with_any_archive_rows': len(seen),
        'paired_spread_games': len(spread),
        'paired_moneyline_games_before_quality_filter': len(possible_ml),
        'paired_moneyline_games_after_quality_filter': len(quotes),
        'paired_moneyline_book_games_after_quality_filter': sum(by_book.values()),
    }, dict(sorted(rejected.items())), dict(sorted(by_book.items())), _sha256(path))


def load_forecasts(path, candidate):
    rows = []
    seen = set()
    with open(path, encoding='utf-8') as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            if row['candidate'] != candidate or row['league'] != 'cfb':
                raise ValueError('Wrong candidate or league in frozen forecast input.')
            if row['game_id'] in seen:
                raise ValueError('Duplicate frozen forecast game.')
            seen.add(row['game_id'])
            rows.append(row)
    return rows


def _period_summary(rows, bets, archive, bootstrap_samples):
    eligible = [r for r in rows if r['forecast_eligible']]
    ml = [r for r in eligible if r['game_id'] in archive.quotes]
    with_spread = [r for r in eligible if r['game_id'] in archive.spread]
    realized = [r for r in ml if r['actual_margin'] != 0]
    market_brier = []
    model_brier = []
    for r in realized:
        pairs = defaultdict(dict)
        for q in archive.quotes[r['game_id']]:
            pairs[q['book_key']][q['side']] = q['odds']
        fair = [implied_probability(p['home']) / (implied_probability(p['home']) + implied_probability(p['away']))
                for p in pairs.values() if 'home' in p and 'away' in p]
        if not fair:
            continue
        label = float(r['actual_margin'] > 0)
        market_brier.append((median(fair) - label) ** 2)
        model_p = r['home_win_probability'] / (r['home_win_probability'] + r['away_win_probability'])
        model_brier.append((model_p - label) ** 2)
    profit = sum(b['profit_units'] for b in bets)
    roi_rows = [dict(b, league='cfb', fold=b['fold']) for b in bets]
    largest_first = sorted(bets, key=lambda b: b['profit_units'], reverse=True)
    after_largest_win = largest_first[1:]
    after_two_largest_wins = largest_first[2:]
    return {
        'source_games': len(rows), 'forecast_eligible_games': len(eligible),
        'moneyline_paired_eligible_games': len(ml),
        'moneyline_missing_eligible_games': len(eligible) - len(ml),
        'spread_paired_eligible_games': len(with_spread),
        'spread_real_payout_odds_available_games': 0,
        'market_spread_margin_mae': mean(abs(r['actual_margin'] - archive.spread[r['game_id']]) for r in with_spread) if with_spread else None,
        'model_margin_mae_on_market_spread_sample': mean(abs(r['actual_margin'] - r['predicted_margin']) for r in with_spread) if with_spread else None,
        'market_moneyline_brier_on_paired_sample': mean(market_brier) if market_brier else None,
        'model_moneyline_brier_on_paired_sample': mean(model_brier) if model_brier else None,
        'moneyline_binary_games': len(model_brier),
        'hypothetical_selected_moneyline_bets': len(bets),
        'hypothetical_wins': sum(b['settlement'] == 'win' for b in bets),
        'hypothetical_losses': sum(b['settlement'] == 'loss' for b in bets),
        'hypothetical_pushes': sum(b['settlement'] == 'push' for b in bets),
        'hypothetical_staked_units': len(bets),
        'hypothetical_profit_units_at_archived_price': profit,
        'hypothetical_roi_at_archived_price': profit / len(bets) if bets else None,
        'hypothetical_positive_american_odds_bets': sum(b['american_odds'] > 0 for b in bets),
        'hypothetical_roi_excluding_largest_winner': (sum(b['profit_units'] for b in after_largest_win) / len(after_largest_win)
                                                      if after_largest_win else None),
        'hypothetical_roi_excluding_two_largest_winners': (sum(b['profit_units'] for b in after_two_largest_wins) / len(after_two_largest_wins)
                                                           if after_two_largest_wins else None),
        'hypothetical_roi_week_cluster_95pct_interval': cluster_interval(roi_rows, lambda b: b['profit_units'], bootstrap_samples),
        'roi_is_executable': False,
    }


def _candidate_decisions(rows, archive, game_index, candidate, stress_points):
    """Use the frozen prior folds and the original strict bet acceptance gates."""
    rules = CONFIG['cfb']
    by_fold = defaultdict(list)
    for row in rows:
        if row['game_id'] not in game_index:
            raise ValueError(f'No normalized score for frozen forecast: {row["game_id"]}')
        game = game_index[row['game_id']]
        if row['actual_margin'] != game['home_score'] - game['away_score']:
            raise ValueError(f'Frozen outcome differs from source game: {row["game_id"]}')
        by_fold[row['fold']].append(row)
    prior_eligible = []
    bets = []
    reasons = Counter()
    for fold in sorted(by_fold):
        group = by_fold[fold]
        cutoff = timestamp(group[0]['training_cutoff'])
        season = group[0]['season']
        if any(r['season'] != season or timestamp(r['training_cutoff']) != cutoff for r in group):
            raise ValueError('Inconsistent chronological frozen forecast fold.')
        prior = [r for r in prior_eligible
                 if timestamp(r['source_provenance']['available_at']) < cutoff]
        prior_season = [r for r in prior if r['season'] == season]
        margin_errors = [r['actual_margin'] - r['predicted_margin'] for r in prior_season]
        baseline_errors = [r['actual_margin'] - r['baseline_margin'] for r in prior_season]
        is_calibrated = CANDIDATES[candidate].calibrate_moneyline
        calibrator = PriorOnlyCalibrator(prior) if is_calibrated else None
        for row in group:
            if row['prior_validation_games'] != len(prior_season):
                raise ValueError('Prior-fold error count differs from frozen run.')
            if (row['calibration_training_games'] != (calibrator.n if calibrator else 0)
                    or row['calibration_fitted'] != bool(calibrator and calibrator.fitted)):
                raise ValueError('Calibrator provenance differs from frozen run.')
            raw = row['raw_home_conditional']
            predicted = calibrator.predict(raw) if calibrator and calibrator.fitted else raw
            frozen = row['home_win_probability'] / (row['home_win_probability'] + row['away_win_probability'])
            if not math.isclose(predicted, frozen, abs_tol=2e-9):
                raise ValueError('Reconstructed prior-only calibrator changes frozen win probability.')
            base = []
            if row['training_games'] < rules['minimum_history']:
                base.append('insufficient_training_history')
            if min(row['home_prior_games'], row['away_prior_games']) < rules['minimum_team']:
                base.append('insufficient_team_history')
            if len(prior_season) < rules['minimum_holdout']:
                base.append('insufficient_prior_forecast_validation')
            if prior_season and mean(abs(e) for e in margin_errors) >= mean(abs(e) for e in baseline_errors):
                base.append('forecast_not_better_than_simple_baseline')
            game_quotes = archive.quotes.get(row['game_id'], [])
            if not game_quotes:
                base.append('no_valid_paired_untimestamped_moneyline')
            candidates = []
            for quote in game_quotes:
                w, p, l = _quote_probabilities({'margin': row['predicted_margin'], 'total': row['predicted_total']},
                                                quote, 'cfb', row['margin_sigma'], row['total_sigma'], calibrator)
                cw, _, cl = _quote_probabilities({'margin': row['predicted_margin'], 'total': row['predicted_total']},
                                                  quote, 'cfb', row['margin_sigma'], row['total_sigma'], calibrator,
                                                  stress=stress_points)
                ev = expected_value(w, l, quote['odds'])
                conservative = expected_value(cw, cl, quote['odds'])
                conditional = w / max(1 - p, 1e-12)
                declined = list(base)
                if conditional - implied_probability(quote['odds']) < rules['minimum_edge'] or ev < rules['minimum_ev']:
                    declined.append('below_predeclared_edge_threshold')
                if conservative <= 0:
                    declined.append('fails_adverse_projection_stress')
                candidates.append({**quote, 'expected_value': ev, 'conservative_ev': conservative,
                                   'predicted_win_probability': w, 'push_probability': p,
                                   'loss_probability': l, 'declined': declined})
            accepted = [c for c in candidates if not c['declined']]
            if not accepted:
                reasons.update(set(base + [r for c in candidates for r in c['declined']]))
                continue
            best = max(accepted, key=lambda c: (c['conservative_ev'], c['expected_value'], str(c['book_key'])))
            actual = row['actual_margin'] * (1 if best['side'] == 'home' else -1)
            settlement = 'push' if actual == 0 else 'win' if actual > 0 else 'loss'
            odds = best['odds']
            profit = (odds / 100 if odds > 0 else 100 / abs(odds)) if settlement == 'win' else 0.0 if settlement == 'push' else -1.0
            bets.append({'candidate': candidate, 'game_id': row['game_id'], 'season': season,
                         'home_team': row['home_team'], 'away_team': row['away_team'],
                         'final_home_score': game_index[row['game_id']]['home_score'],
                         'final_away_score': game_index[row['game_id']]['away_score'],
                         'game_time': row['game_time'], 'model_decision_time': row['decision_time'],
                         'quote_observed_at': None, 'fold': fold, 'side': best['side'],
                         'book_key': best['book_key'], 'american_odds': odds,
                         'model_win_probability': best['predicted_win_probability'],
                         'estimated_ev_units': best['expected_value'] / 100,
                         'estimated_stressed_ev_units': best['conservative_ev'] / 100,
                         'settlement': settlement, 'profit_units': profit,
                         'pricing_basis': PRICING_BASIS,
                         'realized_at_a_verified_historical_decision_time': False})
        prior_eligible.extend(r for r in group if r['forecast_eligible'])
    return bets, dict(sorted(reasons.items()))


def run_study(archive_path, normalized_paths, forecast_paths, *, bootstrap_samples=500):
    normalized_paths = tuple(normalized_paths)
    games = load_normalized_games(normalized_paths)
    archive = load_archive(archive_path, games)
    output = {'study': 'free_cfb_archived_price_reference_1',
              'pricing_basis': PRICING_BASIS,
              'asof_or_t60_returns_calculated': False, 'uses_prices_as_model_features': False,
              'no_spread_odds_imputed': True,
              'source': {'url': ARCHIVE_URL, 'sha256': archive.source_sha256,
                         'observation_timestamp_in_source': False,
                         'nonopening_prices_verified_as_actual_closes': False,
                         'date_time_column_represents': 'game_kickoff',
                         'normalized_espn_games': len(games),
                         'normalized_input_sha256': [
                             {'filename': Path(path).name, 'sha256': _sha256(path)}
                             for path in normalized_paths
                         ],
                         'coverage': archive.coverage, 'rejections': archive.rejections,
                         'paired_moneyline_book_counts': archive.books},
              'config': {'candidates': list(CANDIDATE_NAMES),
                         'one_moneyline_bet_per_game': True,
                         'moneyline_minimum_edge': CONFIG['cfb']['minimum_edge'],
                         'moneyline_minimum_ev_dollars_per_100': CONFIG['cfb']['minimum_ev'],
                         'adverse_margin_stress_points': 3.0,
                         'paired_moneyline_overround_accepted': [0.98, 1.25],
                         'quarantine_ml_contradicting_consensus_spread_points': 3.5,
                         'bootstrap_samples': bootstrap_samples},
              'candidate_reports': {}, 'selected_bets': [], 'market_references': []}
    for game_id, game in sorted(games.items()):
        quotes = archive.quotes.get(game_id, [])
        pairs = defaultdict(dict)
        for q in quotes:
            pairs[q['book_key']][q['side']] = q['odds']
        output['market_references'].append({
            'game_id': game_id, 'season': game['season'],
            'archived_home_margin': archive.spread.get(game_id),
            'moneyline_book_pairs': [{'book_key': book,
                                     'home_american_odds': pair['home'], 'away_american_odds': pair['away']}
                                    for book, pair in sorted(pairs.items()) if 'home' in pair and 'away' in pair],
            'pricing_basis': PRICING_BASIS,
        })
    for candidate in CANDIDATE_NAMES:
        files = forecast_paths[candidate]
        forecasts = []
        for path in files:
            forecasts.extend(load_forecasts(path, candidate))
        if len(forecasts) != len({r['game_id'] for r in forecasts}):
            raise ValueError('Duplicate game across frozen forecast files.')
        configs = {r['config_hash'] for r in forecasts}
        # The development and 2025 final folds deliberately have different
        # start/end selection configs. All rows are frozen, never recalculated.
        if len(configs) > 2:
            raise ValueError('Unexpected number of frozen replay configurations.')
        bets, reasons = _candidate_decisions(forecasts, archive, games, candidate, 3.0)
        output['selected_bets'].extend(bets)
        summary = {'frozen_forecast_inputs': [{'path': str(path), 'sha256': _sha256(path)} for path in files],
                   'frozen_model_versions': sorted({r['model_version'] for r in forecasts}),
                   'frozen_config_hashes': sorted(configs),
                   'rejected_bet_reason_counts': reasons,
                   'periods': {}}
        for period, seasons in PERIODS.items():
            group = [r for r in forecasts if r['season'] in seasons]
            group_bets = [b for b in bets if b['season'] in seasons]
            summary['periods'][period] = _period_summary(group, group_bets, archive, bootstrap_samples)
        summary['seasons'] = {
            str(season): _period_summary([r for r in forecasts if r['season'] == season],
                                         [b for b in bets if b['season'] == season],
                                         archive, bootstrap_samples)
            for season in sorted({r['season'] for r in forecasts})
        }
        output['candidate_reports'][candidate] = summary
    return output


def write_study(report, directory):
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    parts = {'report.json': {k: v for k, v in report.items() if k not in ('selected_bets', 'market_references')},
             'selected_bets.jsonl': report['selected_bets'],
             'market_reference.jsonl': report['market_references']}
    for name, value in parts.items():
        path = target / name
        if name.endswith('.jsonl'):
            with path.open('w', encoding='utf-8') as dest:
                for row in value:
                    dest.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')
        else:
            path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')
    hashes = {name: _sha256(target / name) for name in parts}
    (target/'SHA256SUMS.json').write_text(json.dumps(hashes, indent=2, sort_keys=True) + '\n')
    return hashes
