"""Archetype + buzz-timing features for the edge model.

Train/serve consistency: the archetype is computed from the symbol's day
shape UP TO the setup bar only (``partial_archetype``), never from the full
day — the live hunter and the offline labeler see the same information.

Buzz lead-lag: minutes since the most recent headline mentioning the symbol
before the setup bar (999 = none today). Live reads the in-memory NewsStore;
training reads the persisted ``state/news_log.jsonl``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger("autotrader.edge_features")

ARCHETYPES = ("gap-and-go", "gap-and-fade", "midday-pump", "afternoon-fade", "chop")
ARCH_KEYS = tuple(f"arch_{a.replace('-', '_')}" for a in ARCHETYPES)
NO_BUZZ = 999.0


def _at_or_none(times: Sequence[dt.time | None], hh: int, mm: int, upto: int) -> int | None:
    """Index of the first bar at/after hh:mm, or None if past ``upto``."""
    target = dt.time(hh, mm)
    for i, t in enumerate(times):
        if i > upto:
            return None
        if isinstance(t, dt.time) and t >= target:
            return i
    return None


def partial_archetype(
    times: Sequence[Any], open_: Sequence[float], close: Sequence[float], upto: int,
) -> str:
    """Classify the day shape using only bars [0..upto] (no lookahead)."""
    n = min(upto, len(close) - 1)
    if n < 3:
        return "chop"
    first = float(open_[0]) if open_ is not None and len(open_) else float(close[0])
    if first <= 0:
        return "chop"
    ret_now = float(close[n]) / first - 1.0

    i1030 = _at_or_none(times, 10, 30, n)
    i1300 = _at_or_none(times, 13, 0, n)
    i1500 = _at_or_none(times, 15, 0, n)
    ret_open = float(close[i1030]) / first - 1.0 if i1030 is not None else None
    ret_mid = float(close[i1300]) / first - 1.0 if i1300 is not None else None
    ret_pm = float(close[i1500]) / first - 1.0 if i1500 is not None else None

    if ret_open is not None and ret_open >= 0.08:
        if ret_now >= ret_open * 0.6:
            return "gap-and-go"
        if ret_now < ret_open * 0.3:
            return "gap-and-fade"
        return "chop"
    if ret_mid is not None and ret_mid >= 0.10 and (ret_open or 0.0) < 0.05:
        return "midday-pump"
    if ret_pm is not None and ret_pm >= 0.05 and ret_now < ret_pm * 0.5:
        return "afternoon-fade"
    return "chop"


def arch_onehots(archetype: str) -> dict[str, float]:
    return {k: 1.0 if k == f"arch_{archetype.replace('-', '_')}" else 0.0
            for k in ARCH_KEYS}


def buzz_lead_from_log(symbol: str, ts_epoch: float, log_path: Path) -> float:
    """Minutes since the last headline mentioning ``symbol`` before ts_epoch."""
    if not log_path.exists():
        return NO_BUZZ
    bare = symbol.split(".")[0].split("-")[0].upper()
    day = dt.datetime.fromtimestamp(ts_epoch).date()
    best: float | None = None
    try:
        with log_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    h = json.loads(line)
                except json.JSONDecodeError:
                    continue
                hts = float(h.get("ts", 0.0) or 0.0)
                if hts <= 0 or hts > ts_epoch:
                    continue
                if dt.datetime.fromtimestamp(hts).date() != day:
                    continue
                syms = [str(s).upper() for s in h.get("symbols", [])]
                title = str(h.get("title", "")).upper()
                if bare in syms or f" {bare} " in f" {title} ":
                    best = hts if best is None else max(best, hts)
    except OSError:
        return NO_BUZZ
    if best is None:
        return NO_BUZZ
    return min((ts_epoch - best) / 60.0, NO_BUZZ)


def buzz_lead_from_store(symbol: str, ts_epoch: float, news: Any) -> float:
    """Live variant reading the in-memory NewsStore."""
    if news is None:
        return NO_BUZZ
    bare = symbol.split(".")[0].split("-")[0].upper()
    best: float | None = None
    try:
        for h in news.recent(max_age_s=16 * 3600, limit=500):
            hts = float(getattr(h, "ts", 0.0) or 0.0)
            if hts <= 0 or hts > ts_epoch:
                continue
            syms = [str(s).upper() for s in getattr(h, "symbols", [])]
            title = str(getattr(h, "title", "")).upper()
            if bare in syms or f" {bare} " in f" {title} ":
                best = hts if best is None else max(best, hts)
    except Exception:  # noqa: BLE001 — feature is best-effort
        return NO_BUZZ
    if best is None:
        return NO_BUZZ
    return min((ts_epoch - best) / 60.0, NO_BUZZ)


def enrich(feats: dict[str, Any], *, archetype: str, buzz_lead_min: float) -> dict[str, Any]:
    out = dict(feats)
    out.update(arch_onehots(archetype))
    out["buzz_lead_min"] = float(buzz_lead_min)
    return out
