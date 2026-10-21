import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_edge import calibration as cal
from kalshi_edge.consistency import implied_normal
from kalshi_edge.costs import fill_price, taker_fee_total
from kalshi_edge.edge import analyze
from kalshi_edge.estimators.evidence import EvidenceEstimator
from kalshi_edge.estimators.parlay import ParlayEstimator
from kalshi_edge.estimators.weather import WeatherHighEstimator, bucket_probability, target_date
from kalshi_edge.external.weather import parse_open_meteo_ensemble
from kalshi_edge.ingestion.kalshi import normalize_market
from kalshi_edge.models import ContractSpec, Estimate, OrderbookLevel
from kalshi_edge.store import PredictionStore

NOW = datetime(2026, 10, 21, 20, 30, tzinfo=timezone.utc)


def mk(ticker="KXHIGHNY-26OCT22-B65.5", st="between", lo=65, hi=66, yb=0.52, ya=0.60,
       rules="Settles on CLINYC.", **kw):
    m = {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": "t", "yes_sub_title": "s",
         "strike_type": st, "floor_strike": lo, "cap_strike": hi, "rules_primary": rules,
         "yes_bid_dollars": yb, "yes_ask_dollars": ya, "no_bid_dollars": round(1 - ya, 4),
         "no_ask_dollars": round(1 - yb, 4), "volume_24h_fp": 2000, "open_interest_fp": 2000,
         "close_time": "2026-10-23T05:00:00Z", **kw}
    return normalize_market(m, "test", NOW)


FC = {"forecasts": [{"source": "NWS test", "high_f": 65, "issued_at": "2026-10-21T18:00:00Z", "weight": 2},
                    {"source": "MET test", "high_f": 65.5, "issued_at": "2026-10-21T18:00:00Z"}]}


# ---------------- costs ----------------
def test_fee_formula_rounds_up_to_cent():
    assert taker_fee_total(0.50, 1) == 0.02          # 1.75c -> 2c
    assert taker_fee_total(0.50, 100) == 1.75
    assert taker_fee_total(0.60, 10) == 0.17          # 16.8c -> 17c
    assert taker_fee_total(0.99, 1) == 0.01


def test_fill_walks_book_and_flags_depth():
    lv = [OrderbookLevel(0.40, 5), OrderbookLevel(0.42, 5)]
    avg, ok = fill_price(lv, 10)
    assert ok is True and avg == pytest.approx(0.41)
    avg, ok = fill_price(lv, 20)
    assert ok is False
    assert fill_price([OrderbookLevel(0.4, None)], 10) == (0.4, None)


def test_orderbook_no_bid_becomes_yes_ask():
    m = normalize_market({"ticker": "X-1", "event_ticker": "X"}, "t", NOW,
                         orderbook={"orderbook_fp": {"yes_dollars": [["0.30", "10"]],
                                                     "no_dollars": [["0.65", "7"]]}})
    assert m.yes_ask_levels[0].price == pytest.approx(0.35)
    assert m.no_ask_levels[0].price == pytest.approx(0.70)


def test_placeholder_asks_ignored():
    m = mk(yb=0.0, ya=0.0)
    assert m.yes_ask is None and m.mid is None


