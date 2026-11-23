"""Diagnostic engine: categorize losing trades into failure modes.

Parses ``trades.csv`` (entry/exit row pairs written by the engine) plus the
OHLCV snapshot, and deterministically tags each losing round trip:

  - ``premature_stop``   — exited at a loss, price recovered above entry
                           within the recovery horizon (stop was too tight).
  - ``fakeout_entry``    — entered on a spike bar and lost within a few bars.
  - ``whipsaw_churn``    — >= 3 consecutive short losing trades in a symbol.
  - ``overbought_entry`` — entry-day RSI already above the exit band.
  - ``illiquid``         — entry-day dollar volume in the symbol's bottom decile.
  - ``late_exit``        — the trade was well in profit at its peak but was
                           given back into a loss.
  - ``oversized_loss``   — single trade lost more than the per-trade budget.

These tags feed ``strategy_space.mutate_biased`` (parameter nudges) and the
prompt overlays for the TradingAgents committee. An optional LLM pass turns
the per-generation aggregate into a human-readable memo.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

from autotrader.config import LlmConfig
from autotrader.llm import chat

logger = logging.getLogger(__name__)

FAILURE_MODES = (
    "premature_stop",
    "fakeout_entry",
    "whipsaw_churn",
    "overbought_entry",
    "illiquid",
    "late_exit",
    "oversized_loss",
)


@dataclass
class RoundTrip:
    code: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    return_pct: float
    tags: list[str] = field(default_factory=list)


@dataclass
class DiagnosticsReport:
    n_trades: int = 0
    n_losers: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    worst_symbols: list[tuple[str, float]] = field(default_factory=list)
    memo: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_trades": self.n_trades,
            "n_losers": self.n_losers,
            "failure_counts": dict(self.failure_counts),
            "worst_symbols": [[s, round(p, 4)] for s, p in self.worst_symbols],
            "memo": self.memo,
        }


def parse_round_trips(trades_csv: str | Path) -> list[RoundTrip]:
    """Rebuild round trips from the engine's entry/exit row pairs."""
    path = Path(trades_csv)
    if not path.exists():
        return []
    df = pd.read_csv(path)
    if df.empty:
        return []
    trips: list[RoundTrip] = []
    pending: dict[str, dict[str, Any]] = {}
    for _idx, row in df.iterrows():
        code = str(row["code"])
        is_entry = float(row["pnl"]) == 0.0 and str(row["reason"]) == "signal" and code not in pending
        if is_entry:
            pending[code] = dict(row)
            continue
        entry = pending.pop(code, None)
        if entry is None:
            continue
        trips.append(
            RoundTrip(
                code=code,
                entry_ts=pd.Timestamp(entry["timestamp"]),
                exit_ts=pd.Timestamp(row["timestamp"]),
                entry_price=float(entry["price"]),
                exit_price=float(row["price"]),
                qty=float(row["qty"]),
                pnl=float(row["pnl"]),
                return_pct=float(row["return_pct"]),
            )
        )
    return trips


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def tag_trades(
    trips: list[RoundTrip],
    data_map: Dict[str, pd.DataFrame],
    *,
    recovery_horizon: int = 5,
    fakeout_max_bars: int = 3,
    spike_mult: float = 3.0,
    overbought_rsi: float = 70.0,
    illiquid_pctile: float = 0.10,
    giveback_frac: float = 0.5,
    oversized_loss_pct: float = -5.0,
) -> None:
    """Attach failure-mode tags to losing trades (mutates ``trips``)."""
    per_symbol_recent: dict[str, list[RoundTrip]] = {}
    for t in trips:
        frame = data_map.get(t.code)
        if frame is None or t.pnl >= 0:
            per_symbol_recent.setdefault(t.code, []).append(t)
            continue
        close = frame["close"].astype(float)
        vol = frame["volume"].astype(float)
        idx = frame.index

        entry_loc = idx.searchsorted(t.entry_ts)
        exit_loc = idx.searchsorted(t.exit_ts)
        entry_loc = min(entry_loc, len(idx) - 1)
        exit_loc = min(exit_loc, len(idx) - 1)
        holding_bars = max(exit_loc - entry_loc, 0)

        # premature_stop: price back above entry within the recovery horizon
        after = close.iloc[exit_loc + 1 : exit_loc + 1 + recovery_horizon]
        if len(after) and float(after.max()) > t.entry_price:
            t.tags.append("premature_stop")

        # fakeout_entry: entry bar was a volume/price spike and the trade died fast
        if holding_bars <= fakeout_max_bars and entry_loc >= 20:
            med_vol = float(vol.iloc[entry_loc - 20 : entry_loc].median())
            entry_vol = float(vol.iloc[entry_loc])
            entry_ret = abs(float(close.pct_change().iloc[entry_loc]))
            ret_sig = float(close.pct_change().iloc[entry_loc - 20 : entry_loc].std())
            if (med_vol > 0 and entry_vol > spike_mult * med_vol) or (
                ret_sig > 0 and entry_ret > spike_mult * ret_sig
            ):
                t.tags.append("fakeout_entry")

        # overbought_entry
        rsi_series = _rsi(close)
        if float(rsi_series.iloc[entry_loc]) >= overbought_rsi:
            t.tags.append("overbought_entry")

        # illiquid: bottom-decile dollar volume at entry
        dollar_vol = (close * vol).iloc[max(entry_loc - 120, 0) : entry_loc + 1]
        if len(dollar_vol) >= 20:
            threshold = float(dollar_vol.quantile(illiquid_pctile))
            if float(dollar_vol.iloc[-1]) <= threshold:
                t.tags.append("illiquid")

        # late_exit: peak unrealized gain given back into a loss
        if exit_loc > entry_loc:
            window = close.iloc[entry_loc : exit_loc + 1]
            peak_gain = float(window.max()) / t.entry_price - 1.0
            if peak_gain > 0.02 and (t.exit_price / t.entry_price - 1.0) < peak_gain * (
                1.0 - giveback_frac
            ) and t.pnl < 0:
                t.tags.append("late_exit")

        # oversized_loss
        if t.return_pct <= oversized_loss_pct:
            t.tags.append("oversized_loss")

        per_symbol_recent.setdefault(t.code, []).append(t)

    # whipsaw_churn: >= 3 consecutive losing short trades on one symbol
    for code, sym_trips in per_symbol_recent.items():
        sym_trips.sort(key=lambda x: x.entry_ts)
        streak: list[RoundTrip] = []
        for t in sym_trips:
            short = (t.exit_ts - t.entry_ts).days <= 7
            if t.pnl < 0 and short:
                streak.append(t)
                if len(streak) >= 3:
                    for s in streak:
                        if "whipsaw_churn" not in s.tags:
                            s.tags.append("whipsaw_churn")
            else:
                streak = []


