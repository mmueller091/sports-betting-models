import copy
from datetime import datetime, timedelta, timezone
import unittest

from research.models import CANDIDATES, fit_candidate
from research.replay import ReplayConfig, comparable_clv, run_replay, select_asof_quotes, settle_bet


def games(weeks=10, league='nfl', first_year=2023):
    start=datetime(first_year,9,3,18,tzinfo=timezone.utc)
    result=[]
    for week in range(weeks):
        for pair in range(8):
            kickoff=start+timedelta(weeks=week,hours=pair)
            result.append({'league':league,'game_id':f'{first_year}:{week}:{pair}', 'season':first_year,
                'game_time':kickoff.isoformat(),'available_at':(kickoff+timedelta(hours=12)).isoformat(),
                'home_team_id':str(pair*2),'away_team_id':str(pair*2+1),
                'home_team':f'Home{pair}','away_team':f'Away{pair}',
                'home_score':30+week%3,'away_score':10+pair%3,'completed':True,'neutral_site':False,
                'source_url':'fixture','retrieved_at':'2026-11-12T12:00:00Z',
                'availability_basis':'synthetic_fixture','data_revision':'fixture'})
    return result


def quote(at='2023-11-05T16:00:00Z', line=-3, side='home', **extra):
    return {'game_id':'g','market':'spread','side':side,'line':line,'odds':-110,
            'book_key':'book','quote_time':at,'snapshot_time':at,**extra}


class ResearchLeakageTests(unittest.TestCase):
    def test_target_and_later_week_outcomes_do_not_change_frozen_forecasts(self):
        source=games()
        a=run_replay(source)
        changed=copy.deepcopy(source)
        for game in changed[-8:]:
            game['home_score']=0;game['away_score']=80
        b=run_replay(changed)
        for original, altered in zip(a,b):
            self.assertEqual(original['predicted_margin'],altered['predicted_margin'])
            self.assertEqual(original['home_win_probability'],altered['home_win_probability'])

    def test_same_fold_earlier_outcome_cannot_change_later_game_prediction(self):
        source=games()
        a=run_replay(source)
        changed=copy.deepcopy(source)
        changed[-8]['home_score']=90
        b=run_replay(changed)
        self.assertEqual(a[-1]['predicted_margin'],b[-1]['predicted_margin'])

    def test_delayed_result_and_features_do_not_enter_early_training(self):
        source=games()
        source[0]['available_at']='2024-01-01T12:00:00Z'
        a=run_replay(source)
        changed=copy.deepcopy(source)
        changed[0]['home_score']=99
        b=run_replay(changed)
        self.assertEqual([r['predicted_margin'] for r in a[8:]], [r['predicted_margin'] for r in b[8:]])
        for row in a:
            self.assertTrue(row['latest_feature_time'] is None or row['latest_feature_time']<=row['training_cutoff'])

    def test_feature_availability_is_independent_of_final_result_availability(self):
        source=games()
        metrics={'epa_per_play':.1,'defensive_epa_per_play':-.1,'pass_epa':.1,'defensive_pass_epa':-.1,
                 'success_rate':.5,'defensive_success_rate':.4,'explosive_rate':.1,'defensive_explosive_rate':.1,
                 'sack_rate':.07,'pressure_sack_rate':.07,'plays':60,'defensive_plays':60,'dropbacks':35,'defensive_dropbacks':35}
        for game in source:
            game['home_metrics']=dict(metrics);game['away_metrics']=dict(metrics)
        source[0]['feature_available_at']='2024-01-01T12:00:00Z'
        a=run_replay(source)
        changed=copy.deepcopy(source)
        changed[0]['home_metrics']['epa_per_play']=99
        b=run_replay(changed)
        self.assertEqual([r['predicted_margin'] for r in a],[r['predicted_margin'] for r in b])
        # The source adapter's alias must enforce the same availability boundary.
        source[0]['metrics_available_at']=source[0].pop('feature_available_at')
        changed[0]['metrics_available_at']=changed[0].pop('feature_available_at')
        a,b=run_replay(source),run_replay(changed)
        self.assertEqual([r['predicted_margin'] for r in a],[r['predicted_margin'] for r in b])

    def test_no_prices_means_no_returns_and_inputs_unchanged(self):
        source=games()
        before=copy.deepcopy(source)
        rows=run_replay(source)
        self.assertEqual(source,before)
        self.assertTrue(all(r['bet'] is None for r in rows))
        self.assertTrue(all('no_executable_asof_quote' in r['no_bet_reasons'] for r in rows))

    def test_closing_spread_is_benchmark_only_and_uses_correct_sign(self):
        source=games(1)
        source[0].update(closing_home_spread=-3.5,closing_home_moneyline=-150,closing_away_moneyline=130)
        row=run_replay(source)[0]
        self.assertEqual(row['closing_home_margin'],3.5)
        self.assertIsNone(row['bet'])

    def test_duplicate_game_and_missing_availability_are_rejected(self):
        source=games(1)
        with self.assertRaises(ValueError):run_replay(source+[source[0]])
        del source[0]['available_at']
        with self.assertRaises(KeyError):run_replay(source)

    def test_calibrator_uses_only_available_earlier_predictions(self):
        # Enough varied outcomes to fit calibration; final-fold labels must not leak.
        source=games(25)
        for i,game in enumerate(source):
            if i%8>=4:game['home_score'],game['away_score']=10,30
        a=run_replay(source,candidate='current_calibrated')
        changed=copy.deepcopy(source)
        for game in changed[-8:]:game['home_score'],game['away_score']=99,0
        b=run_replay(changed,candidate='current_calibrated')
        self.assertTrue(a[-1]['calibration_fitted'])
        self.assertEqual(a[-1]['home_win_probability'],b[-1]['home_win_probability'])
        self.assertEqual(a[-1]['calibration_training_games'],b[-1]['calibration_training_games'])

    def test_fixed_home_candidate_does_not_mutate_production(self):
        from models.football_bets import CONFIG
        before=copy.deepcopy(CONFIG)
        source=games(10,league='cfb')
        fixed=run_replay(source,league='cfb',candidate='cfb_fixed_home')
        self.assertEqual(CONFIG,before)
        self.assertEqual(len(fixed),len(source))
        with self.assertRaises(ValueError):run_replay(games(1),candidate='cfb_fixed_home')


