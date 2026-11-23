"""Shared market-data store for all live feeds.

One ``MarketData`` instance is shared by the feed threads (writers) and the
trading engine (reader). All access goes through a single lock; feeds update
at second-to-minute cadence so contention is negligible.

Point-in-time honesty: the store only ever contains data that has already
been published. There is no history rewrite — bars append, quotes replace.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Quote:
    bid: float
    ask: float
    last: float
    ts: float

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread_bps(self) -> float:
        m = self.mid
        if m <= 0 or self.bid <= 0 or self.ask <= 0:
            return 0.0
        return (self.ask - self.bid) / m * 1e4


class Tape:
    """Rolling 1-minute OHLCV history for one symbol."""

    __slots__ = ("bars", "maxlen")

    def __init__(self, maxlen: int = 1600) -> None:
        self.maxlen = maxlen
        # each bar: (epoch_s, open, high, low, close, volume)
        self.bars: deque[tuple[float, float, float, float, float, float]] = deque(
            maxlen=maxlen
        )

    def merge(self, bars: list[tuple[float, float, float, float, float, float]]) -> int:
        """Merge bars (sorted by ts). Replaces the still-forming last bar,
        appends anything newer. Returns count of new/updated bars."""
        if not bars:
            return 0
        last_ts = self.bars[-1][0] if self.bars else -1.0
        updated = 0
        for bar in bars:
            if bar[0] > last_ts:
                self.bars.append(bar)
                last_ts = bar[0]
                updated += 1
            elif bar[0] == last_ts and self.bars:
                self.bars[-1] = bar
                updated += 1
        return updated

    def last_ts(self) -> float:
        return self.bars[-1][0] if self.bars else 0.0

    def arrays(self, tf_minutes: int = 1, tail: int = 400) -> dict[str, np.ndarray] | None:
        """OHLCV numpy arrays resampled to ``tf_minutes``, last ``tail`` bars.

        Resampling buckets by epoch // (tf*60) using only completed 1m bars,
        so the final resampled bar may still be forming — the engine treats
        the last bar as 'current' for stops and the rest as history.
        """
        n_1m = min(len(self.bars), tail * tf_minutes + tf_minutes)
        if n_1m < 2:
            return None
        raw = list(self.bars)[-n_1m:]
        ts = np.array([b[0] for b in raw])
        o = np.array([b[1] for b in raw])
        h = np.array([b[2] for b in raw])
        l = np.array([b[3] for b in raw])  # noqa: E741
        c = np.array([b[4] for b in raw])
        v = np.array([b[5] for b in raw])
        if tf_minutes <= 1:
            return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}
        bucket = (ts // (tf_minutes * 60)).astype(np.int64)
        # Group consecutive equal buckets
        change = np.flatnonzero(np.diff(bucket)) + 1
        starts = np.concatenate(([0], change))
        ends = np.concatenate((change, [len(bucket)]))
        return {
            "ts": ts[starts],
            "open": o[starts],
            "high": np.array([h[s:e].max() for s, e in zip(starts, ends)]),
            "low": np.array([l[s:e].min() for s, e in zip(starts, ends)]),
            "close": c[ends - 1],
            "volume": np.array([v[s:e].sum() for s, e in zip(starts, ends)]),
        }


@dataclass
class MacroSnapshot:
    """Latest macro tape: oil, vol, dollar, rates, gold + day changes."""

    values: dict[str, float] = field(default_factory=dict)      # last price
    day_change: dict[str, float] = field(default_factory=dict)  # pct vs prev close
    ts: float = 0.0

    def describe(self) -> str:
        if not self.values:
            return "macro tape unavailable"
        parts = []
        for key, value in sorted(self.values.items()):
            chg = self.day_change.get(key, 0.0)
            parts.append(f"{key}={value:.2f} ({chg:+.2%})")
        return ", ".join(parts)


class MarketData:
    """Thread-safe shared store: tapes + quotes + macro."""

    def __init__(self, max_bar_history: int = 1600) -> None:
        self._lock = threading.RLock()
        self._tapes: dict[str, Tape] = {}
        self._quotes: dict[str, Quote] = {}
        self._max_bar_history = max_bar_history
        self.macro = MacroSnapshot()
        self._universe_kind: dict[str, str] = {}  # symbol -> "crypto"|"equity"|"penny"
        self._hot_meta: dict[str, dict] = {}      # symbol -> hot-list scan metadata

    # -- writers -----------------------------------------------------------
    def register(self, symbol: str, kind: str) -> None:
        with self._lock:
            self._universe_kind[symbol] = kind
            self._tapes.setdefault(symbol, Tape(self._max_bar_history))

    def unregister(self, symbol: str) -> None:
        with self._lock:
            self._universe_kind.pop(symbol, None)
            self._tapes.pop(symbol, None)
            self._quotes.pop(symbol, None)

    def push_bars(self, symbol: str, bars: list[tuple]) -> int:
        with self._lock:
            tape = self._tapes.setdefault(symbol, Tape(self._max_bar_history))
            return tape.merge(bars)

    def push_quote(self, symbol: str, quote: Quote) -> None:
        with self._lock:
            self._quotes[symbol] = quote

    def set_macro(self, snap: MacroSnapshot) -> None:
        with self._lock:
            self.macro = snap

    def set_hot_meta(self, meta: dict[str, dict]) -> None:
        """Replace the hot-list scan metadata (scanner writes, traders read)."""
        with self._lock:
            self._hot_meta = {s: dict(m) for s, m in meta.items()}

    def hot_meta(self, symbol: str) -> dict:
        with self._lock:
            return dict(self._hot_meta.get(symbol, {}))

    # -- readers -----------------------------------------------------------
    def symbols(self, kind: str | None = None) -> list[str]:
        with self._lock:
            if kind is None:
                return sorted(self._universe_kind)
            return sorted(s for s, k in self._universe_kind.items() if k == kind)

    def kind(self, symbol: str) -> str:
        with self._lock:
            return self._universe_kind.get(symbol, "equity")

    def quote(self, symbol: str) -> Quote | None:
        with self._lock:
            q = self._quotes.get(symbol)
        if q is None:
            return None
        return q

    def price(self, symbol: str) -> float | None:
        """Best current price: quote if fresh, else last bar close."""
        with self._lock:
            q = self._quotes.get(symbol)
            if q is not None and time.time() - q.ts < 300:
                return q.mid
            tape = self._tapes.get(symbol)
            if tape and tape.bars:
                return float(tape.bars[-1][4])
        return None

    def arrays(self, symbol: str, tf_minutes: int = 1, tail: int = 400):
        with self._lock:
            tape = self._tapes.get(symbol)
            if tape is None:
                return None
            return tape.arrays(tf_minutes=tf_minutes, tail=tail)

    def bar_ts(self, symbol: str) -> float:
        with self._lock:
            tape = self._tapes.get(symbol)
            return tape.last_ts() if tape else 0.0

    def coverage(self) -> dict[str, int]:
        """symbol -> bar count (for health/status reporting)."""
        with self._lock:
            return {s: len(t.bars) for s, t in self._tapes.items()}
