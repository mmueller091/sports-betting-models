"""Chronological football research replay with explicit information cutoffs.

This is a per-game decision strategy, not a reconstruction of the site's hourly
top-five display. No HTTP calls, API credentials, live ledgers or production
settings are used. Revised historical sources are labeled as reconstruction.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from statistics import mean

from models.football_bets import (
    CONFIG, MODEL_VERSION, _week_start, expected_value, implied_probability,
    margin_settlement, settlement_probabilities,
)
from research.models import CANDIDATES, PriorOnlyCalibrator, fit_candidate

REPLAY_VERSION = 'historical-football-replay-1.0'


@dataclass(frozen=True)
class ReplayConfig:
    decision_minutes: int = 60
    max_quote_age_minutes: int = 26 * 60
    training_scope: str = 'season'
    minimum_forecast_team_games: int = 3
    calibration_minimum: int = 100
    start_season: int | None = None
    end_season: int | None = None
    allowed_books: tuple[str, ...] = ()
    stress_points: float = 3.0
    require_confirmed_availability: bool = False

    def __post_init__(self):
        if self.decision_minutes < 0 or self.max_quote_age_minutes <= 0:
            raise ValueError('Decision lead must be nonnegative and quote age positive.')
        if self.training_scope not in ('season', 'expanding'):
            raise ValueError('training_scope must be season or expanding.')


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _available(game):
    # Availability must be explicit. Retrieval today is not historical knowledge.
    return timestamp(game['available_at'])


def _prepare(games, league):
    rows, seen = [], set()
    for source in games:
        if source.get('league') != league or not source.get('completed', True):
            continue
        game = dict(source)
        identity = str(game['game_id'])
        if identity in seen:
            raise ValueError(f'Duplicate game_id in research input: {identity}')
        seen.add(identity)
        kickoff = timestamp(game['game_time'])
        available = _available(game)
        if available < kickoff:
            raise ValueError(f'Final result availability precedes kickoff: {identity}')
        for key in ('home_score', 'away_score'):
            value = float(game[key])
            if not math.isfinite(value) or value < 0 or value != round(value):
                raise ValueError(f'Invalid final score for {identity}')
            game[key] = value
        if str(game['home_team_id']) == str(game['away_team_id']):
            raise ValueError(f'Identical home and away team: {identity}')
        game.update(game_id=identity, home_team_id=str(game['home_team_id']),
                    away_team_id=str(game['away_team_id']), _time=kickoff,
                    _available=available, _fold=_week_start(kickoff))
        if game.get('closing_home_margin') is None and game.get('closing_home_spread') is not None:
            game['closing_home_margin']=-float(game['closing_home_spread'])
        # A provider's stated feature-availability time can be later than final.
        feature_times=[timestamp(game[key]) for key in ('feature_available_at','metrics_available_at') if game.get(key)]
        game['_feature_available'] = max([available]+feature_times)
        rows.append(game)
    return sorted(rows, key=lambda row: (row['_time'], row['game_id']))


def _quote_key(quote):
    return (str(quote['game_id']), quote.get('book_key', quote.get('book')),
            quote.get('market'), quote.get('side'), quote.get('line'))


def _normalize_quote(quote):
    result = dict(quote)
    result['market'] = {'h2h':'moneyline', 'spreads':'spread', 'totals':'total'}.get(result.get('market'), result.get('market'))
    result['book_key'] = result.get('book_key') or result.get('book')
    result['quote_time'] = result.get('quote_time') or result.get('last_update')
    return result


def select_asof_quotes(quotes, decision_time, max_quote_age_minutes=26*60, allowed_books=()):
    """Select the latest known book/market state, never a superseded older line."""
    decision = timestamp(decision_time)
    accepted, rejected = [], []
    for source in quotes:
        quote = _normalize_quote(source)
        try:
            at = timestamp(quote['quote_time'])
            snapshot = timestamp(quote.get('snapshot_time') or quote['quote_time'])
            odds = float(quote['odds'])
            if not math.isfinite(odds) or abs(odds) < 100 or not quote['book_key']:
                raise ValueError('invalid price/book')
            if quote['market'] not in ('moneyline','spread','total'):
                raise ValueError('unsupported market')
            if quote['market'] != 'moneyline':
                line = float(quote['line'])
                if not math.isfinite(line) or not math.isclose(line*2, round(line*2)):
                    raise ValueError('unsupported line')
                quote['line'] = line
            else:
                quote['line'] = None
        except (KeyError, TypeError, ValueError):
            rejected.append('invalid_or_untimestamped_quote')
            continue
        if at > decision or snapshot > decision:
            rejected.append('quote_or_snapshot_after_decision')
            continue
        if at > snapshot:
            rejected.append('quote_after_source_snapshot')
            continue
        if (decision-at).total_seconds() > max_quote_age_minutes*60:
            rejected.append('stale_quote')
            continue
        if allowed_books and quote['book_key'] not in allowed_books:
            rejected.append('book_not_allowed')
            continue
        accepted.append((snapshot,at,quote))
    market_latest = {}
    for snapshot,_,quote in accepted:
        key=(str(quote['game_id']),quote['book_key'],quote['market'])
        market_latest[key]=max(snapshot,market_latest.get(key,snapshot))
    contracts={}
    for snapshot,at,quote in accepted:
        market_key=(str(quote['game_id']),quote['book_key'],quote['market'])
        if snapshot != market_latest[market_key]:
            continue
        key=_quote_key(quote)
        if key not in contracts or at>contracts[key][0]:
            contracts[key]=(at,quote)
    return [q for _,q in contracts.values()],sorted(set(rejected))


def _quote_probabilities(prediction, quote, league, margin_sigma, total_sigma, calibrator=None, stress=0):
    side, market = quote['side'], quote['market']
    if market == 'total':
        if side not in ('over', 'under'):
            raise ValueError('Invalid total side')
        center = prediction['total'] + (-stress if side == 'over' else stress)
        return settlement_probabilities(center, total_sigma, float(quote['line']), side)
    if side not in ('home', 'away') or market not in ('moneyline', 'spread'):
        raise ValueError('Invalid game market or side')
    center = prediction['margin'] * (1 if side == 'home' else -1) - stress
    threshold = -float(quote['line']) if market == 'spread' else 0
    w, p, l = margin_settlement(center, margin_sigma, threshold, league != 'cfb')
    if market == 'moneyline' and calibrator is not None and calibrator.fitted:
        # Calibrator learned home conditional win probability, not cover/total.
        conditional = w / max(w+l, 1e-12)
        calibrated = calibrator.predict(conditional if side == 'home' else 1-conditional)
        side_probability = calibrated if side == 'home' else 1-calibrated
        w, l = side_probability * (1-p), (1-side_probability) * (1-p)
    return w, p, l


def settle_bet(game, quote):
    margin = game['home_score'] - game['away_score']
    total = game['home_score'] + game['away_score']
    market, side = quote['market'], quote['side']
    if market == 'total':
        result = (total - float(quote['line'])) * (1 if side == 'over' else -1)
    else:
        result = margin * (1 if side == 'home' else -1)
        if market == 'spread':
            result += float(quote['line'])
    outcome = 'push' if abs(result) < 1e-9 else 'win' if result > 0 else 'loss'
    odds = float(quote['odds'])
    profit = (odds/100 if odds > 0 else 100/abs(odds)) if outcome == 'win' else -1.0 if outcome == 'loss' else 0.0
    return outcome, profit


def _closing_clv(bet, closing_quotes, kickoff):
    """No-vig closing benchmark only for the same book and exact contract."""
    same, opposite = [], []
    opposing_side = {'home':'away', 'away':'home', 'over':'under', 'under':'over'}[bet['side']]
    latest,_=select_asof_quotes(closing_quotes,kickoff-timedelta(microseconds=1),allowed_books=(bet['book_key'],))
    for source in latest:
        quote = _normalize_quote(source)
        try:
            at = timestamp(quote['quote_time'])
            price = float(quote['odds'])
        except (KeyError, TypeError, ValueError):
            continue
        if at >= kickoff or abs(price) < 100:
            continue
        if quote.get('book_key') != bet['book_key'] or quote.get('market') != bet['market']:
            continue
        if quote.get('side') == bet['side'] and quote.get('line') == bet.get('line'):
            same.append((at, quote))
        other_line = -float(bet['line']) if bet['market'] == 'spread' else bet.get('line')
        if quote.get('side') == opposing_side and quote.get('line') == other_line:
            opposite.append((at, quote))
    if not same or not opposite:
        return None
    at, q = max(same, key=lambda entry: entry[0])
    # Pair quotes from the same historical snapshot, not unrelated updates.
    pairs = [(t,x) for t,x in opposite if abs((t-at).total_seconds()) <= 300]
    if not pairs:
        return None
    _, other = max(pairs, key=lambda entry: entry[0])
    p, op = implied_probability(q['odds']), implied_probability(other['odds'])
    fair = p/(p+op)
    odds = float(bet['odds'])
    decimal = 1 + (odds/100 if odds > 0 else 100/abs(odds))
    return {'closing_quote_time': at.isoformat(), 'closing_price': q['odds'],
            'closing_no_vig_conditional_probability': fair,
            'closing_no_vig_ev_conditional': fair*decimal-1,
            'basis':'same_book_same_line_conditional_on_no_push'}


def comparable_clv(bet, closing_quotes, kickoff):
    return _closing_clv(bet, closing_quotes, timestamp(kickoff))


def _choose_bet(game, prediction, row, quotes, closing_quotes, prior_errors, calibrator, config):
    reasons, candidates, quote_rejections = [], [], []
    league, rules = row['league'], CONFIG[row['league']]
    if row['training_games'] < rules['minimum_history']:
        reasons.append('insufficient_training_history')
    if min(row['home_prior_games'], row['away_prior_games']) < rules['minimum_team']:
        reasons.append('insufficient_team_history')
    if len(prior_errors) < rules['minimum_holdout']:
        reasons.append('insufficient_prior_forecast_validation')
    decision = timestamp(row['decision_time'])
    confirmation = game.get('availability_confirmed_at')
    if config.require_confirmed_availability and (not confirmation or timestamp(confirmation)>decision):
        reasons.append('historical_availability_unverified')
    latest,quote_rejections=select_asof_quotes(quotes,decision,config.max_quote_age_minutes,config.allowed_books)
    for quote in latest:
        candidate_reasons = list(reasons)
        try:
            w,p,l = _quote_probabilities(prediction,quote,league,row['margin_sigma'],row['total_sigma'],calibrator)
            cw,_,cl = _quote_probabilities(prediction,quote,league,row['margin_sigma'],row['total_sigma'],calibrator,config.stress_points)
        except (KeyError, TypeError, ValueError):
            quote_rejections.append('invalid_market_contract')
            continue
        conditional = w/max(1-p,1e-12)
        ev = expected_value(w,l,quote['odds'])
        conservative = expected_value(cw,cl,quote['odds'])
        target = 'total' if quote['market']=='total' else 'margin'
        if prior_errors and mean(abs(x[target]) for x in prior_errors) >= mean(abs(x['baseline_'+target]) for x in prior_errors):
            candidate_reasons.append('forecast_not_better_than_simple_baseline')
        if conditional-implied_probability(quote['odds']) < rules['minimum_edge'] or ev < rules['minimum_ev']:
            candidate_reasons.append('below_predeclared_edge_threshold')
        if conservative <= 0:
            candidate_reasons.append('fails_adverse_projection_stress')
        candidates.append({**quote, 'win_probability':w,'push_probability':p,'loss_probability':l,
                           'expected_value':ev,'conservative_ev':conservative,'reasons':candidate_reasons})
    eligible = [c for c in candidates if not c['reasons']]
    if not eligible:
        return None, sorted(set(reasons + quote_rejections + ([r for c in candidates for r in c['reasons']] if candidates else ['no_executable_asof_quote'])))
    bet = max(eligible,key=lambda c:(c['conservative_ev'],c['expected_value'],str(c['book_key'])))
    outcome,profit=settle_bet(game,bet)
    bet.update(settlement=outcome,profit_units=profit,stake_units=1.0,
               closing_line_value=_closing_clv(bet,closing_quotes,game['_time']))
    return bet, []


def run_replay(games, league='nfl', candidate='current', quotes=None, closing_quotes=None, config=None):
    """Return immutable-style forecast/decision dictionaries for every eligible source game.

    Inputs are copied; neither the input games nor production CONFIG is mutated.
    Every fold is fit before its first decision. All games in a fold remain out of
    training until the following fold. Source revisions remain reconstruction.
    """
    config = config or ReplayConfig()
    if isinstance(config, dict):
        config = ReplayConfig(**config)
    if league not in CONFIG or candidate not in CANDIDATES:
        raise ValueError('Unsupported football league or research candidate.')
    prepared = _prepare(games,league)
    groups = defaultdict(list)
    for game in prepared:
        groups[game['_fold']].append(game)
    quote_index, close_index = defaultdict(list), defaultdict(list)
    for q in quotes or []:
        (close_index if q.get('is_closing') else quote_index)[str(q['game_id'])].append(q)
    for q in closing_quotes or []:
        close_index[str(q['game_id'])].append(q)
    prior, out, calibration_rows = [], [], []
    errors_by_season = defaultdict(list)
    candidate_definition = CANDIDATES[candidate]
    config_digest = hashlib.sha256(json.dumps(asdict(config),sort_keys=True).encode()).hexdigest()[:16]
    for fold, group in sorted(groups.items()):
        cutoff = min(g['_time']-timedelta(minutes=config.decision_minutes) for g in group)
        seasons={g['season'] for g in group}
        if len(seasons)!=1:
            raise ValueError('A chronological fold contains conflicting season labels.')
        season=next(iter(seasons))
        train=[g for g in prior if g['_available']<cutoff and (config.training_scope=='expanding' or g['season']==season)]
        # Preserve known final scores, but exclude metrics unavailable at this cutoff.
        train_inputs=[]
        for game in train:
            copied=dict(game)
            if game['_feature_available']>=cutoff:
                copied.pop('home_metrics',None);copied.pop('away_metrics',None)
            train_inputs.append(copied)
        ratings=fit_candidate(train_inputs,league,candidate)
        prior_errors=[e for e in errors_by_season[season] if e['available_at']<cutoff]
        calibrator=PriorOnlyCalibrator([r for r in calibration_rows if r['available_at']<cutoff],config.calibration_minimum) if candidate_definition.calibrate_moneyline else None
        completed_fold=[]
        for game in group:
            home,away=game['home_team_id'],game['away_team_id']
            prediction=ratings.predict(game)
            nh,na=ratings.counts[home],ratings.counts[away]
            inflation=math.sqrt(1+1/(nh+4)+1/(na+4))
            sigma={}
            for target in ('margin','total'):
                rmse=math.sqrt(mean(e[target]**2 for e in prior_errors)) if prior_errors else 0
                sigma[target]=max(CONFIG[league][target+'_sigma'],rmse)*inflation
            w,p,l=margin_settlement(prediction['margin'],sigma['margin'],0,league!='cfb')
            raw_conditional=w/max(w+l,1e-12)
            if calibrator is not None and calibrator.fitted:
                conditional=calibrator.predict(raw_conditional)
                w,l=conditional*(1-p),(1-conditional)*(1-p)
            actual_margin=game['home_score']-game['away_score']
            actual_total=game['home_score']+game['away_score']
            known_times=[g['_feature_available'] if g['_feature_available']<cutoff else g['_available'] for g in train]
            row={'league':league,'candidate':candidate,'model_version':REPLAY_VERSION+':'+candidate,
                 'production_formula_version':MODEL_VERSION,'config_hash':config_digest,
                 'game_id':game['game_id'],'season':season,'game_time':game['_time'].isoformat(),
                 'home_team':game.get('home_team',home),'away_team':game.get('away_team',away),
                 'decision_time':(game['_time']-timedelta(minutes=config.decision_minutes)).isoformat(),
                 'training_cutoff':cutoff.isoformat(),'latest_feature_time':max(known_times).isoformat() if known_times else None,
                 'fold':fold.isoformat(),'training_games':len(train),'home_prior_games':nh,'away_prior_games':na,
                 'prior_validation_games':len(prior_errors),'forecast_eligible':len(train)>=CONFIG[league]['minimum_history'] and min(nh,na)>=config.minimum_forecast_team_games,
                 'advanced_features':ratings.advanced,'predicted_margin':prediction['margin'],'predicted_total':prediction['total'],
                 'margin_sigma':sigma['margin'],'total_sigma':sigma['total'],
                 'actual_margin':actual_margin,'actual_total':actual_total,
                 'home_win_probability':w,'push_probability':p,'away_win_probability':l,
                 'raw_home_conditional':raw_conditional,
                 'baseline_margin':0 if game.get('neutral_site') else CONFIG[league]['home_prior'],
                 'baseline_total':2*ratings.mean_score,
                 'calibration_fitted':bool(calibrator and calibrator.fitted),'calibration_training_games':calibrator.n if calibrator else 0,
                 'closing_home_margin':game.get('closing_home_margin'),'closing_total':game.get('closing_total'),
                 'closing_home_moneyline':game.get('closing_home_moneyline'),'closing_away_moneyline':game.get('closing_away_moneyline'),
                 'data_mode':'reconstructed_from_retrospective_source_editions',
                 'source_provenance':{k:game.get(k) for k in ('source_url','retrieved_at','data_revision','availability_basis','available_at')},
                 'selection_policy':'one_qualified_bet_per_game_at_predeclared_cutoff',
                 'availability_mode':'not_backfilled' if not game.get('availability_confirmed_at') else 'provided',
                 'bet':None,'no_bet_reasons':[]}
            if row['latest_feature_time'] and timestamp(row['latest_feature_time'])>=cutoff:
                raise AssertionError('Future feature reached training.')
            row['bet'],row['no_bet_reasons']=_choose_bet(game,prediction,row,quote_index[game['game_id']],close_index[game['game_id']],prior_errors,calibrator,config)
            if (config.start_season is None or season>=config.start_season) and (config.end_season is None or season<=config.end_season):
                out.append(row)
            completed_fold.append((game,row))
        # Only after every forecast in the fold is frozen may its labels be added.
        for game,row in completed_fold:
            if row['forecast_eligible']:
                errors_by_season[season].append({'available_at':game['_available'],
                    'margin':row['actual_margin']-row['predicted_margin'],'total':row['actual_total']-row['predicted_total'],
                    'baseline_margin':row['actual_margin']-row['baseline_margin'],'baseline_total':row['actual_total']-row['baseline_total']})
                calibration_rows.append({**row,'available_at':game['_available']})
        prior.extend(group)
    return out
