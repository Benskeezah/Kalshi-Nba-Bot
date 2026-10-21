"""SIMULATED calibration demo. Writes fake resolved predictions (flagged is_mock=1)
into a separate database so you can see what the calibration report looks like.
None of these numbers are real results.

    KALSHI_EDGE_DB=sim.sqlite python examples/simulate_calibration.py
    KALSHI_EDGE_DB=sim.sqlite python -m kalshi_edge calibration --include-mock
"""
import math
import os
import random
import sqlite3

from kalshi_edge.store import PredictionStore

db = os.environ.get("KALSHI_EDGE_DB", "sim.sqlite")
store = PredictionStore(db)
rnd = random.Random(7)
rows = []
for i in range(300):
    true = rnd.uniform(0.03, 0.97)
    outcome = 1 if rnd.random() < true else 0
    lg = math.log(true / (1 - true))
    model = 1 / (1 + math.exp(-(1.5 * lg + rnd.gauss(0, 0.5))))     # deliberately overconfident
    market = 1 / (1 + math.exp(-(lg + rnd.gauss(0, 0.35))))         # noisier than truth, unbiased
    edge = (model - market) * 100
    verdict = "POTENTIAL EDGE" if abs(edge) >= 8 else "NO CLEAR EDGE"
    side = "YES" if edge > 0 else "NO"
    fill = min(0.99, (market if side == "YES" else 1 - market) + 0.01)
    fee = math.ceil(7 * fill * (1 - fill)) / 100
    rows.append((f"SIM-{i:04d}", "simulated", model, market, edge, verdict, side, fill, fee, outcome,
                 "LOW" if i % 3 else "MEDIUM"))
store.conn.executemany(
    """INSERT INTO predictions (created_at, ticker, estimator, model_prob, market_prob, edge_pp, verdict,
       best_side, best_fill, best_fee, outcome, confidence, is_mock, resolved_at)
       VALUES ('2026-01-01T00:00:00Z',?,?,?,?,?,?,?,?,?,?,?,1,'2026-01-02T00:00:00Z')""", rows)
store.conn.commit()
print(f"Wrote {len(rows)} SIMULATED predictions to {db}")
