"""Command line interface (read-only).

  python -m kalshi_edge analyze KXHIGHNY-26OCT22-B65.5 --evidence evidence.json
  python -m kalshi_edge scan --event KXHIGHNY-26OCT22 --html report.html
  python -m kalshi_edge resolve                    # pull settled results from Kalshi
  python -m kalshi_edge resolve TICKER yes         # or record one by hand
  python -m kalshi_edge calibration

Add --fixtures file.json to run offline from a saved market snapshot, and
--no-live to skip live weather downloads (evidence file only).
"""
from __future__ import annotations

import argparse
import os
import sys

from .calibration import format_report
from .config import READ_ONLY, settings
from .estimators.registry import EstimatorRegistry
from .external.evidence_file import EvidenceFile
from .ingestion.kalshi import FixtureKalshiClient, KalshiClient
from .pipeline import analyze_market, scan
from .report import html_report, text_report, text_table
from .external.nba_stats import StatsDB, stats_db_path
from .highprob import high_prob_picks, text_picks
from .store import PredictionStore

assert READ_ONLY, "This build is analysis-only."


def _client(args):
    return FixtureKalshiClient(args.fixtures) if args.fixtures else KalshiClient()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="kalshi_edge", description="Read-only Kalshi probability & edge analysis")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("analyze", "scan"):
        sp = sub.add_parser(name)
        if name == "analyze":
            sp.add_argument("tickers", nargs="+")
        else:
            sp.add_argument("--event")
            sp.add_argument("--series")
        sp.add_argument("--evidence", help="evidence JSON file")
        sp.add_argument("--fixtures", nargs="*", help="saved market snapshot JSON(s) instead of the live API")
        sp.add_argument("--no-live", action="store_true", help="do not download weather data")
        sp.add_argument("--size", type=int, default=None, help="contracts assumed for fee/slippage math")
        sp.add_argument("--html", help="write an HTML report here")
        sp.add_argument("--no-log", action="store_true", help="do not log predictions")
        sp.add_argument("--mode", choices=["edge", "high-prob"], default="edge",
                        help="high-prob: list the most likely winners that are still fairly priced")
        sp.add_argument("--min-prob", type=float, default=0.70, help="high-prob mode: minimum win probability")
        sp.add_argument("--allow-low", action="store_true", help="high-prob mode: include LOW-confidence picks")
    r = sub.add_parser("resolve")
    r.add_argument("ticker", nargs="?")
    r.add_argument("outcome", nargs="?", choices=["yes", "no"])
    r.add_argument("--fixtures", nargs="*")
    b = sub.add_parser("build-stats", help="download/refresh the NBA box-score database from ESPN")
    b.add_argument("--seasons", nargs="*", type=int, help="ESPN season years (2027 = 2026-27)")
    d = sub.add_parser("daily", help="today's full props analysis -> report, picks.json, phone alert")
    d.add_argument("--out", default="site", help="folder for index.html / picks.json")
    d.add_argument("--date", help="ET game date, YYYY-MM-DD (default: today)")
    d.add_argument("--min-prob", type=float, default=None, help="override picks_settings.toml")
    d.add_argument("--allow-low", action="store_true", default=None)
    d.add_argument("--no-stats-update", action="store_true")
    d.add_argument("--no-notify", action="store_true")
    d.add_argument("--evidence")
    d.add_argument("--fixtures", nargs="*")
    d.add_argument("--page-url", default=os.environ.get("REPORT_URL"), help="link opened from the notification")
    c = sub.add_parser("calibration")
    c.add_argument("--include-mock", action="store_true")
    args = p.parse_args(argv)

    store = PredictionStore(settings().db_path)

    if args.cmd in ("analyze", "scan"):
        client = _client(args)
        evidence = EvidenceFile(args.evidence) if args.evidence else EvidenceFile()
        stats = StatsDB() if os.path.exists(stats_db_path()) else None
        registry = EstimatorRegistry(use_live_sources=not args.no_live, evidence=evidence, stats_db=stats)
        log = None if args.no_log else store
        if args.cmd == "analyze":
            results = []
            for t in args.tickers:
                m = client.market(t)
                try:  # siblings let the event-level consistency check run
                    sib = client.markets(event_ticker=m.event_ticker) if m.event_ticker else None
                except Exception:
                    sib = None
                results.append(analyze_market(m, registry, evidence, log, args.size, siblings=sib))
        else:
            if not (args.event or args.series):
                p.error("scan needs --event or --series")
            mkts = client.markets(event_ticker=args.event, series_ticker=args.series)
            results = scan(mkts, registry, evidence, log, args.size)
            print(text_table(results), "\n")
        picks = None
        if args.mode == "high-prob":
            picks = high_prob_picks(results, args.min_prob, allow_low=args.allow_low)
            print(text_picks(picks, args.min_prob), "\n")
        else:
            for a in results:
                print(text_report(a), "\n" + "=" * 78 + "\n")
        if args.html:
            with open(args.html, "w") as f:
                f.write(html_report(results, picks=picks, min_prob=args.min_prob))
            print(f"HTML report written to {args.html}")
        return 0

    if args.cmd == "resolve":
        if args.ticker and args.outcome:
            n = store.set_outcome(args.ticker, 1 if args.outcome == "yes" else 0)
            print(f"Recorded {args.outcome.upper()} for {args.ticker} ({n} rows).")
            return 0
        client = _client(args)
        for t in store.unresolved_tickers():
            try:
                res = client.market(t, with_orderbook=False).result
            except Exception as e:
                print(f"{t}: could not fetch ({e.__class__.__name__})")
                continue
            if res in ("yes", "no"):
                store.set_outcome(t, 1 if res == "yes" else 0)
                print(f"{t}: settled {res.upper()}")
            else:
                print(f"{t}: not settled yet")
        return 0

    if args.cmd == "build-stats":
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        cur = now.year + 1 if now.month >= 8 else now.year
        db = StatsDB()
        n = db.build(args.seasons or [cur - 1, cur])
        print(f"Added {n} games to {db.path}. Latest game: {db.last_game_date()}")
        return 0

    if args.cmd == "daily":
        from datetime import date as _date
        from .daily import run_daily
        out = run_daily(
            _client(args), args.out, day=_date.fromisoformat(args.date) if args.date else None,
            stats=StatsDB(), evidence=EvidenceFile(args.evidence) if args.evidence else None,
            store=store, use_live=not args.fixtures, update_stats=not args.no_stats_update,
            min_prob=args.min_prob, allow_low=args.allow_low, page_url=args.page_url,
            send=not args.no_notify)
        print("\n".join(out["log"]))
        print(f"{len(out['picks'])} pick(s). Report: {os.path.join(args.out, 'index.html')}")
        for line in [p["label"] + f"  ({p['win_prob']:.0%})" for p in out["picks"]]:
            print("  " + line)
        return 0

    if args.cmd == "calibration":
        print(format_report(store.resolved(include_mock=args.include_mock)))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
