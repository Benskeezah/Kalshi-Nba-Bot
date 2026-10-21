"""Kalshi market-data ingestion (GET requests only).

Two interchangeable clients:
  KalshiClient        - live public REST API, no auth required for market data
  FixtureKalshiClient - reads saved JSON snapshots (offline tests / demos)

Both return normalized MarketSnapshot objects. The API has moved from integer
cent fields (yes_bid) to dollar strings (yes_bid_dollars) and fixed-point
counts (volume_fp); the normalizer accepts either.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterable, Optional

from ..config import settings
from ..models import MarketSnapshot, OrderbookLevel, utcnow


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _price(m: dict, name: str) -> Optional[float]:
    """Dollar price, preferring the *_dollars field, falling back to cents."""
    v = _num(m.get(f"{name}_dollars"))
    if v is not None:
        return v
    c = _num(m.get(name))
    return c / 100 if c is not None else None


def _count(m: dict, name: str) -> float:
    v = _num(m.get(f"{name}_fp"))
    if v is None:
        v = _num(m.get(name))
    return v or 0.0


def _ts(v) -> Optional[datetime]:
    if not v:
        return None
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)


def series_from_ticker(event_ticker: str) -> str:
    return (event_ticker or "").split("-")[0]


def normalize_market(m: dict, source: str, fetched_at: Optional[datetime] = None,
                     orderbook: Optional[dict] = None) -> MarketSnapshot:
    yes_bid, yes_ask = _price(m, "yes_bid"), _price(m, "yes_ask")
    no_bid, no_ask = _price(m, "no_bid"), _price(m, "no_ask")
    # A zero ask / one bid on Kalshi means "nothing resting on that side".
    # An ask of 0 or 1 is a placeholder, not a tradeable offer.
    if yes_ask is not None and not 0 < yes_ask < 1:
        yes_ask = None
    if no_ask is not None and not 0 < no_ask < 1:
        no_ask = None
    snap = MarketSnapshot(
        ticker=m["ticker"],
        event_ticker=m.get("event_ticker", ""),
        series_ticker=m.get("series_ticker") or series_from_ticker(m.get("event_ticker", "")),
        title=m.get("title", ""),
        yes_sub_title=m.get("yes_sub_title") or m.get("subtitle") or "",
        rules_primary=m.get("rules_primary", ""),
        rules_secondary=m.get("rules_secondary", ""),
        status=m.get("status", ""),
        close_time=_ts(m.get("close_time")),
        strike_type=m.get("strike_type"),
        floor_strike=_num(m.get("floor_strike")),
        cap_strike=_num(m.get("cap_strike")),
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        last_price=_price(m, "last_price"),
        previous_price=_price(m, "previous_price"),
        volume=_count(m, "volume"),
        volume_24h=_count(m, "volume_24h"),
        open_interest=_count(m, "open_interest"),
        fetched_at=fetched_at or utcnow(),
        source=source,
        result=(m.get("result") or None),
        legs=tuple((l.get("market_ticker"), (l.get("side") or "yes").lower())
                   for l in (m.get("mve_selected_legs") or []) if l.get("market_ticker")),
        raw=m,
    )
    if orderbook:
        apply_orderbook(snap, orderbook)
    else:
        # Without a book, assume only the top-of-book size is available.
        def size(name):
            if m.get(f"{name}_fp") is None and m.get(name) is None:
                return None          # depth unknown
            return _count(m, name)
        if snap.yes_ask is not None:
            snap.yes_ask_levels = [OrderbookLevel(snap.yes_ask, size("yes_ask_size"))]
        if snap.no_ask is not None:
            snap.no_ask_levels = [OrderbookLevel(snap.no_ask, size("no_ask_size"))]
    return snap


def apply_orderbook(snap: MarketSnapshot, ob: dict) -> None:
    """Kalshi books list BIDS only. A NO bid at p is a YES ask at 1-p."""
    book = ob.get("orderbook_fp") or ob.get("orderbook") or {}

    def levels(key_dollars: str, key_cents: str):
        out = []
        for row in book.get(key_dollars) or []:
            out.append((float(row[0]), float(row[1])))
        if not out:
            for row in book.get(key_cents) or []:
                out.append((float(row[0]) / 100, float(row[1])))
        return out

    yes_bids = levels("yes_dollars", "yes")
    no_bids = levels("no_dollars", "no")
    snap.yes_ask_levels = sorted(
        (OrderbookLevel(round(1 - p, 4), q) for p, q in no_bids), key=lambda l: l.price)
    snap.no_ask_levels = sorted(
        (OrderbookLevel(round(1 - p, 4), q) for p, q in yes_bids), key=lambda l: l.price)


class KalshiClient:
    """Live, read-only client for Kalshi public market data."""

    def __init__(self, base: Optional[str] = None, timeout: Optional[float] = None):
        s = settings()
        self.base = (base or s.kalshi_base).rstrip("/")
        self.timeout = timeout or s.http_timeout

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, method="GET",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def market(self, ticker: str, with_orderbook: bool = True) -> MarketSnapshot:
        m = self._get(f"/markets/{ticker}")["market"]
        ob = None
        if with_orderbook:
            try:
                ob = self._get(f"/markets/{ticker}/orderbook")
            except Exception:
                ob = None  # fall back to top-of-book sizes
        return normalize_market(m, "kalshi-api", orderbook=ob)

    def markets(self, *, event_ticker: Optional[str] = None,
                series_ticker: Optional[str] = None, status: str = "open",
                limit: int = 200, with_orderbook: bool = False) -> list[MarketSnapshot]:
        out, cursor = [], None
        while True:
            data = self._get("/markets", {"event_ticker": event_ticker,
                                          "series_ticker": series_ticker,
                                          "status": status, "limit": limit,
                                          "cursor": cursor})
            for m in data.get("markets", []):
                ob = None
                if with_orderbook:
                    try:
                        ob = self._get(f"/markets/{m['ticker']}/orderbook")
                    except Exception:
                        ob = None
                out.append(normalize_market(m, "kalshi-api", orderbook=ob))
            cursor = data.get("cursor")
            if not cursor or not data.get("markets"):
                break
        return out


class FixtureKalshiClient:
    """Reads snapshots saved as {"fetched_at": ..., "markets": [...]} JSON."""

    def __init__(self, paths: Iterable[str]):
        self._markets: dict[str, MarketSnapshot] = {}
        for p in paths:
            with open(p) as f:
                data = json.load(f)
            fetched = _ts(data.get("fetched_at"))
            books = data.get("orderbooks", {})
            for m in data["markets"]:
                self._markets[m["ticker"]] = normalize_market(
                    m, f"fixture:{os.path.basename(p)}", fetched, books.get(m["ticker"]))

    def market(self, ticker: str, with_orderbook: bool = True) -> MarketSnapshot:
        return self._markets[ticker]

    def markets(self, *, event_ticker: Optional[str] = None,
                series_ticker: Optional[str] = None, **_) -> list[MarketSnapshot]:
        return [m for m in self._markets.values()
                if (not event_ticker or m.event_ticker == event_ticker)
                and (not series_ticker or m.series_ticker == series_ticker)]
