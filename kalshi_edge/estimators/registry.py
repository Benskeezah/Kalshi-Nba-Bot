"""Picks an estimator for a contract. Unknown market types -> INSUFFICIENT DATA."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..external.evidence_file import EvidenceFile
from ..external.nba_odds import NBAOddsProvider
from ..external.nba_props import PropProvider
from ..external.nba_stats import StatsDB
from ..models import ContractSpec, Estimate
from .evidence import EvidenceEstimator
from .nba import NBAEstimator, nba_leg_resolver, nba_same_game_joint
from .nba_props import NBAPropEstimator, prop_leg_resolver, prop_same_game_joint
from .parlay import ParlayEstimator
from .weather import WeatherHighEstimator


class EstimatorRegistry:
    def __init__(self, use_live_sources: bool = True, evidence: Optional[EvidenceFile] = None,
                 stats_db: Optional[StatsDB] = None, stats_mock: bool = False):
        data = evidence.data if evidence else {}
        # Hand-entered sportsbook lines (evidence file key "nba_lines") seed the provider.
        self.nba_odds = NBAOddsProvider(use_live_sources, data.get("nba_lines"),
                                        evidence_mock=bool(data.get("_mock")))
        # Hand-entered prop lines ("nba_props") and game logs ("nba_logs").
        self.props = PropProvider(use_live_sources, data.get("nba_props"), data.get("nba_logs"),
                                  mock=bool(data.get("_mock")))
        self.stats = stats_db
        props_est = NBAPropEstimator(self.props, self.nba_odds, stats_db, use_live=use_live_sources,
                                     stats_mock=stats_mock)
        # Order matters: the most specific model first.
        self.estimators = [
            WeatherHighEstimator(use_live_sources),
            NBAEstimator(self.nba_odds),
            props_est,
            ParlayEstimator(resolvers=[nba_leg_resolver(self.nba_odds), prop_leg_resolver(props_est)],
                            joint_fns=[nba_same_game_joint, prop_same_game_joint]),
            EvidenceEstimator(),
        ]

    def estimate(self, spec: ContractSpec, evidence: dict,
                 now: Optional[datetime] = None) -> Estimate:
        for est in self.estimators:
            if est.applies(spec, evidence):
                return est.estimate(spec, evidence, now)
        return Estimate.insufficient_data(
            "none", "No model covers this market type and no evidence was supplied. "
                    "Add a base rate or independent estimates to the evidence file.")
