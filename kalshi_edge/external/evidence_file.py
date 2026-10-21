"""Hand-entered evidence, loaded from a JSON file you control.

This is how research you did yourself (a forecast you read, a sportsbook line,
a base rate you counted) gets into the model with its source and date attached.
Entries are keyed by market ticker or event ticker (the more specific wins).

{
  "KXHIGHNY-26OCT22": {                       # weather: point forecasts
    "forecasts": [
      {"source": "NWS point forecast", "high_f": 65,
       "issued_at": "2026-10-21T06:19:00Z", "url": "...", "weight": 2}
    ]
  },
  "SOME-TICKER": {                            # generic: base rate + other estimates
    "base_rate": {"successes": 41, "trials": 60, "source": "...", "as_of": "2026-10-01"},
    "estimates": [{"p": 0.62, "source": "devigged sportsbook line", "weight": 1,
                   "as_of": "2026-10-21T15:00:00Z"}],
    "adjustments": [{"log_odds": -0.2, "reason": "starter out", "source": "..."}]
  },
  "KXMVE...": {                               # parlays: one probability per leg
    "legs": {"yes Pittsburgh": {"p": 0.58, "source": "...", "as_of": "..."}},
    "legs_independent": false
  },
  "_mock": false                              # set true for invented demo data
}
"""
from __future__ import annotations

import json
from typing import Optional


class EvidenceFile:
    def __init__(self, path: Optional[str] = None, data: Optional[dict] = None):
        if path:
            with open(path) as f:
                data = json.load(f)
        self.data = data or {}
        self.is_mock = bool(self.data.get("_mock", False))

    def for_market(self, ticker: str, event_ticker: str) -> dict:
        merged = dict(self.data.get(event_ticker, {}))
        merged.update(self.data.get(ticker, {}))
        if merged:
            merged.setdefault("_mock", self.is_mock)
        return merged
