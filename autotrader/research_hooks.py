"""On-demand research invocation for anomaly events.

The simulator's deterministic anomaly detector flags volume/volatility spikes
per tick. Those events are batched here — OUTSIDE the hot loop — and enriched
with point-in-time-safe context:

  - yfinance news is fetched per symbol and filtered to items published
    BEFORE the event date (PIT-safe: only the publication timestamp gates
    inclusion, so no future information leaks into the event record).
  - An optional LLM pass summarizes what the market was reacting to.

Enrichment is advisory: it annotates the registry/committee context and never
feeds back into the deterministic inner loop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

import pandas as pd

from autotrader.config import LlmConfig
from autotrader.llm import chat
from autotrader.simulator import AnomalyEvent

logger = logging.getLogger(__name__)


def _yf_symbol(code: str) -> str:
    """Map loader symbols to yfinance tickers (AAPL.US -> AAPL, BTC-USDT -> BTC-USD)."""
    if code.upper().endswith(".US"):
        return code[:-3]
    if code.upper().endswith("-USDT"):
        return code[:-5] + "-USD"
    return code


def enrich_events(
    events: list[AnomalyEvent],
    *,
    max_symbols: int = 5,
    news_per_symbol: int = 5,
) -> list[dict[str, Any]]:
    """Attach PIT-filtered news headlines to the highest-magnitude events.

    Network access is required; failures degrade to the bare event record.
    """
    enriched: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for event in events:
        record: dict[str, Any] = asdict(event)
        if event.symbol not in seen_symbols and len(seen_symbols) < max_symbols:
            seen_symbols.add(event.symbol)
            record["news"] = _news_before(
                event.symbol, event.date, limit=news_per_symbol
            )
        enriched.append(record)
    return enriched


def _news_before(code: str, date: str, limit: int = 5) -> list[dict[str, str]]:
    try:
        import yfinance as yf

        ticker = yf.Ticker(_yf_symbol(code))
        items = ticker.news or []
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        logger.debug("news fetch failed for %s: %s", code, exc)
        return []
    cutoff = pd.Timestamp(date).tz_localize("UTC") + pd.Timedelta(days=1)
    out: list[dict[str, str]] = []
    for item in items:
        content = item.get("content", item)
        pub = content.get("pubDate") or content.get("providerPublishTime")
        try:
            ts = (
                pd.Timestamp(pub, unit="s", tz="UTC")
                if isinstance(pub, (int, float))
                else pd.Timestamp(pub)
            )
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
        except (TypeError, ValueError):
            continue
        # PIT gate: only items published before the event date (+1d buffer for
        # same-day coverage) are attached.
        if ts <= cutoff:
            out.append(
                {
                    "title": str(content.get("title", "")),
                    "published": str(ts.date()),
                }
            )
        if len(out) >= limit:
            break
    return out


def summarize_events(
    enriched: list[dict[str, Any]],
    llm: LlmConfig,
) -> str:
    """Optional LLM digest of the anomaly batch."""
    if not enriched:
        return ""
    reply = chat(
        llm,
        system=(
            "You are a market microstructure analyst. Summarize in <=5 "
            "sentences what these volume/volatility anomalies (with any "
            "attached headlines) suggest about regime or event risk that a "
            "systematic strategy should be aware of. No preamble."
        ),
        user=json.dumps(enriched[:40]),
        max_tokens=400,
    )
    return reply or ""
