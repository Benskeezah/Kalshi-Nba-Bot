"""Keeps The Odds API inside its monthly credits so a run never fails mid-month.

Before each run: ask the API how many credits are left (a free request), then
allow only as many games as the remaining credits can cover for every run left
this month, after holding back a small reserve. After each run: record what one
game actually cost, so the next run's estimate is based on real usage.
"""
from __future__ import annotations

import calendar
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from .config import settings

DEFAULT_PER_GAME = 3.0      # player points + rebounds + assists, one region


def usage_path() -> str:
    db = os.environ.get("KALSHI_EDGE_DB", "predictions.sqlite")
    return os.path.join(os.path.dirname(db) or ".", "odds_usage.json")


def load_usage() -> dict:
    try:
        with open(usage_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_usage(d: dict) -> None:
    try:
        with open(usage_path(), "w") as f:
            json.dump(d, f, indent=1)
    except OSError:
        pass


def credits_remaining(key: str) -> Optional[int]:
    """The /sports endpoint does not use credits but reports the balance in its headers."""
    url = "https://api.the-odds-api.com/v4/sports?" + urllib.parse.urlencode({"apiKey": key})
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "kalshi-edge-research"})
    try:
        with urllib.request.urlopen(req, timeout=settings().http_timeout) as r:
            v = r.headers.get("x-requests-remaining")
            return int(float(v)) if v is not None else None
    except Exception:
        return None


def runs_left_this_month(now: datetime, runs_per_day: int) -> int:
    days = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    return max(1, days * runs_per_day)


def games_allowed(remaining: Optional[int], wanted: int, now: datetime, *, reserve: int,
                  runs_per_day: int, per_game: float) -> tuple[int, str]:
    if remaining is None:
        return wanted, "credit balance unknown"
    spendable = remaining - reserve
    if spendable <= 0:
        return 0, f"only {remaining} credits left (keeping {reserve} in reserve)"
    per_run = spendable / runs_left_this_month(now, runs_per_day)
    allowed = min(wanted, int(per_run // max(per_game, 0.5)))
    if allowed < wanted:
        return allowed, (f"{remaining} credits left for {runs_left_this_month(now, runs_per_day)} runs "
                         f"this month at ~{per_game:.0f} per game")
    return allowed, ""


def record_cost(before: Optional[int], after: Optional[str], games: int) -> Optional[float]:
    """Update the measured credits-per-game (smoothed). Returns this run's per-game cost."""
    try:
        after_i = int(float(after)) if after is not None else None
    except ValueError:
        after_i = None
    if before is None or after_i is None or games <= 0 or after_i > before:
        return None
    cost = (before - after_i) / games
    u = load_usage()
    prev = u.get("per_game")
    u["per_game"] = round(cost if prev is None else 0.6 * prev + 0.4 * cost, 2)
    u["last_run"] = {"games": games, "credits_used": before - after_i, "credits_left": after_i}
    save_usage(u)
    return cost