def diagnose(
    trades_csv: str | Path,
    data_map: Dict[str, pd.DataFrame],
) -> DiagnosticsReport:
    """Full deterministic diagnostic pass for one simulation run."""
    trips = parse_round_trips(trades_csv)
    tag_trades(trips, data_map)
    losers = [t for t in trips if t.pnl < 0]
    counts: Counter[str] = Counter()
    for t in losers:
        counts.update(t.tags)
    pnl_by_symbol: Counter[str] = Counter()
    for t in trips:
        pnl_by_symbol[t.code] += t.pnl
    worst = sorted(pnl_by_symbol.items(), key=lambda kv: kv[1])[:5]
    return DiagnosticsReport(
        n_trades=len(trips),
        n_losers=len(losers),
        failure_counts=dict(counts),
        worst_symbols=[(s, float(p)) for s, p in worst],
    )


def merge_reports(reports: list[DiagnosticsReport]) -> DiagnosticsReport:
    """Aggregate per-genome reports into a generation-level report."""
    merged = DiagnosticsReport()
    counts: Counter[str] = Counter()
    pnl: Counter[str] = Counter()
    for r in reports:
        merged.n_trades += r.n_trades
        merged.n_losers += r.n_losers
        counts.update(r.failure_counts)
        for sym, p in r.worst_symbols:
            pnl[sym] += p
    merged.failure_counts = dict(counts)
    merged.worst_symbols = sorted(pnl.items(), key=lambda kv: kv[1])[:5]
    return merged


def llm_memo(
    report: DiagnosticsReport,
    llm: LlmConfig,
    *,
    sleeve: str,
    generation: int,
) -> str:
    """Optional LLM summary of the generation's failure taxonomy."""
    reply = chat(
        llm,
        system=(
            "You are a quantitative trading coach. Given a failure-mode "
            "histogram from a strategy-evolution generation, write a concise "
            "3-5 sentence memo: what is going wrong most, and which parameter "
            "directions (stop width, entry filters, holding period, sizing) "
            "the next generation should explore. No preamble."
        ),
        user=json.dumps(
            {
                "sleeve": sleeve,
                "generation": generation,
                "n_trades": report.n_trades,
                "n_losers": report.n_losers,
                "failure_counts": report.failure_counts,
                "worst_symbols": report.worst_symbols,
            }
        ),
        max_tokens=400,
    )
    return reply or ""
