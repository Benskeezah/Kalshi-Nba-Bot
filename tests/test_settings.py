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
