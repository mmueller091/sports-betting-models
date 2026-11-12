import unittest

from research.evaluation import compare_candidates, evaluate_forecasts


def row(game_id,week=1,home_win=True,probability=.75):
    return {'league':'nfl','candidate':'current','game_id':game_id,'season':2023,
            'game_time':f'2023-10-{week*7:02d}T18:00:00Z','fold':f'2023-10-{week*7:02d}',
            'forecast_eligible':True,'predicted_margin':3,'predicted_total':45,
            'actual_margin':7 if home_win else -7,'actual_total':45,
            'home_win_probability':probability,'push_probability':0,'away_win_probability':1-probability,
            'baseline_margin':1.5,'baseline_total':44,'bet':None,'no_bet_reasons':['no_executable_asof_quote']}


class ResearchEvaluationTests(unittest.TestCase):
    def test_probabilities_and_no_odds_report_no_roi(self):
        rows=[row('a'),row('b',2,False)]
        result=evaluate_forecasts(rows,bootstrap_samples=20)['overall']
        self.assertAlmostEqual(result['brier'],(.25**2+.75**2)/2)
        self.assertEqual(result['forecast_games'],2)
        self.assertIsNone(result['betting']['roi'])
        self.assertIsNone(result['betting']['profit_units'])
        self.assertEqual(result['betting']['priced_bets'],0)

    def test_duplicate_or_multiple_candidates_cannot_inflate_samples(self):
        a=row('a')
        with self.assertRaises(ValueError):evaluate_forecasts([a,a])
        b=row('b');b['candidate']='scoring_only'
        with self.assertRaises(ValueError):evaluate_forecasts([a,b])

    def test_warmup_rows_are_not_scored_and_ties_not_forced_into_binary_losses(self):
        a=row('a');a['forecast_eligible']=False
        b=row('b');b['actual_margin']=0
        c=row('c',2)
        result=evaluate_forecasts([a,b,c],bootstrap_samples=10)['overall']
        self.assertEqual(result['forecast_games'],2)
        self.assertEqual(result['binary_probability_games'],1)
        self.assertEqual(result['ties_excluded_from_conditional_metrics'],1)

    def test_exact_stake_profit_and_cluster_interval(self):
        rows=[row('a'),row('b',2)]
        for r,profit,outcome in zip(rows,[1.5,-1],['win','loss']):
            r['bet']={'profit_units':profit,'stake_units':1,'settlement':outcome,'closing_line_value':None}
        summary=evaluate_forecasts(rows,bootstrap_samples=20)['overall']['betting']
        self.assertAlmostEqual(summary['roi'],.25)
        self.assertEqual(summary['roi_interval']['clusters'],2)
        self.assertEqual(summary['roi_interval']['cluster'],'league_week')

    def test_paired_comparison_uses_common_games(self):
        a,b=row('a'),row('b',2)
        better=dict(a,predicted_margin=6,candidate='challenger')
        comparison=compare_candidates([better],[a,b],bootstrap_samples=0)
        self.assertEqual(comparison['matched_games'],1)
        self.assertLess(comparison['margin']['mean_absolute_error_difference'],0)


if __name__=='__main__':unittest.main()
