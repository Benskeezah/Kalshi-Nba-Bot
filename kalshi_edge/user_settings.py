"""Reads picks_settings.toml: the knobs a user can change without touching code.

Only simple `key = value` lines are used (numbers, true/false), so the file is
valid TOML but needs no TOML library. Unknown keys are ignored; missing keys
fall back to the defaults below. Path: KALSHI_EDGE_SETTINGS or picks_settings.toml.
"""
from __future__ import annotations

import os
from functools import lru_cache

DEFAULTS = {
    "min_win_probability": 0.70,
    "min_margin_points": 2.0,
    "show_low_confidence": False,
    "include_points": True,
    "include_rebounds": True,
    "include_assists": True,
    "sportsbook_weight": 0.5,
    "sportsbook_games_per_run": 2,
    "sportsbook_credit_reserve": 20,
    "runs_per_day": 2,
    "matchup_weight": 0.5,
    "home_court_percent": 1.5,
    "back_to_back_minutes_percent": 3.0,
    "short_night_chance_percent": 7.0,
    "value_max_price_cents": 70,
    "value_min_edge_points": 10,
    "value_max_spread_cents": 4,
}


def _parse_value(raw: str):
    v = raw.strip().strip('"').strip("'")
    if v.lower() in ("true", "yes", "on"):
        return True
    if v.lower() in ("false", "no", "off"):
        return False
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return v


def parse(text: str) -> dict:
    out = dict(DEFAULTS)
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k in DEFAULTS:
            val = _parse_value(v)
            if isinstance(DEFAULTS[k], bool) and not isinstance(val, bool):
                continue
            if isinstance(DEFAULTS[k], (int, float)) and not isinstance(DEFAULTS[k], bool):
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    continue
            out[k] = val
    # keep values in sensible ranges
    out["min_win_probability"] = min(0.99, max(0.5, float(out["min_win_probability"])))
    out["sportsbook_weight"] = min(1.0, max(0.0, float(out["sportsbook_weight"])))
    out["matchup_weight"] = min(1.0, max(0.0, float(out["matchup_weight"])))
    out["sportsbook_games_per_run"] = max(0, int(out["sportsbook_games_per_run"]))
    out["sportsbook_credit_reserve"] = max(0, int(out["sportsbook_credit_reserve"]))
    out["runs_per_day"] = max(1, int(out["runs_per_day"]))
    return out


@lru_cache(maxsize=1)
def load(path: str = "") -> dict:
    path = path or os.environ.get("KALSHI_EDGE_SETTINGS", "picks_settings.toml")
    try:
        with open(path) as f:
            return parse(f.read())
    except OSError:
        return dict(DEFAULTS)
