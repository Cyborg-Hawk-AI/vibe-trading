"""Setup snapshot logger: every hot-list candidate, taken or skipped.

Writes one JSONL row per (symbol, completed bar) to
``livetrader/state/setup_snapshots/YYYY-MM-DD.jsonl`` so the nightly edge
model can learn P(target-before-stop) from both trades we took AND setups
we correctly (or incorrectly) skipped.

Feature schema is stable and additive; the labeler only needs fields that
exist. Skipped setups are training data too.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

from livetrader.config import state_dir

logger = logging.getLogger("livetrader.snapshots")

NY = ZoneInfo("America/New_York")
_LOCK = threading.Lock()
_LAST_KEY: dict[str, float] = {}   # symbol -> last bar ts written


def _path_for(day: str | None = None):
    day = day or dt.datetime.now(tz=NY).strftime("%Y-%m-%d")
    out = state_dir() / "setup_snapshots"
    out.mkdir(parents=True, exist_ok=True)
    return out / f"{day}.jsonl"


def record_setup(
    symbol: str,
    meta: dict[str, Any],
    *,
    bar_ts: float,
    taken: bool = False,
    skip_reason: str = "",
    side: str = "long",
    extras: dict[str, Any] | None = None,
) -> None:
    """Append one setup row. Dedupes per (symbol, bar_ts) within the process."""
    key = f"{symbol}:{side}"
    with _LOCK:
        if _LAST_KEY.get(key, 0.0) >= bar_ts and not taken:
            return
        _LAST_KEY[key] = bar_ts
    try:
        et = dt.datetime.now(tz=NY)
        open_et = et.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_open = max((et - open_et).total_seconds() / 60.0, 0.0)
        row = {
            "ts": round(time.time(), 1),
            "bar_ts": round(bar_ts, 1),
            "date_ny": et.strftime("%Y-%m-%d"),
            "symbol": symbol,
            "side": side,
            "taken": bool(taken),
            "skip_reason": skip_reason,
            "gap": float(meta.get("gap", 0.0) or 0.0),
            "rvol": float(meta.get("rvol", 0.0) or 0.0),
            "buzz": float(meta.get("buzz", 0.0) or 0.0),
            "catalyst": bool(meta.get("catalyst", False)),
            "sources": list(meta.get("sources", []) or []),
            "score": float(meta.get("score", 0.0) or 0.0),
            "ext_vwap": float(meta.get("ext_vwap", 0.0) or 0.0),
            "price": float(meta.get("price", 0.0) or 0.0),
            "minutes_open": round(minutes_open, 1),
        }
        if extras:
            row.update(extras)
        with _path_for().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except Exception:  # noqa: BLE001 — snapshots must never break trading
        logger.exception("setup snapshot failed for %s", symbol)


def load_snapshots(day: str | None = None) -> list[dict]:
    path = _path_for(day)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def gate_reject_counts(day: str | None = None) -> dict[str, int]:
    """skip_reason -> count for the scoreboard."""
    counts: dict[str, int] = {}
    for row in load_snapshots(day):
        if row.get("taken"):
            continue
        reason = str(row.get("skip_reason") or "other")
        counts[reason] = counts.get(reason, 0) + 1
    return counts
