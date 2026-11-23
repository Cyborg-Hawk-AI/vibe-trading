"""Session tape recorder: persist the live 1m tape for replay learning.

Livetrader's feeds hold a rolling in-memory 1m tape per symbol that was
thrown away on restart. This module dumps it to
``livetrader/state/tape/YYYY-MM-DD/SYMBOL.parquet`` once per day (daily
report tick + shutdown), where ``autotrader.intraday_data.ingest_live_tape``
resamples it into the daytrade snapshot library.

Every real trading day the replay engine's session library grows by one —
past the ~60-day limit of free intraday history.
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

import pandas as pd

from livetrader.config import state_dir
from livetrader.feeds.base import MarketData

logger = logging.getLogger("livetrader.recorder")

NY = ZoneInfo("America/New_York")
MIN_BARS = 30


def dump_tape(market: MarketData, when: dt.datetime | None = None) -> int:
    """Write each symbol's current 1m tape to today's tape directory.

    Timestamps are stored tz-naive UTC; the ingester converts equities to
    ET sessions. Idempotent per day — re-dumps overwrite with newer bars
    (the ingester merges + dedupes anyway).
    """
    when = when or dt.datetime.now(tz=NY)
    out_dir = state_dir() / "tape" / f"{when:%Y-%m-%d}"
    saved = 0
    for symbol in market.symbols():
        try:
            arrays = market.arrays(symbol, tf_minutes=1, tail=2000)
            if arrays is None or len(arrays["close"]) < MIN_BARS:
                continue
            frame = pd.DataFrame(
                {
                    "open": arrays["open"],
                    "high": arrays["high"],
                    "low": arrays["low"],
                    "close": arrays["close"],
                    "volume": arrays["volume"],
                },
                index=pd.to_datetime(arrays["ts"], unit="s"),
            )
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            out_dir.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(out_dir / f"{symbol.replace('/', '_')}.parquet")
            saved += 1
        except Exception:  # noqa: BLE001 — recording must never break trading
            logger.exception("tape dump failed for %s", symbol)
    if saved:
        # A fresh dump means the day needs (re-)ingesting.
        marker = out_dir / ".ingested"
        marker.unlink(missing_ok=True)
        logger.info("session tape: %d symbols dumped to %s", saved, out_dir.name)
    return saved
