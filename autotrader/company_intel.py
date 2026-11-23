"""Per-company research briefs for the attribution/committee deep-dives.

``get_company_brief(symbol, as_of_date, frame)`` aggregates everything the
LLM layer needs to reason about WHY a symbol moved:

  - price/volume statistics computed strictly from bars up to ``as_of_date``
    (point-in-time safe by construction);
  - anomaly events (volume/volatility spikes) that fired on the symbol;
  - news headlines filtered by publication timestamp <= as_of (+1 day for
    same-day coverage), same PIT gate as research_hooks;
  - a static profile (name/sector/industry/market cap) from yfinance.

The static profile reflects TODAY's metadata, not the historical snapshot —
that is unavoidable with free data and is flagged in the brief so the LLM
does not treat it as point-in-time.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from autotrader.research_hooks import _news_before, _yf_symbol

logger = logging.getLogger(__name__)


def _profile(symbol: str) -> dict[str, Any]:
    """Best-effort static company profile (network; degrades to {})."""
    try:
        import yfinance as yf

        info = yf.Ticker(_yf_symbol(symbol)).info or {}
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        logger.debug("profile fetch failed for %s: %s", symbol, exc)
        return {}
    keys = (
        "shortName", "sector", "industry", "marketCap", "floatShares",
        "shortRatio", "beta", "trailingPE", "profitMargins",
    )
    return {k: info[k] for k in keys if info.get(k) is not None}


def _price_stats(frame: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, Any]:
    """Causal price/volume stats from bars up to ``as_of`` only."""
    hist = frame.loc[:as_of]
    if len(hist) < 25:
        return {"bars": len(hist)}
    close = hist["close"].astype(float)
    vol = hist["volume"].astype(float)
    ret = close.pct_change()
    last = float(close.iloc[-1])
    out = {
        "bars": len(hist),
        "last_close": round(last, 4),
        "ret_5d": round(float(close.iloc[-1] / close.iloc[-6] - 1.0), 4) if len(close) > 6 else None,
        "ret_20d": round(float(close.iloc[-1] / close.iloc[-21] - 1.0), 4) if len(close) > 21 else None,
        "vol_20d_annualized": round(float(ret.iloc[-20:].std() * np.sqrt(252)), 4),
        "volume_vs_20d_median": round(
            float(vol.iloc[-1] / max(vol.iloc[-21:-1].median(), 1e-9)), 2
        ),
        "dist_from_20d_high": round(float(last / close.iloc[-21:].max() - 1.0), 4),
        "avg_dollar_volume_20d": round(float((close * vol).iloc[-20:].mean()), 0),
    }
    return out


def get_company_brief(
    symbol: str,
    as_of_date: str,
    frame: pd.DataFrame | None = None,
    *,
    include_news: bool = True,
    include_profile: bool = True,
    news_limit: int = 5,
) -> dict[str, Any]:
    """Aggregate a research brief for one symbol as of a historical date."""
    as_of = pd.Timestamp(as_of_date)
    brief: dict[str, Any] = {"symbol": symbol, "as_of": str(as_of.date())}
    if frame is not None and len(frame):
        brief["price_stats"] = _price_stats(frame, as_of)
    if include_news:
        brief["news"] = _news_before(symbol, as_of_date, limit=news_limit)
    if include_profile:
        profile = _profile(symbol)
        if profile:
            profile["_note"] = "profile is current-day metadata, not point-in-time"
            brief["profile"] = profile
    return brief
