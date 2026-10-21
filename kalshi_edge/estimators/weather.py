"""Daily high-temperature markets (Kalshi KXHIGH* series).

Model: the settled value (an integer °F from the NWS climate report) is treated
as round(T) where T ~ Normal(mu, sigma).
  mu    = weighted mean of the available point forecasts / ensemble mean
  sigma = forecast-error SD for the lead time, widened when sources disagree
          and blended with ensemble spread when an ensemble is available.

The lead-time error table below is a STARTING ASSUMPTION, not a fitted value.
The calibration module exists to check it once real outcomes are logged.
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..external import weather as wx
from ..models import ContractSpec, Estimate, Evidence, utcnow
from .base import Estimator, norm_cdf

# Assumed SD (°F) of a daily-high forecast error by lead time in days.
# Rough figures for US first-order stations; to be replaced by fitted values.
SIGMA_BY_LEAD = {0: 1.8, 1: 2.6, 2: 3.2, 3: 3.7, 4: 4.2}
SIGMA_FAR = 5.0
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _aware(s):
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def target_date(event_ticker: str) -> Optional[date]:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))


def bucket_probability(spec: ContractSpec, mu: float, sigma: float) -> Optional[float]:
    """P(YES) for an integer-valued settlement with continuous T ~ N(mu, sigma)."""
    st, lo, hi = spec.strike_type, spec.floor_strike, spec.cap_strike
    F = lambda x: norm_cdf((x - mu) / sigma)
    if st == "between" and lo is not None and hi is not None:
        return F(hi + 0.5) - F(lo - 0.5)
    if st == "greater" and lo is not None:            # value > floor
        return 1 - F(lo + 0.5)
    if st == "greater_or_equal" and lo is not None:   # value >= floor
        return 1 - F(lo - 0.5)
    if st == "less" and hi is not None:               # value < cap
        return F(hi - 0.5)
    if st == "less_or_equal" and hi is not None:      # value <= cap
        return F(hi + 0.5)
    return None


class WeatherHighEstimator(Estimator):
    name = "weather_high_v1"

    def __init__(self, use_live_sources: bool = True):
        self.use_live = use_live_sources

    def applies(self, spec: ContractSpec, evidence: dict) -> bool:
        return spec.series_ticker in wx.STATIONS

    def _gather(self, spec, evidence, station, tdate):
        """Returns list of (Evidence, weight) and optional ensemble sd."""
        points, ens_sd, notes = [], None, []
        mock = bool(evidence.get("_mock"))
        for f in evidence.get("forecasts", []):
            issued = f.get("issued_at")
            points.append((Evidence(
                source=f["source"], description=f"Forecast high {f['high_f']}°F",
                value=float(f["high_f"]),
                observed_at=_aware(issued),
                url=f.get("url"), is_mock=mock), float(f.get("weight", 1))))
            if f.get("ensemble_sd") is not None:
                ens_sd = float(f["ensemble_sd"])
        if self.use_live:
            try:
                ev = wx.nws_forecast_high(station, tdate)
                if ev:
                    points.append((ev, 2.0))
            except Exception as e:  # network or schema problem: record, don't guess
                notes.append(f"NWS fetch failed: {e.__class__.__name__}")
            try:
                r = wx.open_meteo_ensemble(station, tdate)
                if r:
                    ev, sd, _ = r
                    points.append((ev, 1.5))
                    ens_sd = sd
            except Exception as e:
                notes.append(f"Open-Meteo fetch failed: {e.__class__.__name__}")
        return points, ens_sd, notes

    def estimate(self, spec: ContractSpec, evidence: dict,
                 now: Optional[datetime] = None) -> Estimate:
        now = now or utcnow()
        station = wx.STATIONS[spec.series_ticker]
        tdate = target_date(spec.event_ticker)
        if tdate is None:
            return Estimate.insufficient_data(self.name, "Could not read the target date from the ticker.")
        rules = (spec.rules_primary or "") + " " + (spec.rules_secondary or "")
        if rules.strip() and station.climate_code not in rules:
            return Estimate.insufficient_data(
                self.name, f"Rules text does not mention {station.climate_code}; the settlement "
                           "station may differ from the one this model forecasts.")
        if bucket_probability(spec, 70, 3) is None:
            return Estimate.insufficient_data(self.name, f"Unsupported strike type '{spec.strike_type}'.")

        points, ens_sd, notes = self._gather(spec, evidence, station, tdate)
        evs = [e for e, _ in points]
        if not points:
            return Estimate.insufficient_data(
                self.name, "No forecast available for the target date. " + "; ".join(notes), evs)

        tz = ZoneInfo(station.tz)
        local_now = now.astimezone(tz)
        lead = (tdate - local_now.date()).days
        if lead < 0:
            return Estimate.insufficient_data(self.name, "Target date has passed; check the settled result.", evs)

        wsum = sum(w for _, w in points)
        mu = sum(e.value * w for e, w in points) / wsum
        vals = [e.value for e, _ in points]
        disagreement = (max(vals) - min(vals)) if len(vals) > 1 else 0.0
        src_sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0

        base = SIGMA_BY_LEAD.get(lead, SIGMA_FAR)
        sigma = math.sqrt(base ** 2 + src_sd ** 2)
        if ens_sd is not None:
            # Raw ensembles are usually under-dispersed; never let them shrink sigma
            # far below the climatological error for this lead time.
            sigma = max(0.8 * base, math.sqrt(ens_sd ** 2 + 1.0 ** 2), sigma * 0.9)

        p = bucket_probability(spec, mu, sigma)
        # Range: shift mu by +-1°F (station bias / unseen model runs) and sigma by
        # 0.75x-1.3x (uncertainty in the error table itself).
        grid = [bucket_probability(spec, mu + dm, sigma * k)
                for dm in (-1.0, 0.0, 1.0) for k in (0.75, 1.0, 1.3)]
        p_lo, p_hi = min(grid), max(grid)

        issued = [e.observed_at for e in evs if e.observed_at]
        age_h = (now - max(issued)).total_seconds() / 3600 if issued else None

        # ---- confidence ----
        independent = len({e.source.split(" ")[0] for e in evs})
        conf = "HIGH"
        reasons_down = []
        if independent < 2:
            conf = "MEDIUM"
            reasons_down.append("only one forecast source")
        if age_h is None or age_h > 12:
            conf = "MEDIUM" if conf == "HIGH" else conf
            reasons_down.append(f"newest forecast is {age_h:.0f}h old" if age_h else "forecast issue time unknown")
        if age_h is not None and age_h > 30:
            conf = "LOW"
        if disagreement > 4:
            conf = "LOW"
            reasons_down.append(f"sources disagree by {disagreement:.1f}°F")
        if lead >= 3:
            conf = "LOW"
            reasons_down.append(f"{lead}-day lead time")
        if any(e.is_mock for e in evs):
            conf = "LOW"
            reasons_down.append("MOCK forecast data")

        width = (spec.cap_strike or 0) - (spec.floor_strike or 0) + 1 if spec.strike_type == "between" else None
        supporting = [
            f"Weighted forecast high {mu:.1f}°F from {len(points)} source(s) "
            f"({', '.join(f'{e.value:g}' for e in evs)}).",
            f"Lead time {lead} day(s); assumed forecast error SD {sigma:.1f}°F.",
        ]
        if disagreement <= 2 and len(vals) > 1:
            supporting.append(f"Sources agree within {disagreement:.1f}°F.")
        opposing = []
        if width is not None and width <= 2:
            opposing.append(f"Bucket is only {width}°F wide; a 1-2°F forecast miss flips the outcome.")
        if age_h is not None and age_h > 6:
            opposing.append(f"Newest forecast is ~{age_h:.0f}h old; later model runs may have moved.")
        if lead == 0:
            opposing.append("Same-day market: traders can see live observations this model does not use.")
        opposing += [f"Data issue: {n}" for n in notes]

        return Estimate(
            estimator=self.name, p_yes=p, p_low=p_lo, p_high=p_hi, confidence=conf,
            evidence=evs, supporting=supporting, opposing=opposing,
            assumptions=[
                "Settlement value behaves like round(T) with T normally distributed around the forecast.",
                f"Forecast-error SD for a {lead}-day lead is ~{SIGMA_BY_LEAD.get(lead, SIGMA_FAR)}°F "
                "(uncalibrated default; check with the calibration report).",
                f"The forecast point represents the settlement station ({station.climate_code}).",
                "The climate-report day runs midnight-to-midnight local STANDARD time "
                "(1am-1am during daylight time); late-night temperatures can set the high.",
            ] + ([f"Confidence lowered: {'; '.join(reasons_down)}."] if reasons_down else []),
            would_change=[
                "A newer NWS forecast or model run (NBM/HRRR) moving the high by 1°F or more.",
                "Observed temperatures on the day (for same-day markets).",
                "Fronts or cloud/rain timing shifts that change the afternoon peak.",
            ],
            time_sensitive=lead <= 1,
            created_at=now,
            params={"mu": mu, "sigma": sigma, "lead_days": lead},
        )
