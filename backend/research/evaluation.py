"""Forecast and exact-price strategy evaluation with clustered uncertainty."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
from statistics import mean

import numpy as np
from models.football_bets import implied_probability


def _mean(values):
    return mean(values) if values else None


def _cluster(row):
    # Same-game wagers are already limited to one. A week cluster also preserves
    # common schedule effects and same-week cross-team dependence.
    if row.get('fold'):
        return str(row['league'])+':'+str(row['fold'])
    at=datetime.fromisoformat(str(row['game_time']).replace('Z','+00:00'))
    iso=at.isocalendar()
    return f"{row['league']}:{iso.year}-{iso.week:02d}"


def _binary_rows(rows):
    output=[]
    for row in rows:
        if row['actual_margin']==0:
            continue
        w,l=row['home_win_probability'],row['away_win_probability']
        p=max(1e-12,min(1-1e-12,w/max(w+l,1e-12)))
        y=int(row['actual_margin']>0)
        output.append((row,p,y))
    return output


def cluster_interval(rows, value_fn, samples=500, seed=20261112):
    """Percentile interval for a mean, resampling complete weeks with replacement."""
    groups=defaultdict(list)
    for row in rows:
        value=value_fn(row)
        if value is not None and math.isfinite(value):
            groups[_cluster(row)].append(float(value))
    if len(groups)<2 or samples<1:
        return None
    grouped=list(groups.values())
    sums=np.asarray([sum(group) for group in grouped])
    counts=np.asarray([len(group) for group in grouped])
    rng=np.random.default_rng(seed)
    values=[]
    for _ in range(samples):
        draw=rng.integers(0,len(grouped),size=len(grouped))
        values.append(float(sums[draw].sum()/counts[draw].sum()))
    low,high=np.quantile(values,[.025,.975])
    return {'low':float(low),'high':float(high),'level':.95,'cluster':'league_week',
            'clusters':len(grouped),'bootstrap_samples':samples,'seed':seed}


def _summarize(rows, bootstrap_samples, seed):
    eligible=[row for row in rows if row.get('forecast_eligible',True)]
    binary=_binary_rows(eligible)
    metrics={'source_games':len(rows),'forecast_games':len(eligible),
             'binary_probability_games':len(binary),'ties_excluded_from_conditional_metrics':len(eligible)-len(binary)}
    for target in ('margin','total'):
        errors=[row['actual_'+target]-row['predicted_'+target] for row in eligible]
        metrics[target+'_mae']=_mean([abs(e) for e in errors])
        metrics[target+'_rmse']=math.sqrt(_mean([e*e for e in errors])) if errors else None
        metrics[target+'_mean_error']=_mean(errors)
        baseline=[abs(row['actual_'+target]-row['baseline_'+target]) for row in eligible if row.get('baseline_'+target) is not None]
        metrics['baseline_'+target+'_mae']=_mean(baseline)
        closing_key='closing_home_margin' if target=='margin' else 'closing_total'
        paired=[row for row in eligible if row.get(closing_key) is not None]
        metrics['closing_'+target+'_games']=len(paired)
        metrics['closing_'+target+'_mae']=_mean([abs(row['actual_'+target]-row[closing_key]) for row in paired])
        metrics['model_'+target+'_mae_on_market_sample']=_mean([abs(row['actual_'+target]-row['predicted_'+target]) for row in paired])
        metrics[target+'_mae_interval']=cluster_interval(eligible,lambda row:abs(row['actual_'+target]-row['predicted_'+target]),bootstrap_samples,seed)
    metrics['brier']=_mean([(p-y)**2 for _,p,y in binary])
    metrics['log_loss']=_mean([-(y*math.log(p)+(1-y)*math.log(1-p)) for _,p,y in binary])
    metrics['mean_home_probability']=_mean([p for _,p,_ in binary])
    metrics['observed_home_win_rate']=_mean([y for _,_,y in binary])
    market_binary=[]
    for row,_,y in binary:
        home,away=row.get('closing_home_moneyline'),row.get('closing_away_moneyline')
        if home is not None and away is not None and abs(home)>=100 and abs(away)>=100:
            ph,pa=implied_probability(home),implied_probability(away)
            market_binary.append((row,ph/(ph+pa),y))
    metrics['closing_moneyline_games']=len(market_binary)
    metrics['closing_moneyline_brier']=_mean([(p-y)**2 for _,p,y in market_binary])
    metrics['closing_moneyline_log_loss']=_mean([-(y*math.log(p)+(1-y)*math.log(1-p)) for _,p,y in market_binary])
    matched_ids={row['game_id'] for row,_,_ in market_binary}
    metrics['model_brier_on_closing_moneyline_sample']=_mean([(p-y)**2 for row,p,y in binary if row['game_id'] in matched_ids])
    brier_by_id={row['game_id']:(p-y)**2 for row,p,y in binary}
    metrics['brier_interval']=cluster_interval(eligible,lambda row:brier_by_id.get(row['game_id']),bootstrap_samples,seed)
    metrics['calibration_bins']=[]
    for number in range(10):
        lower,upper=number/10,(number+1)/10
        selected=[(p,y) for _,p,y in binary if lower<=p<upper or (number==9 and p==1)]
        if selected:
            metrics['calibration_bins'].append({'lower':lower,'upper':upper,'n':len(selected),
                                                'mean_probability':mean(p for p,_ in selected),'observed_rate':mean(y for _,y in selected)})
    bets=[row for row in rows if row.get('bet') is not None]
    stakes=sum(row['bet'].get('stake_units',1.0) for row in bets)
    profit=sum(row['bet']['profit_units'] for row in bets)
    clv=[row['bet']['closing_line_value']['closing_no_vig_ev_conditional'] for row in bets if row['bet'].get('closing_line_value')]
    metrics['betting']={'priced_bets':len(bets),'staked_units':stakes,'profit_units':profit if bets else None,
                       'roi':profit/stakes if stakes else None,
                       'wins':sum(row['bet']['settlement']=='win' for row in bets),
                       'losses':sum(row['bet']['settlement']=='loss' for row in bets),
                       'pushes':sum(row['bet']['settlement']=='push' for row in bets),
                       'roi_interval':cluster_interval(bets,lambda row:row['bet']['profit_units'],bootstrap_samples,seed),
                       'comparable_clv_bets':len(clv),'mean_closing_no_vig_ev_conditional':_mean(clv),
                       'status':'exact_historical_quotes' if bets else 'not_tested_no_qualifying_exact_price_bets'}
    reasons=defaultdict(int)
    for row in rows:
        for reason in row.get('no_bet_reasons',[]):
            reasons[reason]+=1
    metrics['no_bet_reason_counts']=dict(sorted(reasons.items()))
    return metrics


def evaluate_forecasts(rows, bootstrap_samples=500, seed=20261112):
    """Evaluate a single candidate; never pool multiple models as extra samples."""
    rows=list(rows)
    identities=[(row.get('candidate'),row['game_id']) for row in rows]
    if len(identities)!=len(set(identities)):
        raise ValueError('Duplicate candidate/game forecasts cannot be counted twice.')
    candidates={row.get('candidate') for row in rows}
    if len(candidates)>1:
        raise ValueError('Evaluate each candidate separately, then compare matched games.')
    result={'candidate':next(iter(candidates)) if candidates else None,
            'overall':_summarize(rows,bootstrap_samples,seed),'seasons':{},
            'notes':['Forecast metrics use all forecast-eligible held-out games, not just selected bets.',
                     'Win probabilities are evaluated conditional on no tie; actual ties are excluded from binary metrics.',
                     'Intervals resample complete league weeks; they do not correct model-selection bias.',
                     'Returns require an exact timestamped quoted contract. Missing odds do not become assumed -110 bets.',
                     'Historical reconstruction may contain revised source editions; it is not exact archived feed replay.']}
    for season in sorted({row['season'] for row in rows}):
        result['seasons'][str(season)]=_summarize([row for row in rows if row['season']==season],bootstrap_samples,seed)
    return result


def compare_candidates(left_rows,right_rows,bootstrap_samples=500,seed=20261112):
    """Paired score-error differences on common eligible games. Negative favors left."""
    left={row['game_id']:row for row in left_rows if row.get('forecast_eligible',True)}
    right={row['game_id']:row for row in right_rows if row.get('forecast_eligible',True)}
    shared=sorted(left.keys()&right.keys())
    differences=[]
    for key in shared:
        a,b=left[key],right[key]
        if a['actual_margin']!=b['actual_margin'] or a['actual_total']!=b['actual_total']:
            raise ValueError('Candidate outcomes differ for the same game.')
        row={'game_id':key,'league':a['league'],'game_time':a['game_time'],'fold':a.get('fold')}
        for target in ('margin','total'):
            row[target]=abs(a['actual_'+target]-a['predicted_'+target])-abs(b['actual_'+target]-b['predicted_'+target])
        if a['actual_margin']!=0:
            y=int(a['actual_margin']>0)
            pa=a['home_win_probability']/max(a['home_win_probability']+a['away_win_probability'],1e-12)
            pb=b['home_win_probability']/max(b['home_win_probability']+b['away_win_probability'],1e-12)
            row['brier']=(pa-y)**2-(pb-y)**2
        else:
            row['brier']=None
        differences.append(row)
    return {'matched_games':len(shared),
            **{target:{'mean_absolute_error_difference':_mean([row[target] for row in differences]),
                       'interval':cluster_interval(differences,lambda row:row[target],bootstrap_samples,seed)} for target in ('margin','total')},
            'brier':{'mean_difference':_mean([row['brier'] for row in differences if row['brier'] is not None]),
                     'interval':cluster_interval(differences,lambda row:row['brier'],bootstrap_samples,seed)},
            'interpretation':'Negative differences favor the first candidate; discovery comparisons still require untouched validation.'}
