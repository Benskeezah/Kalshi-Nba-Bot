"""The daily job: everything needed for "give me today's best props", in one command.

    python -m kalshi_edge daily --out site

Steps
  1. Refresh the NBA stats database with any new box scores.
  2. Settle yesterday's logged predictions from Kalshi results (feeds calibration).
  3. Find today's open Kalshi prop markets (points / rebounds / assists).
  4. Estimate every rung with the stats model (no paid data used).
  5. Spend sportsbook-line lookups only on the games holding the best candidates
     (ODDS_API_GAMES_PER_RUN, default 3) and re-estimate those games.
  6. Pick the high-probability, fairly-priced sides; re-check their order books.
  7. Write site/index.html (+ a dated copy and picks.json), log every prediction,
     and send a phone notification if NTFY_TOPIC is set.

Read-only: nothing here can place an order.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .calibration import format_report, pattern_html, summarize
from .config import settings
from .edge import analyze
from .estimators.nba_props import PROP_SERIES, parse_prop_ticker
from .estimators.registry import EstimatorRegistry
from .external.evidence_file import EvidenceFile
from .external.nba_stats import StatsDB
from .highprob import Pick, high_prob_picks
from .models import Analysis
from .pipeline import rank_score, scan
from .report import html_report
from .store import PredictionStore
from .user_settings import load as load_settings
from .value import value_html, value_picks

ET = ZoneInfo("America/New_York")


def today_et(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(ET).date()


def todays_prop_markets(client, day: date) -> list:
    out = []
    for series in PROP_SERIES:
        try:
            for m in client.markets(series_ticker=series, status="open"):
                t = parse_prop_ticker(m.ticker)
                if t and t["date"] == day:
                    out.append(m)
        except Exception as e:
            print(f"  could not list {series}: {e.__class__.__name__}")
    return out


def _game_key(a: Analysis) -> Optional[frozenset]:
    t = parse_prop_ticker(a.market.ticker)
    return frozenset((t["home"], t["away"])) if t else None


def pick_lines(picks: list[Pick], limit: int = 8) -> list[str]:
    return [f"{i}. {p.label} — {p.win_p:.0%} to win, {p.price * 100:.0f}¢ (break-even {p.breakeven:.0%})"
            for i, p in enumerate(picks[:limit], 1)]


def notify(title: str, body: str, click: Optional[str] = None) -> bool:
    """Push to the free ntfy app (ntfy.sh). The topic name works like a password."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Tags": "basketball"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"{server}/{topic}", data=body.encode(), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print(f"  notification failed: {e.__class__.__name__}")
        return False