# ---------------- anti-anchoring ----------------
def test_contract_spec_has_no_price_fields():
    fields = set(ContractSpec.__dataclass_fields__)
    assert not fields & {"yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "volume", "open_interest"}


def test_estimate_independent_of_market_price():
    w = WeatherHighEstimator(use_live_sources=False)
    a = w.estimate(mk(yb=0.05, ya=0.07).contract_spec(), FC, NOW)
    b = w.estimate(mk(yb=0.90, ya=0.92).contract_spec(), FC, NOW)
    assert a.p_yes == b.p_yes and a.p_low == b.p_low


# ---------------- weather ----------------
def test_target_date():
    assert str(target_date("KXHIGHNY-26OCT22")) == "2026-10-22"


def test_buckets_partition_sums_to_one():
    specs = [mk(st="less", lo=None, hi=63).contract_spec()]
    specs += [mk(lo=a, hi=a + 1).contract_spec() for a in range(63, 71, 2)]
    specs += [mk(st="greater", lo=70, hi=None).contract_spec()]
    assert sum(bucket_probability(s, 65.3, 2.4) for s in specs) == pytest.approx(1.0, abs=1e-9)


def test_weather_insufficient_without_forecast_or_wrong_station():
    w = WeatherHighEstimator(use_live_sources=False)
    assert w.estimate(mk().contract_spec(), {}, NOW).insufficient
    assert w.estimate(mk(rules="Settles on CLIMIA.").contract_spec(), FC, NOW).insufficient


def test_weather_range_contains_central():
    e = WeatherHighEstimator(use_live_sources=False).estimate(mk().contract_spec(), FC, NOW)
    assert e.p_low <= e.p_yes <= e.p_high and e.params["lead_days"] == 1


def test_open_meteo_parse():
    data = {"daily": {"time": ["2026-10-22"], **{f"temperature_2m_max_member{i:02d}": [60 + i * 0.5]
                                                 for i in range(10)}}}
    ev, sd, n = parse_open_meteo_ensemble(data, target_date("KXHIGHNY-26OCT22"))
    assert n == 10 and ev.value == pytest.approx(62.25, abs=0.06) and sd > 0


def test_implied_normal_recovers_parameters():
    ms = [mk("E-26OCT22-T63", "less", None, 63)] + [mk(f"E-26OCT22-B{a}", "between", a, a + 1)
                                                    for a in range(63, 71, 2)] + [mk("E-26OCT22-T70", "greater", 70, None)]
    for m in ms:
        p = bucket_probability(m.contract_spec(), 66.0, 1.8)
        m.yes_bid, m.yes_ask = p - 0.001, p + 0.001
    mu, sig, _ = implied_normal(ms)
    assert abs(mu - 66.0) < 0.3 and abs(sig - 1.8) < 0.3


# ---------------- other estimators ----------------
def test_evidence_estimator_rules():
    e = EvidenceEstimator()
    spec = mk().contract_spec()
    assert e.estimate(spec, {}, NOW).insufficient
    r = e.estimate(spec, {"base_rate": {"successes": 30, "trials": 100, "source": "s"},
                          "adjustments": [{"log_odds": 2.0, "reason": "hunch"}]}, NOW)
    assert r.p_yes == pytest.approx(0.30, abs=0.01)          # unsourced adjustment ignored
    assert any("unsourced" in o for o in r.opposing)


def test_parlay_needs_every_leg():
    p = ParlayEstimator()
    spec = ContractSpec("KXMVEX-1", "KXMVEX", "KXMVEX", "yes A,yes B", "", "", "", None, None, None, None)
    assert p.estimate(spec, {"legs": {"yes A": {"p": 0.5, "source": "x"}}}, NOW).insufficient
    r = p.estimate(spec, {"legs": {"yes A": {"p": 0.5, "source": "x"}, "yes B": {"p": 0.6, "source": "y"}}}, NOW)
    assert r.p_yes == pytest.approx(0.30) and r.confidence == "LOW"


# ---------------- verdicts ----------------
def est(p, lo, hi, conf="MEDIUM"):
    return Estimate("t", p, lo, hi, conf)


def test_verdicts():
    m = mk(yb=0.49, ya=0.51)
    assert analyze(m, Estimate.insufficient_data("t", "x"), 10, 0, NOW).verdict == "INSUFFICIENT DATA"
    assert analyze(m, est(0.51, 0.45, 0.57), 10, 0, NOW).verdict == "MARKET APPEARS FAIR"
    assert analyze(m, est(0.58, 0.45, 0.70), 10, 0, NOW).verdict == "NO CLEAR EDGE"     # market inside range
    assert analyze(m, est(0.62, 0.53, 0.70), 10, 0, NOW).verdict == "POTENTIAL EDGE"
    assert analyze(m, est(0.62, 0.53, 0.70, "HIGH"), 10, 0, NOW).verdict == "STRONG POTENTIAL EDGE"
    assert analyze(m, est(0.62, 0.53, 0.70, "LOW"), 10, 0, NOW).verdict == "SMALL EDGE"
    # 25pp gap: capped at POTENTIAL even with HIGH confidence
    assert analyze(m, est(0.75, 0.70, 0.80, "HIGH"), 10, 0, NOW).verdict == "POTENTIAL EDGE"
    # pessimistic end loses money after fees -> SMALL EDGE
    assert analyze(m, est(0.56, 0.52, 0.60), 10, 0, NOW).verdict == "SMALL EDGE"


def test_no_side_selected_when_model_below_market():
    a = analyze(mk(yb=0.49, ya=0.51), est(0.38, 0.30, 0.45), 10, 0, NOW)
    assert a.best_side == "NO" and a.edge_pp == pytest.approx(-12)


# ---------------- calibration ----------------
def _sim(n, distort, seed=1):
    rnd = random.Random(seed)
    ps, ys = [], []
    for _ in range(n):
        true = rnd.uniform(0.05, 0.95)
        ys.append(1 if rnd.random() < true else 0)
        ps.append(distort(true))
    return ps, ys


def test_calibration_slope_detects_overconfidence():
    ps, ys = _sim(4000, lambda p: p)
    _, b = cal.calibration_slope(ps, ys)
    assert 0.85 < b < 1.15
    over = lambda p: 1 / (1 + math.exp(-2.2 * math.log(p / (1 - p))))
    ps, ys = _sim(4000, over)
    _, b = cal.calibration_slope(ps, ys)
    assert b < 0.6


def test_summary_detects_yes_bias(tmp_path):
    ps, ys = _sim(800, lambda p: min(0.99, p + 0.12))
    rows = [{"model_prob": p, "outcome": y, "market_prob": None, "verdict": "", "best_fill": None,
             "estimator": "x", "confidence": "LOW"} for p, y in zip(ps, ys)]
    assert cal.summarize(rows)["yes_bias_label"] == "biased toward YES"


def test_store_roundtrip(tmp_path):
    s = PredictionStore(str(tmp_path / "p.sqlite"))
    m = mk()
    a = analyze(m, est(0.40, 0.33, 0.47), 10, 0, NOW)
    s.log(a)
    s.log(a)
    assert s.unresolved_tickers() == [m.ticker]
    s.set_outcome(m.ticker, 0)
    assert len(s.resolved()) == 1          # latest prediction per ticker only
    assert s.resolved_count("t") == 1


def test_nws_gridpoint_parse(monkeypatch=None):
    from kalshi_edge.external import weather as wx
    pages = {
        "points": {"properties": {"forecastGridData": "https://api.weather.gov/gridpoints/OKX/33,37"}},
        "grid": {"properties": {"updateTime": "2026-10-21T18:02:00+00:00", "maxTemperature": {"values": [
            {"validTime": "2026-10-21T11:00:00+00:00/PT13H", "value": 16.1},
            {"validTime": "2026-10-22T11:00:00+00:00/PT13H", "value": 18.3}]}}}}
    orig = wx._get_json
    wx._get_json = lambda url, headers=None: pages["points" if "/points/" in url else "grid"]
    try:
        ev = wx.nws_forecast_high(wx.STATIONS["KXHIGHNY"], target_date("KXHIGHNY-26OCT22"))
    finally:
        wx._get_json = orig
    assert ev.value == pytest.approx(64.9, abs=0.05)
