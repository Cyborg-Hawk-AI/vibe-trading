"""Real-time crypto feed: OKX public REST (no API key, 24/7, US-reachable).

Two loops in one thread:
  - every ``crypto_quote_poll_s``: ONE spot-tickers call returns bid/ask for
    every instrument on the exchange -> real-time quotes for the universe;
  - every ``crypto_bar_refresh_s``: 1m candles per pair (authoritative OHLCV,
    includes the still-forming bar).

Crypto is the sleeve that keeps the arena fully live overnight and on
weekends while US equities are closed. (Binance's API returns HTTP 451 from
US networks; OKX public endpoints do not, and OKX instIds already match the
repo's ``BTC-USDT`` symbol format.)
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request

from livetrader.config import FeedConfig
from livetrader.feeds.base import MarketData, Quote

logger = logging.getLogger("livetrader.feeds.crypto")

OKX = "https://www.okx.com"

DEFAULT_PAIRS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "LTC-USDT",
    "DOT-USDT", "SUI-USDT", "APT-USDT", "ARB-USDT", "OP-USDT",
    "NEAR-USDT", "ATOM-USDT", "FIL-USDT", "UNI-USDT", "AAVE-USDT",
    "INJ-USDT", "PEPE-USDT", "SHIB-USDT", "WLD-USDT", "TRX-USDT",
    "ETC-USDT", "BCH-USDT", "HBAR-USDT", "RENDER-USDT", "TON-USDT",
]


def _get(path: str, timeout: float = 15.0):
    req = urllib.request.Request(
        OKX + path, headers={"User-Agent": "livetrader/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("code") not in ("0", 0):
        raise RuntimeError(f"okx error {body.get('code')}: {body.get('msg')}")
    return body.get("data", [])


def _to_bars(rows: list[list[str]]) -> list[tuple]:
    """OKX candles arrive newest-first: [ts_ms, o, h, l, c, vol, ...]."""
    bars = [
        (float(r[0]) / 1000.0, float(r[1]), float(r[2]), float(r[3]),
         float(r[4]), float(r[5]))
        for r in rows
    ]
    bars.sort(key=lambda b: b[0])
    return bars


class CryptoFeed(threading.Thread):
    def __init__(
        self,
        market: MarketData,
        cfg: FeedConfig,
        stop: threading.Event,
        pairs: list[str] | None = None,
    ) -> None:
        super().__init__(name="crypto-feed", daemon=True)
        self.market = market
        self.cfg = cfg
        self.stop_event = stop
        self.pairs = (pairs or DEFAULT_PAIRS)[: cfg.crypto_universe_size]
        self.healthy = False
        self.last_ok = 0.0

    # -- one-shot helpers ----------------------------------------------------
    def _backfill(self, pair: str, want: int) -> list[tuple]:
        """Newest 300 bars + paginated history until ``want`` 1m bars."""
        bars = _to_bars(_get(f"/api/v5/market/candles?instId={pair}&bar=1m&limit=300"))
        oldest = bars[0][0] if bars else time.time()
        while len(bars) < want and not self.stop_event.is_set():
            older = _to_bars(_get(
                f"/api/v5/market/history-candles?instId={pair}&bar=1m"
                f"&after={int(oldest * 1000)}&limit=300"
            ))
            if not older:
                break
            bars = older + bars
            oldest = bars[0][0]
        return bars[-want:]

    def warmup(self) -> int:
        """Backfill 1m history so indicators are live from the first minute."""
        loaded = 0
        for pair in self.pairs:
            self.market.register(pair, "crypto")
            try:
                bars = self._backfill(pair, self.cfg.crypto_warmup_bars)
                self.market.push_bars(pair, bars)
                loaded += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("warmup %s failed: %s", pair, exc)
            if self.stop_event.is_set():
                break
        logger.info("crypto warmup: %d/%d pairs backfilled", loaded, len(self.pairs))
        return loaded

    def _poll_quotes(self) -> None:
        data = _get("/api/v5/market/tickers?instType=SPOT")
        now = time.time()
        wanted = set(self.pairs)
        hits = 0
        for row in data:
            inst = row.get("instId", "")
            if inst not in wanted:
                continue
            try:
                bid = float(row["bidPx"])
                ask = float(row["askPx"])
                last = float(row.get("last") or 0.0) or (bid + ask) / 2
            except (KeyError, TypeError, ValueError):
                continue
            if bid <= 0 or ask <= 0:
                continue
            self.market.push_quote(inst, Quote(bid=bid, ask=ask, last=last, ts=now))
            hits += 1
        if hits:
            self.healthy = True
            self.last_ok = now

    def _refresh_bars(self) -> None:
        for pair in self.pairs:
            if self.stop_event.is_set():
                return
            try:
                rows = _get(f"/api/v5/market/candles?instId={pair}&bar=1m&limit=5")
                self.market.push_bars(pair, _to_bars(rows))
            except Exception as exc:  # noqa: BLE001
                logger.debug("candles %s failed: %s", pair, exc)

    # -- thread loop -----------------------------------------------------------
    def run(self) -> None:
        self.warmup()
        next_bars = 0.0
        while not self.stop_event.is_set():
            start = time.time()
            try:
                self._poll_quotes()
            except Exception as exc:  # noqa: BLE001
                self.healthy = False
                logger.warning("quote poll failed: %s", exc)
            if start >= next_bars:
                self._refresh_bars()
                next_bars = start + self.cfg.crypto_bar_refresh_s
            elapsed = time.time() - start
            self.stop_event.wait(max(self.cfg.crypto_quote_poll_s - elapsed, 0.5))
        logger.info("crypto feed stopped")
