"""Prediction log (SQLite). Every analysis is stored before anyone acts on it."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from .models import Analysis, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  ticker TEXT NOT NULL, event_ticker TEXT, series_ticker TEXT, title TEXT,
  close_time TEXT,
  estimator TEXT, model_prob REAL, p_low REAL, p_high REAL, confidence TEXT,
  market_prob REAL, yes_bid REAL, yes_ask REAL, spread REAL,
  volume_24h REAL, open_interest REAL, market_fetched_at TEXT, market_source TEXT,
  edge_pp REAL, verdict TEXT, best_side TEXT, best_fill REAL, best_fee REAL, best_ev_net REAL,
  evidence_json TEXT, is_mock INTEGER DEFAULT 0,
  outcome INTEGER, resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_pred_ticker ON predictions(ticker);
"""


class PredictionStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def log(self, a: Analysis) -> int:
        m, e = a.market, a.estimate
        best = {"YES": a.yes_cost, "NO": a.no_cost}.get(a.best_side or "")
        cur = self.conn.execute(
            """INSERT INTO predictions (created_at, ticker, event_ticker, series_ticker, title, close_time,
               estimator, model_prob, p_low, p_high, confidence, market_prob, yes_bid, yes_ask, spread,
               volume_24h, open_interest, market_fetched_at, market_source, edge_pp, verdict, best_side,
               best_fill, best_fee, best_ev_net, evidence_json, is_mock)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (e.created_at.isoformat(), m.ticker, m.event_ticker, m.series_ticker,
             f"{m.title} [{m.yes_sub_title}]", m.close_time.isoformat() if m.close_time else None,
             e.estimator, e.p_yes, e.p_low, e.p_high, e.confidence, a.market_prob, m.yes_bid, m.yes_ask,
             m.spread, m.volume_24h, m.open_interest, m.fetched_at.isoformat(), m.source,
             a.edge_pp, a.verdict, a.best_side,
             best.avg_fill if best else None, best.fee_per_contract if best else None,
             best.ev_net if best else None,
             json.dumps({"evidence": [x.to_dict() for x in e.evidence], "assumptions": e.assumptions,
                         "insufficient_reason": e.insufficient_reason,
                         "params": {k: v for k, v in (e.params or {}).items()
                                    if isinstance(v, (int, float, str, bool)) or v is None}}),
             int(any(x.is_mock for x in e.evidence))))
        self.conn.commit()
        return cur.lastrowid

    def resolved_count(self, estimator: str) -> int:
        r = self.conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM predictions WHERE estimator=? AND outcome IS NOT NULL "
            "AND is_mock=0", (estimator,)).fetchone()
        return r[0]

    def unresolved_tickers(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT ticker FROM predictions WHERE outcome IS NULL")]

    def set_outcome(self, ticker: str, outcome: int) -> int:
        cur = self.conn.execute("UPDATE predictions SET outcome=?, resolved_at=? WHERE ticker=?",
                                (outcome, utcnow().isoformat(), ticker))
        self.conn.commit()
        return cur.rowcount

    def resolved(self, include_mock: bool = False) -> list[dict]:
        """Latest prediction per ticker (per estimator) that has an outcome."""
        q = """SELECT p.* FROM predictions p JOIN (
                 SELECT ticker, estimator, MAX(id) AS mid FROM predictions
                 WHERE outcome IS NOT NULL AND model_prob IS NOT NULL {mock}
                 GROUP BY ticker, estimator) l ON p.id = l.mid"""
        rows = self.conn.execute(q.format(mock="" if include_mock else "AND is_mock=0"))
        return [dict(r) for r in rows]
