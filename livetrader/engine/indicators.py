"""Causal indicators over live bar arrays, with a per-bar memo cache.

Everything is computed from completed bars only, backward-looking; the same
formulas as the autotrader's generated SignalEngine (Wilder RSI, EWM ATR,
rolling MA/high, close-to-close vol), so a genome behaves the same way in
the historical simulator and in the live arena.

The cache key includes the last bar timestamp: dozens of traders sharing
indicator parameters hit the same entry until a new bar lands, which keeps
tens of thousands of evaluations per tick cheap.
"""

from __future__ import annotations

import numpy as np

_CACHE: dict[tuple, tuple] = {}
_CACHE_MAX = 60_000


def _memo(key: tuple, fn):
    hit = _CACHE.get(key)
    if hit is not None:
        return hit[0]
    value = fn()
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (value,)
    return value


def rsi(close: np.ndarray, period: int, key: tuple = ()) -> float:
    """Wilder RSI of the last completed bar."""
    def compute() -> float:
        if len(close) < period + 2:
            return 50.0
        delta = np.diff(close)
        gain = np.clip(delta, 0.0, None)
        loss = np.clip(-delta, 0.0, None)
        alpha = 1.0 / period
        g = gain[0]
        l = loss[0]  # noqa: E741
        for i in range(1, len(gain)):
            g = alpha * gain[i] + (1 - alpha) * g
            l = alpha * loss[i] + (1 - alpha) * l  # noqa: E741
        if l <= 1e-12:
            return 100.0
        rs = g / l
        return float(100.0 - 100.0 / (1.0 + rs))
    return _memo(("rsi", period, *key), compute)


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        period: int, key: tuple = ()) -> float:
    def compute() -> float:
        n = len(close)
        if n < 3:
            return 0.0
        prev_close = close[:-1]
        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
        )
        alpha = 1.0 / period
        a = tr[0]
        for i in range(1, len(tr)):
            a = alpha * tr[i] + (1 - alpha) * a
        return float(a)
    return _memo(("atr", period, *key), compute)


def sma(close: np.ndarray, period: int, key: tuple = ()) -> float:
    def compute() -> float:
        if len(close) < period:
            return float(np.mean(close)) if len(close) else 0.0
        return float(np.mean(close[-period:]))
    return _memo(("sma", period, *key), compute)


def momentum(close: np.ndarray, lookback: int, key: tuple = ()) -> float:
    def compute() -> float:
        if len(close) <= lookback or close[-1 - lookback] <= 0:
            return 0.0
        return float(close[-1] / close[-1 - lookback] - 1.0)
    return _memo(("mom", lookback, *key), compute)


def prior_high(high: np.ndarray, lookback: int, key: tuple = ()) -> float:
    """Highest high of the lookback window EXCLUDING the current bar."""
    def compute() -> float:
        if len(high) < lookback + 1:
            return float("inf")
        return float(np.max(high[-1 - lookback:-1]))
    return _memo(("phigh", lookback, *key), compute)


def realized_vol(close: np.ndarray, lookback: int, bars_per_year: float,
                 key: tuple = ()) -> float:
    def compute() -> float:
        if len(close) < lookback + 1:
            return 0.0
        rets = np.diff(close[-lookback - 1:]) / close[-lookback - 1:-1]
        return float(np.std(rets) * np.sqrt(bars_per_year))
    return _memo(("rvol", lookback, *key), compute)


def momentum_skip(close: np.ndarray, lookback: int, skip: int, key: tuple = ()) -> float:
    """Momentum that ignores the most recent ``skip`` bars (12-1 style)."""
    def compute() -> float:
        skip_n = max(int(skip), 0)
        if len(close) <= lookback + skip_n:
            return 0.0
        end = -1 - skip_n if skip_n else -1
        start = end - lookback
        # Convert negative indexes carefully
        end_i = len(close) + end if end < 0 else end
        start_i = len(close) + start if start < 0 else start
        if start_i < 0 or close[start_i] <= 0:
            return 0.0
        return float(close[end_i] / close[start_i] - 1.0)
    return _memo(("momskip", lookback, skip, *key), compute)


def volume_ok(volume: np.ndarray, mult: float, lookback: int = 20, key: tuple = ()) -> bool:
    def compute() -> bool:
        if len(volume) < lookback:
            return True
        med = float(np.median(volume[-lookback:]))
        if med <= 0:
            return True
        return bool(volume[-1] >= med * mult)
    return _memo(("volok", mult, lookback, *key), compute)


def range_compressed(high: np.ndarray, low: np.ndarray, lookback: int,
                     key: tuple = ()) -> bool:
    def compute() -> bool:
        half = max(lookback // 2, 3)
        if len(high) < lookback:
            return False
        recent = float(np.max(high[-half:]) - np.min(low[-half:]))
        prior = float(np.max(high[-lookback:-half]) - np.min(low[-lookback:-half]))
        if prior <= 0:
            return False
        return recent < prior * 0.7
    return _memo(("compress", lookback, *key), compute)


def pullback_ok(close: np.ndarray, ma: float, key: tuple = ()) -> bool:
    if ma <= 0:
        return False
    return bool(ma * 0.97 <= close[-1] <= ma * 1.02)


def session_vwap(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    ts: np.ndarray,
    *,
    session_open_epoch: float | None = None,
) -> float | None:
    """Session VWAP from bars at/after today's RTH open (typical price x vol).

    Returns None when there is no usable session volume yet.
    """
    if len(close) < 2 or len(ts) != len(close):
        return None
    if session_open_epoch is None:
        # Approximate: last gap > 4h marks session open; else use all bars.
        gaps = np.where(np.diff(ts) > 4 * 3600)[0]
        start = int(gaps[-1] + 1) if len(gaps) else 0
    else:
        start = int(np.searchsorted(ts, session_open_epoch, side="left"))
    if start >= len(close):
        return None
    tp = (high[start:] + low[start:] + close[start:]) / 3.0
    vol = np.asarray(volume[start:], dtype=float)
    vol = np.where(np.isfinite(vol) & (vol > 0), vol, 0.0)
    den = float(vol.sum())
    if den <= 0:
        return None
    return float(np.sum(tp * vol) / den)


def opening_range(high: np.ndarray, low: np.ndarray, ts: np.ndarray,
                  orb_minutes: int = 5) -> tuple[float, float] | None:
    """High/low of the first ``orb_minutes`` of the current NY session.

    Returns None if the opening range is not yet complete.
    """
    if len(ts) < orb_minutes + 1:
        return None
    # Approximate: last orb_minutes bars after a gap in timestamps > 4h = session open
    gaps = np.where(np.diff(ts) > 4 * 3600)[0]
    start = int(gaps[-1] + 1) if len(gaps) else 0
    end = start + orb_minutes
    if end >= len(ts) - 1:
        return None  # ORB not complete / still forming
    return float(np.max(high[start:end])), float(np.min(low[start:end]))


def cache_size() -> int:
    return len(_CACHE)
