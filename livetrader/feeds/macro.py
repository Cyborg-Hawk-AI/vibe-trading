"""Macro tape: oil, VIX, dollar, rates, gold, natgas — via yfinance.

The engine and the LLM check-ins both consume this snapshot: the engine as a
regime signal (risk-on/off), the LLM as context for news assessment. Weather
enters through the news feed (NWS alerts + energy headlines); its market
expression — natgas/oil moves — is captured here.
"""

from __future__ import annotations

import logging
import threading
import time

from livetrader.config import FeedConfig
from livetrader.feeds.base import MacroSnapshot, MarketData

logger = logging.getLogger("livetrader.feeds.macro")

MACRO_TICKERS = {
    "CL=F": "wti_oil",
    "BZ=F": "brent_oil",
    "NG=F": "natgas",
    "GC=F": "gold",
    "^VIX": "vix",
    "^TNX": "us10y",
    "DX-Y.NYB": "dollar_idx",
    "^GSPC": "spx",
    "BTC-USD": "btc",
}


class MacroFeed(threading.Thread):
    def __init__(self, market: MarketData, cfg: FeedConfig, stop: threading.Event) -> None:
        super().__init__(name="macro-feed", daemon=True)
        self.market = market
        self.cfg = cfg
        self.stop_event = stop

    def poll_once(self) -> MacroSnapshot | None:
        try:
            import yfinance as yf

            df = yf.download(
                tickers=" ".join(MACRO_TICKERS), period="5d", interval="1d",
                group_by="ticker", threads=True, progress=False, auto_adjust=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("macro poll failed: %s", exc)
            return None
        snap = MacroSnapshot(ts=time.time())
        for ticker, name in MACRO_TICKERS.items():
            try:
                closes = df[ticker]["Close"].dropna()
                if len(closes) >= 2:
                    last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                    snap.values[name] = last
                    snap.day_change[name] = (last / prev - 1.0) if prev else 0.0
            except Exception:  # noqa: BLE001
                continue
        if snap.values:
            self.market.set_macro(snap)
            return snap
        return None

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.poll_once()
            self.stop_event.wait(self.cfg.macro_poll_s)
        logger.info("macro feed stopped")


def risk_regime(snap: MacroSnapshot) -> str:
    """Coarse risk regime from the macro tape: 'risk_on'|'risk_off'|'neutral'."""
    if not snap.values:
        return "neutral"
    score = 0.0
    vix = snap.values.get("vix", 0.0)
    if vix:
        score += -1.0 if vix > 25 else (0.5 if vix < 15 else 0.0)
    spx_chg = snap.day_change.get("spx", 0.0)
    score += 1.0 if spx_chg > 0.005 else (-1.0 if spx_chg < -0.005 else 0.0)
    btc_chg = snap.day_change.get("btc", 0.0)
    score += 0.5 if btc_chg > 0.01 else (-0.5 if btc_chg < -0.01 else 0.0)
    oil_chg = abs(snap.day_change.get("wti_oil", 0.0))
    if oil_chg > 0.04:  # violent oil moves = geopolitical stress either way
        score -= 0.5
    if score >= 1.0:
        return "risk_on"
    if score <= -1.0:
        return "risk_off"
    return "neutral"
