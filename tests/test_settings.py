from kalshi_edge.user_settings import DEFAULTS, parse


def test_settings_parse_and_guard():
    s = parse("""
    min_win_probability = 0.8   # note
    show_low_confidence = true
    include_rebounds = false
    sportsbook_weight = 3       # clamped to 1
    matchup_weight = oops       # ignored, keeps default
    unknown_key = 5
    """)
    assert s["min_win_probability"] == 0.8 and s["show_low_confidence"] is True
    assert s["include_rebounds"] is False and s["sportsbook_weight"] == 1.0
    assert s["matchup_weight"] == DEFAULTS["matchup_weight"] and "unknown_key" not in s


def test_shipped_settings_file_matches_defaults():
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "picks_settings.toml")
    assert parse(open(path).read()) == parse("")


def test_credit_guard():
    from datetime import datetime, timezone
    from kalshi_edge.credits import games_allowed, runs_left_this_month
    oct24 = datetime(2026, 10, 24, tzinfo=timezone.utc)
    assert runs_left_this_month(oct24, 2) == 16
    # plenty of credits: keep the requested 2 games
    assert games_allowed(476, 2, oct24, reserve=20, runs_per_day=2, per_game=3)[0] == 2
    # low credits late in the month: cut back
    oct30 = datetime(2026, 10, 30, tzinfo=timezone.utc)
    n, why = games_allowed(40, 2, oct30, reserve=20, runs_per_day=2, per_game=3)
    assert n == 1 and "credits left" in why
    assert games_allowed(15, 2, oct30, reserve=20, runs_per_day=2, per_game=3)[0] == 0
    assert games_allowed(None, 2, oct30, reserve=20, runs_per_day=2, per_game=3)[0] == 2


def test_record_cost(tmp_path, monkeypatch=None):
    import os
    from kalshi_edge import credits
    os.environ["KALSHI_EDGE_DB"] = str(tmp_path / "p.sqlite")
    try:
        assert credits.record_cost(500, "488", 2) == 6
        assert credits.load_usage()["per_game"] == 6
        credits.record_cost(488, "482", 2)                  # 3 per game -> smoothed
        assert credits.load_usage()["per_game"] == 4.8
        assert credits.record_cost(None, "480", 2) is None
    finally:
        del os.environ["KALSHI_EDGE_DB"]
