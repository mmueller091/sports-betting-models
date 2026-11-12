"""Offline, learned NHL scoring baselines with chronological weekly refits.

These candidates use final-score history only. They do not reconstruct old
goalie announcements, injuries, xG feeds or the site's hand-weighted formula.
Configuration is predeclared; this module never modifies production settings.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
import math

import numpy as np
from scipy.stats import skellam
from sklearn.linear_model import PoissonRegressor

from models.nhl_bets import nhl_score_distribution, market_probabilities
from research.replay import (
    _prepare, timestamp, select_asof_quotes, settle_bet, comparable_clv,
)

NHL_REPLAY_VERSION = 'historical-nhl-replay-1.0'
NHL_CANDIDATES = ('nhl_baseline', 'nhl_poisson')


def _prepare_nhl(games):
    prepared = _prepare(games, 'nhl')
    if any(game['home_score'] == game['away_score'] for game in prepared):
        raise ValueError('Completed modern NHL final score cannot be tied; regulation-only results are unsupported.')
    return prepared


def _training_source_hash(history):
    fields = ('game_id', 'home_team_id', 'away_team_id', 'home_score', 'away_score',
              'game_time', 'neutral_site', 'available_at')
    identities = [{**{key: game.get(key) for key in fields},
                   'feature_available_at': game['_feature_available'].isoformat()} for game in history]
    return hashlib.sha256(json.dumps(identities, separators=(',', ':'), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class NHLReplayConfig:
    decision_minutes: int = 60
    max_quote_age_minutes: int = 120
    training_days: int = 730
    decay_days: float = 180.0
    poisson_alpha: float = 0.05
    minimum_training_games: int = 200
    minimum_forecast_team_games: int = 10
    minimum_ev: float = 0.02
    start_season: int | None = None
    end_season: int | None = None
    allowed_books: tuple[str, ...] = ()

    def __post_init__(self):
        if self.decision_minutes < 0 or self.max_quote_age_minutes <= 0:
            raise ValueError('Invalid NHL decision time or quote age.')
        if self.training_days <= 0 or self.decay_days <= 0 or self.poisson_alpha < 0:
            raise ValueError('Invalid NHL training configuration.')
        if self.minimum_training_games < 1 or self.minimum_forecast_team_games < 1:
            raise ValueError('NHL minimum sample counts must be positive.')


class NHLScoringModel:
    """Regularized log-link attack/defense model, or a league-average baseline.

    Two rows per prior game fit goals scored from scoring-team identity,
    defending-team identity and home status. Recency weights and regularization
    are fixed before evaluation. A neutral game receives no home indicator.
    """
    def __init__(self, history, cutoff, candidate, config):
        if candidate not in NHL_CANDIDATES:
            raise ValueError(f'Unknown NHL candidate: {candidate}')
        self.candidate, self.config = candidate, config
        self.cutoff = timestamp(cutoff)
        self.teams = sorted({str(g[k]) for g in history for k in ('home_team_id', 'away_team_id')})
        self.index = {team: i for i, team in enumerate(self.teams)}
        self.counts = Counter(str(g[k]) for g in history for k in ('home_team_id', 'away_team_id'))
        self.model = None
        self.home_mean, self.away_mean = 3.05, 3.05
        self.training_games = len(history)
        self.latest_feature_time = max((g['_feature_available'] for g in history), default=None)
        self.source_ids_hash = _training_source_hash(history)
        if not history:
            return
        weights = np.asarray([math.exp(-(self.cutoff-g['_time']).total_seconds()/86400/config.decay_days)
                              for g in history])
        self.home_mean = float(np.average([g['home_score'] for g in history], weights=weights))
        self.away_mean = float(np.average([g['away_score'] for g in history], weights=weights))
        if candidate == 'nhl_poisson' and len(history) >= config.minimum_training_games:
            x, y, w = [], [], []
            for game, weight in zip(history, weights):
                for side, other in (('home', 'away'), ('away', 'home')):
                    x.append(self._features(game[side+'_team_id'], game[other+'_team_id'],
                                            side == 'home' and not game.get('neutral_site', False)))
                    y.append(game[side+'_score'])
                    w.append(weight)
            self.model = PoissonRegressor(alpha=config.poisson_alpha, max_iter=500, tol=1e-7)
            self.model.fit(np.asarray(x), np.asarray(y), sample_weight=np.asarray(w))

    def _features(self, scoring, defending, home):
        n = len(self.teams)
        row = np.zeros(2*n+1)
        if str(scoring) in self.index:
            row[self.index[str(scoring)]] = 1
        if str(defending) in self.index:
            row[n+self.index[str(defending)]] = 1
        row[-1] = float(home)
        return row

    def predict(self, game):
        neutral = game.get('neutral_site', False)
        home, away = self.home_mean, self.away_mean
        if neutral:
            home = away = (home+away)/2
        if self.model is not None:
            x = [self._features(game['home_team_id'], game['away_team_id'], not neutral),
                 self._features(game['away_team_id'], game['home_team_id'], False)]
            home, away = (float(v) for v in self.model.predict(np.asarray(x)))
        # These means were fitted to final scoreboard goals. Preserve their
        # total when mapping to a final-score distribution; do not add OT twice.
        h, a = max(home, 1e-8), max(away, 1e-8)
        p = float(skellam.sf(0, h, a) + 0.5*skellam.pmf(0, h, a))
        total = h+a
        distribution = nhl_score_distribution(p, total)
        margin = sum((hg-ag)*mass for hg, ag, mass in distribution)
        return {'margin': margin, 'total': total, 'home_win_probability': p,
                'home_score': (total+margin)/2, 'away_score': (total-margin)/2,
                'distribution': distribution}

    def artifact(self):
        return {'schema_version': 1, 'model_version': NHL_REPLAY_VERSION,
                'candidate': self.candidate, 'config': asdict(self.config),
                'training_cutoff': self.cutoff.isoformat(), 'training_games': self.training_games,
                'latest_feature_time': self.latest_feature_time.isoformat() if self.latest_feature_time else None,
                'training_source_hash': self.source_ids_hash, 'teams': self.teams,
                'team_game_counts': dict(self.counts),
                'baseline_home_mean': self.home_mean, 'baseline_away_mean': self.away_mean,
                'intercept': float(self.model.intercept_) if self.model is not None else None,
                'coefficients': self.model.coef_.tolist() if self.model is not None else None,
                'feature_order': 'scoring-team indicators, defending-team indicators, home indicator',
                'scope': 'Research only; final-score reconstruction, no historical lineup or xG features.'}


def _choose_nhl_bet(game, prediction, row, quotes, closing_quotes, config):
    if not row['forecast_eligible']:
        return None, ['insufficient_prior_history']
    available, reasons = select_asof_quotes(quotes, row['decision_time'],
                                           config.max_quote_age_minutes, config.allowed_books)
    if not available:
        return None, sorted(set(reasons + ['no_executable_historical_quote']))
    candidates = []
    for quote in available:
        try:
            probabilities = market_probabilities(prediction['distribution'], quote['market'],
                                                  quote['side'], quote.get('line'))
        except (TypeError, ValueError, KeyError):
            reasons.append('unsupported_quote_contract')
            continue
        price = float(quote['odds'])
        payout = price/100 if price > 0 else 100/abs(price)
        ev = probabilities['win']*payout-probabilities['loss']
        if ev < config.minimum_ev:
            continue
        settlement, profit = settle_bet(game, quote)
        candidates.append({**quote, 'win_probability': probabilities['win'],
                           'push_probability': probabilities['push'], 'loss_probability': probabilities['loss'],
                           'expected_value': ev*100, 'settlement': settlement, 'profit_units': profit, 'stake_units': 1.0,
                           'closing_line_value': comparable_clv(quote, closing_quotes, game['_time'])})
    if not candidates:
        return None, sorted(set(reasons+['no_quote_meets_predeclared_ev']))
    return max(candidates, key=lambda b: (b['expected_value'], b['book_key'], b['market'], b['side'])), []


def run_nhl_replay(games, candidate='nhl_poisson', quotes=None, closing_quotes=None, config=None):
    config = config or NHLReplayConfig()
    prepared = _prepare_nhl(games)
    folds, quote_map, closing_map = defaultdict(list), defaultdict(list), defaultdict(list)
    for game in prepared:
        folds[game['_fold']].append(game)
    for quote in quotes or []:
        (closing_map if quote.get('is_closing') else quote_map)[str(quote['game_id'])].append(quote)
    for quote in closing_quotes or []:
        closing_map[str(quote['game_id'])].append(quote)
    output = []
    for fold, targets in sorted(folds.items()):
        selected = [g for g in targets if (config.start_season is None or g['season'] >= config.start_season)
                    and (config.end_season is None or g['season'] <= config.end_season)]
        if not selected:
            continue
        cutoff = min(g['_time']-timedelta(minutes=config.decision_minutes) for g in targets)
        floor = cutoff-timedelta(days=config.training_days)
        history = [g for g in prepared if floor <= g['_time'] < cutoff and g['_fold'] < fold
                   and g['_feature_available'] < cutoff]
        model = NHLScoringModel(history, cutoff, candidate, config)
        baseline = NHLScoringModel(history, cutoff, 'nhl_baseline', config)
        for game in selected:
            forecast, simple = model.predict(game), baseline.predict(game)
            counts = [model.counts[game[s+'_team_id']] for s in ('home', 'away')]
            eligible = len(history) >= config.minimum_training_games and min(counts) >= config.minimum_forecast_team_games
            row = {'league': 'nhl', 'candidate': candidate, 'model_version': NHL_REPLAY_VERSION,
                   'game_id': game['game_id'], 'season': game['season'], 'game_time': game['_time'].isoformat(),
                   'decision_time': (game['_time']-timedelta(minutes=config.decision_minutes)).isoformat(),
                   'training_cutoff': cutoff.isoformat(), 'fold': str(fold), 'training_games': len(history),
                   'latest_feature_time': model.latest_feature_time.isoformat() if model.latest_feature_time else None,
                   'home_prior_games': counts[0], 'away_prior_games': counts[1],
                   'forecast_eligible': eligible, 'advanced_features': False,
                   'predicted_margin': forecast['margin'], 'predicted_total': forecast['total'],
                   'actual_margin': game['home_score']-game['away_score'],
                   'actual_total': game['home_score']+game['away_score'],
                   'home_win_probability': forecast['home_win_probability'], 'push_probability': 0.0,
                   'away_win_probability': 1-forecast['home_win_probability'],
                   'raw_home_conditional': forecast['home_win_probability'],
                   'baseline_margin': simple['margin'], 'baseline_total': simple['total'],
                   'source_provenance': game.get('provenance', game.get('source_url')),
                   'source_data_basis': 'reconstructed_final_score_history',
                   'training_source_hash': model.source_ids_hash,
                   'strategy': f'per_game_T{config.decision_minutes}_weekly_refit_one_bet', 'bet': None}
            row['bet'], row['no_bet_reasons'] = _choose_nhl_bet(
                game, forecast, row, quote_map[game['game_id']], closing_map[game['game_id']], config)
            output.append(row)
    return output


def train_nhl_artifact(games, cutoff, config=None):
    """Persistable JSON coefficients for a research model at an explicit cutoff."""
    config = config or NHLReplayConfig()
    cutoff = timestamp(cutoff)
    floor = cutoff-timedelta(days=config.training_days)
    history = [g for g in _prepare_nhl(games) if floor <= g['_time'] < cutoff and g['_feature_available'] < cutoff]
    if len(history) < config.minimum_training_games:
        raise ValueError('Insufficient games for a trained NHL artifact.')
    return NHLScoringModel(history, cutoff, 'nhl_poisson', config).artifact()
