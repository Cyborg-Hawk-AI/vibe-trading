"""Diagnostics: round-trip parsing and failure tagging."""

import pandas as pd

from autotrader.diagnostics import diagnose, parse_round_trips, tag_trades, RoundTrip


def _write_trades_csv(tmp_path, rows):
    cols = ["timestamp", "code", "side", "price", "qty", "reason", "pnl",
            "holding_days", "return_pct"]
    path = tmp_path / "trades.csv"
    pd.DataFrame(rows, columns=cols).to_csv(path, index=False)
    return path


def test_parse_round_trips(tmp_path):
    rows = [
        ["2025-01-02", "AAPL.US", "buy", 100.0, 1.0, "signal", 0.0, 0, 0.0],
        ["2025-01-08", "AAPL.US", "sell", 95.0, 1.0, "signal", -5.0, 6, -5.0],
        ["2025-02-01", "MSFT.US", "buy", 200.0, 1.0, "signal", 0.0, 0, 0.0],
        ["2025-02-10", "MSFT.US", "sell", 210.0, 1.0, "signal", 10.0, 9, 5.0],
    ]
    trips = parse_round_trips(_write_trades_csv(tmp_path, rows))
    assert len(trips) == 2
    assert trips[0].pnl == -5.0 and trips[1].pnl == 10.0


def test_premature_stop_tag(data_map, tmp_path):
    frame = data_map["AAPL.US"]
    entry_ts = frame.index[100]
    exit_ts = frame.index[103]
    entry_price = float(frame["close"].iloc[100])
    # Exit below entry while the real series recovers above entry soon after:
    # force the frame so close[104..108] > entry_price.
    frames = {"AAPL.US": frame.copy()}
    frames["AAPL.US"].iloc[104:109, frames["AAPL.US"].columns.get_loc("close")] = entry_price * 1.05

    trips = [
        RoundTrip(
            code="AAPL.US",
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_price=entry_price,
            exit_price=entry_price * 0.97,
            qty=1.0,
            pnl=-3.0,
            return_pct=-3.0,
        )
    ]
    tag_trades(trips, frames)
    assert "premature_stop" in trips[0].tags


def test_diagnose_counts(data_map, tmp_path):
    rows = [
        ["2025-01-02", "AAPL.US", "buy", 100.0, 1.0, "signal", 0.0, 0, 0.0],
        ["2025-01-08", "AAPL.US", "sell", 90.0, 1.0, "signal", -10.0, 6, -10.0],
    ]
    report = diagnose(_write_trades_csv(tmp_path, rows), data_map)
    assert report.n_trades == 1
    assert report.n_losers == 1
    assert "oversized_loss" in report.failure_counts
