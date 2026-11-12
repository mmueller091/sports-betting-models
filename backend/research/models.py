"""Predeclared research candidates; production model configuration is never changed."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math

import numpy as np
from sklearn.linear_model import LogisticRegression

from models.football_bets import FootballRatings


@dataclass(frozen=True)
class Candidate:
    name: str
    description: str
    remove_advanced: bool = False
    fixed_home: bool = False
    calibrate_moneyline: bool = False


CANDIDATES = {
    'current': Candidate('current', 'Unmodified production football formula, reconstructed historical inputs.'),
    'scoring_only': Candidate('scoring_only', 'Production formula with advanced play features removed.', remove_advanced=True),
    'cfb_fixed_home': Candidate('cfb_fixed_home', 'CFB team ratings refitted with the existing 2.5-point home prior held fixed.', fixed_home=True),
    'current_calibrated': Candidate('current_calibrated', 'Production formula with prior out-of-fold moneyline calibration.', calibrate_moneyline=True),
    'cfb_fixed_home_calibrated': Candidate('cfb_fixed_home_calibrated', 'Fixed CFB home prior with prior out-of-fold moneyline calibration.', fixed_home=True, calibrate_moneyline=True),
}


class FixedHomeRatings(FootballRatings):
    def _margin_x(self, home, away, neutral, home_rates, away_rates):
        row = super()._margin_x(home, away, neutral, home_rates, away_rates)
        row[len(self.index)] = 0
        return row


def fit_candidate(games, league, candidate='current'):
    definition = CANDIDATES[candidate]
    if definition.fixed_home and league != 'cfb':
        raise ValueError('The fixed-home candidate is restricted to CFB.')
    prepared = deepcopy(games)
    if definition.remove_advanced:
        for game in prepared:
            game.pop('home_metrics', None)
            game.pop('away_metrics', None)
    return (FixedHomeRatings if definition.fixed_home else FootballRatings)(prepared, league)


class PriorOnlyCalibrator:
    """Logistic calibration fitted only to available, earlier-fold predictions.

    The fixed minimum and regularization are research configuration, not tuned on
    the final test season. Ties are omitted and push mass is preserved separately.
    """
    def __init__(self, forecasts, minimum=100):
        usable = [row for row in forecasts if row.get('actual_margin') != 0]
        self.n = len(usable)
        self.model = None
        labels = [int(row['actual_margin'] > 0) for row in usable]
        if self.n >= minimum and min(labels.count(0), labels.count(1)) >= 20:
            x = np.asarray([[self._logit(row['raw_home_conditional'])] for row in usable])
            fitted = LogisticRegression(C=1.0, solver='lbfgs', max_iter=500, random_state=20261112).fit(x, labels)
            # Preserve the meaning of an adverse projection shift. A negative
            # calibration slope would turn the fixed stress test into a boost.
            if fitted.coef_[0, 0] > 0:
                self.model = fitted

    @staticmethod
    def _logit(probability):
        p = min(1 - 1e-8, max(1e-8, probability))
        return math.log(p / (1 - p))

    def predict(self, probability):
        if self.model is None:
            return probability
        return float(self.model.predict_proba([[self._logit(probability)]])[0, 1])

    @property
    def fitted(self):
        return self.model is not None