def run_daily(client, out_dir: str, *, day: Optional[date] = None, now: Optional[datetime] = None,
              stats: Optional[StatsDB] = None, evidence: Optional[EvidenceFile] = None,
              store: Optional[PredictionStore] = None, use_live: bool = True,
              update_stats: bool = True, min_prob: Optional[float] = None, allow_low: Optional[bool] = None,
              stats_mock: bool = False, page_url: Optional[str] = None, send: bool = True) -> dict:
    now = now or datetime.now(timezone.utc)
    day = day or today_et(now)
    cfg = load_settings()
    min_prob = cfg["min_win_probability"] if min_prob is None else min_prob
    allow_low = cfg["show_low_confidence"] if allow_low is None else allow_low
    min_margin = cfg["min_margin_points"] / 100
    wanted = {s for s, k in (("KXNBAPTS", "include_points"), ("KXNBAREB", "include_rebounds"),
                             ("KXNBAAST", "include_assists")) if cfg[k]}
    evidence = evidence or EvidenceFile()
    size = settings().default_size
    os.makedirs(out_dir, exist_ok=True)
    log = []

    # 1. stats
    if stats is not None and update_stats:
        cur = now.year + 1 if now.month >= 8 else now.year
        added = stats.build([cur - 1, cur], log=lambda m: print("  " + m, flush=True))
        log.append(f"Stats: +{added} games (latest {stats.last_game_date()}).")

    # 2. settle yesterday
    if store is not None:
        settled = 0
        for t in store.unresolved_tickers():
            try:
                res = client.market(t, with_orderbook=False).result
            except Exception:
                continue
            if res in ("yes", "no"):
                store.set_outcome(t, 1 if res == "yes" else 0)
                settled += 1
        log.append(f"Settled {settled} earlier predictions.")

    # 3-4. today's markets, stats model only (no paid lookups yet)
    markets = [m for m in todays_prop_markets(client, day) if m.series_ticker in wanted]
    log.append(f"{len(markets)} open prop markets for {day}.")
    reg1 = EstimatorRegistry(use_live, evidence, stats, stats_mock)
    reg1.props.odds_games = set()
    first = scan(markets, reg1, evidence, None, size, now)

    # 5. budgeted sportsbook lookups for the most promising games
    budget = int(os.environ.get("ODDS_API_GAMES_PER_RUN") or cfg["sportsbook_games_per_run"])
    cands = high_prob_picks(first, max(0.5, min_prob - 0.08), min_margin=-0.05, allow_low=True)
    games: list = []
    for p in cands:
        g = _game_key(p.analysis)
        if g and g not in games:
            games.append(g)
    games = games[:budget]
    reg2 = EstimatorRegistry(use_live, evidence, stats, stats_mock)
    reg2.props.odds_games = set(games)
    if games and os.environ.get("ODDS_API_KEY"):
        in_games = [m for m in markets if _game_key_m(m) in games]
        rescored = {a.market.ticker: a for a in scan(in_games, reg2, evidence, None, size, now)}
        results = [rescored.get(a.market.ticker, a) for a in first]
        log.append(f"Sportsbook lines checked for {len(games)} game(s); "
                   f"Odds API credits left: {reg2.props.quota_remaining or 'unknown'}.")
    else:
        results = first
        log.append("No sportsbook lines used (ODDS_API_KEY not set or no candidates).")

    # 6. picks, then confirm executable depth for them
    picks = high_prob_picks(results, min_prob, min_margin, allow_low=allow_low)
    if picks and use_live:
        refreshed = {}
        for p in picks[:15]:
            try:
                m = client.market(p.analysis.market.ticker, with_orderbook=True)
                a = analyze(m, p.analysis.estimate, size, 0, now)
                a.rank_score = rank_score(a, now)
                refreshed[m.ticker] = a
            except Exception:
                pass
        results = [refreshed.get(a.market.ticker, a) for a in results]
        picks = high_prob_picks(results, min_prob, min_margin, allow_low=allow_low)
    if store is not None:
        for a in results:
            store.log(a)

    # 7. outputs
    cal = format_report(store.resolved()) if store is not None else "No prediction log."
    cal_n = summarize(store.resolved())["n"] if store is not None else 0
    vpicks = value_picks(results)
    log.append(f"Value watchlist: {len(vpicks)} practice pick(s).")
    detail = ({p.analysis.market.ticker for p in picks} | {v.analysis.market.ticker for v in vpicks[:25]}
              | {a.market.ticker for a in results[:25]})
    any_mock = stats_mock or any(x.is_mock for a in results for x in a.estimate.evidence)
    note = (f"Daily run for {day.strftime('%a %b %d, %Y')} · generated {now.strftime('%Y-%m-%d %H:%M UTC')}. "
            f"Settings: picks need {min_prob:.0%}+ to win and {min_margin * 100:g}+ points over break-even"
            f"{'; low-confidence shown' if allow_low else ''}. "
            + " ".join(log)
            + ("" if cal_n >= 50 else f" Only {cal_n} settled predictions so far, so the model's win "
               "probabilities are not yet verified. Treat picks as research, not certainties."))
    patterns = pattern_html(store.resolved()) if store is not None else ""
    footer = (f"{patterns}<section class='sheet'><h2>Track record</h2><pre class='mono' "
              f"style='white-space:pre-wrap'>{cal}</pre></section>")
    vsec = value_html(vpicks, store.resolved() if store is not None else [])
    page = html_report(results, f"NBA Props {day.strftime('%b %d')}", note=note, picks=picks,
                       min_prob=min_prob, detail_tickers=detail, footer=footer, value_section=vsec)
    for name in ("index.html", f"{day.isoformat()}.html"):
        with open(os.path.join(out_dir, name), "w") as f:
            f.write(page)
    payload = {"date": day.isoformat(), "generated_at": now.isoformat(), "mock": any_mock, "log": log,
               "picks": [{"label": p.label, "ticker": p.analysis.market.ticker, "side": p.side,
                          "win_prob": round(p.win_p, 4), "win_prob_low": round(p.win_low, 4),
                          "price": p.price, "breakeven": round(p.breakeven, 4),
                          "confidence": p.analysis.estimate.confidence} for p in picks],
               "value_watchlist": [{"label": v.label, "ticker": v.analysis.market.ticker, "side": v.side,
                                    "price": v.price, "win_prob": round(v.win_p, 4), "edge": round(v.edge, 4),
                                    "sportsbooks": v.book} for v in vpicks]}
    with open(os.path.join(out_dir, "picks.json"), "w") as f:
        json.dump(payload, f, indent=1)

    if send:
        title = f"{len(picks)} NBA prop pick(s) for {day.strftime('%b %d')}" if picks else \
            f"No qualifying NBA props for {day.strftime('%b %d')}"
        body = "\n".join(pick_lines(picks)) or "Nothing beat its price with enough margin today."
        if any_mock:
            title = "[MOCK] " + title
        notify(title, body, page_url)
    payload["results"] = results
    payload["picks_obj"] = picks
    return payload


def _game_key_m(m) -> Optional[frozenset]:
    t = parse_prop_ticker(m.ticker)
    return frozenset((t["home"], t["away"])) if t else None
