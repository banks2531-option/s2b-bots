from bot.app.summary import summarize_trades


def test_summarize_trades():
    rows = [
        {"event": "OPEN", "pnl": ""},
        {"event": "CLOSE", "pnl": "85.0"},
        {"event": "CLOSE", "pnl": "-120.0"},
        {"event": "CLOSE", "pnl": "50.0"},
    ]
    s = summarize_trades(rows)
    assert s["closed"] == 3 and s["wins"] == 2
    assert s["win_rate"] == 66.7
    assert s["net"] == 15.0
    assert s["pf"] == 1.12          # gp 135 / gl 120


def test_summarize_empty():
    s = summarize_trades([{"event": "OPEN", "pnl": ""}])
    assert s["closed"] == 0 and s["net"] == 0 and s["pf"] == 0.0