class ResearchQuoteTests(unittest.TestCase):
    def test_future_snapshot_rejected_even_when_book_timestamp_is_old(self):
        q=quote(snapshot_time='2023-11-05T18:00:00Z')
        selected,reasons=select_asof_quotes([q],'2023-11-05T17:00:00Z')
        self.assertEqual(selected,[])
        self.assertIn('quote_or_snapshot_after_decision',reasons)

    def test_moved_line_does_not_leave_stale_better_contract_available(self):
        old=quote(line=-3)
        new=quote(at='2023-11-05T16:45:00Z',line=-4)
        selected,_=select_asof_quotes([old,new],'2023-11-05T17:00:00Z')
        self.assertEqual([q['line'] for q in selected],[-4])

    def test_stale_and_disallowed_prices_rejected(self):
        selected,reasons=select_asof_quotes([quote()],'2023-11-05T17:00:00Z',max_quote_age_minutes=30)
        self.assertEqual(selected,[]);self.assertIn('stale_quote',reasons)
        selected,reasons=select_asof_quotes([quote()],'2023-11-05T17:00:00Z',allowed_books=('other',))
        self.assertEqual(selected,[]);self.assertIn('book_not_allowed',reasons)

    def test_exact_price_and_push_settlement(self):
        game={'home_score':24,'away_score':21}
        self.assertEqual(settle_bet(game,quote(line=-3)),('push',0.0))
        outcome,profit=settle_bet(game,quote(line=-2.5,odds=150))
        self.assertEqual((outcome,profit),('win',1.5))
        self.assertEqual(settle_bet(game,quote(line=-3.5)),('loss',-1.0))

    def test_clv_requires_same_contract_and_two_sides(self):
        bet=quote(line=-3,odds=100)
        closing=[quote(at='2023-11-05T17:55:00Z',line=-3,odds=-120),
                 quote(at='2023-11-05T17:55:00Z',line=3,side='away',odds=100)]
        clv=comparable_clv(bet,closing,'2023-11-05T18:00:00Z')
        self.assertIsNotNone(clv)
        self.assertGreater(clv['closing_no_vig_ev_conditional'],0)
        self.assertIsNone(comparable_clv(bet,closing[:1],'2023-11-05T18:00:00Z'))
        moved=[quote(at='2023-11-05T17:58:00Z',line=-4),
               quote(at='2023-11-05T17:58:00Z',line=4,side='away')]
        self.assertIsNone(comparable_clv(bet,closing+moved,'2023-11-05T18:00:00Z'))


if __name__=='__main__':unittest.main()
