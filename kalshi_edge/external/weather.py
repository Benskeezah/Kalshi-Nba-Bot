"""External weather data for Kalshi daily high-temperature markets.

Sources (all free, no API key):
  * NWS gridpoint forecast (api.weather.gov)          - official point forecast
  * Open-Meteo ensemble API (GFS + ECMWF ensembles)  - spread -> uncertainty
Plus forecasts typed into an evidence file (see external/evidence_file.py).

Every value comes back as an Evidence record with its source, issue time and
URL so it can be logged and audited later.
"""
from __future__ import annotations

import json
import statistics
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..config import settings
from ..models import Evidence

# Settlement stations. The estimator refuses to run unless the market's own
# rules text mentions the expected climate-report code, so a wrong row here
# produces INSUFFICIENT DATA rather than a wrong probability.
@dataclass(frozen=True)
class Station:
    name: str
    lat: float
    lon: float
    tz: str
    climate_code: str   # code that appears in the Kalshi rules text


STATIONS: dict[str, Station] = {
    "KXHIGHNY":   Station("New York (Central Park)", 40.7789, -73.9692, "America/New_York", "CLINYC"),
    "KXHIGHCHI":  Station("Chicago (Midway)", 41.7868, -87.7522, "America/Chicago", "CLIMDW"),
    "KXHIGHMIA":  Station("Miami (MIA)", 25.7906, -80.3164, "America/New_York", "CLIMIA"),
    "KXHIGHAUS":  Station("Austin (Bergstrom)", 30.1831, -97.6799, "America/Chicago", "CLIAUS"),
    "KXHIGHDEN":  Station("Denver (DEN)", 39.8466, -104.6562, "America/Denver", "CLIDEN"),
    "KXHIGHLAX":  Station("Los Angeles (LAX)", 33.9382, -118.3866, "America/Los_Angeles", "CLILAX"),
    "KXHIGHPHIL": Station("Philadelphia (PHL)", 39.8733, -75.2268, "America/New_York", "CLIPHL"),
}


def _get_json(url: str, headers: Optional[dict] = None) -> dict:
    s = settings()
    req = urllib.request.Request(url, headers={"Accept": "application/geo+json, application/json",
                                               "User-Agent": s.nws_user_agent, **(headers or {})})
    with urllib.request.urlopen(req, timeout=s.http_timeout) as r:
        return json.loads(r.read().decode())


def nws_forecast_high(st: Station, target: date) -> Optional[Evidence]:
    """Max temperature for the target local date from the NWS gridpoint data."""
    pts = _get_json(f"https://api.weather.gov/points/{st.lat},{st.lon}")
    grid_url = pts["properties"]["forecastGridData"]
    grid = _get_json(grid_url)["properties"]
    updated = datetime.fromisoformat(grid["updateTime"].replace("Z", "+00:00"))
    tz = ZoneInfo(st.tz)
    best = None
    for v in grid["maxTemperature"]["values"]:
        start = datetime.fromisoformat(v["validTime"].split("/")[0])
        if start.astimezone(tz).date() == target and v["value"] is not None:
            best = v["value"] * 9 / 5 + 32  # degC -> degF
    if best is None:
        return None
    return Evidence(source="NWS gridpoint forecast (api.weather.gov)",
                    description=f"Forecast high {best:.0f}°F for {target} at {st.name}",
                    value=round(best, 1), observed_at=updated, url=grid_url)


def open_meteo_ensemble(st: Station, target: date) -> Optional[tuple[Evidence, float, int]]:
    """Ensemble mean, standard deviation and member count for the daily max."""
    q = urllib.parse.urlencode({
        "latitude": st.lat, "longitude": st.lon, "daily": "temperature_2m_max",
        "models": "gfs_seamless,ecmwf_ifs025", "temperature_unit": "fahrenheit",
        "timezone": st.tz, "start_date": target.isoformat(), "end_date": target.isoformat()})
    url = f"https://ensemble-api.open-meteo.com/v1/ensemble?{q}"
    data = _get_json(url)
    return parse_open_meteo_ensemble(data, target, url)


def parse_open_meteo_ensemble(data: dict, target: date, url: str = ""):
    daily = data.get("daily", {})
    try:
        idx = daily["time"].index(target.isoformat())
    except (KeyError, ValueError):
        return None
    members = [vals[idx] for k, vals in daily.items()
               if k.startswith("temperature_2m_max") and vals[idx] is not None]
    if len(members) < 5:
        return None
    mean, sd = statistics.fmean(members), statistics.pstdev(members)
    ev = Evidence(source="Open-Meteo ensemble (GFS + ECMWF members)",
                  description=f"Ensemble mean {mean:.1f}°F, spread (sd) {sd:.1f}°F, {len(members)} members",
                  value=round(mean, 1), observed_at=datetime.now(timezone.utc), url=url)
    return ev, sd, len(members)
