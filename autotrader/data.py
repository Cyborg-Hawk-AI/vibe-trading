"""Data prefetch and offline snapshot store.

One network pass fetches OHLCV for the whole universe through vibe-trading's
loader registry (``fetch_data_map`` with ``source="auto"`` walks the
per-market fallback chains). Frames are persisted as parquet under
``autotrader/state/data/<snapshot>/`` so every subsequent simulation sweep is
fully offline, reproducible, and immune to provider rate limits.

Point-in-time integrity: bars are settled historical OHLCV keyed by trade
date; the simulator only ever hands a strategy the slice up to the current
window end, and the engine itself shifts signals one bar (next-bar-open).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

import pandas as pd

from autotrader.bootstrap import ensure_agent_path, state_dir

logger = logging.getLogger(__name__)

ensure_agent_path()


def snapshot_dir(name: str) -> Path:
    d = state_dir() / "data" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_for_interval(base: str, interval: str) -> str:
    """Snapshot name for a bar interval (1D keeps the bare name, back-compat)."""
    return base if interval == "1D" else f"{base}__{interval}"


def _frame_path(snap: Path, code: str) -> Path:
    # Symbols like BTC-USDT / AAPL.US are filesystem-safe apart from "/".
    return snap / f"{code.replace('/', '_')}.parquet"


def prefetch(
    codes: list[str],
    start_date: str,
    end_date: str,
    *,
    snapshot: str = "default",
    interval: str = "1D",
    source: str = "auto",
    min_bars: int = 60,
) -> dict[str, int]:
    """Fetch OHLCV for ``codes`` and persist parquet frames.

    Returns a summary ``{"fetched": n, "skipped_short": n, "failed": n}``.
    Already-persisted symbols are not refetched.
    """
    from backtest.runner import fetch_data_map

    snap = snapshot_dir(snapshot)
    missing = [c for c in codes if not _frame_path(snap, c).exists()]
    summary = {
        "fetched": 0,
        "skipped_short": 0,
        "failed": 0,
        "already_present": len(codes) - len(missing),
    }
    # Fetch in small batches so one bad symbol can't sink a whole market group
    # and progress is durable across interruptions.
    batch_size = 8
    for i in range(0, len(missing), batch_size):
        batch = missing[i : i + batch_size]
        try:
            result = fetch_data_map(
                {
                    "codes": batch,
                    "start_date": start_date,
                    "end_date": end_date,
                    "interval": interval,
                    "source": source,
                }
            )
            data_map = result.data_map
        except Exception as exc:  # noqa: BLE001 — provider failures must not kill prefetch
            logger.warning("prefetch batch %s failed: %s", batch, exc)
            summary["failed"] += len(batch)
            continue
        for code in batch:
            frame = data_map.get(code)
            if frame is None or len(frame) < min_bars:
                n = 0 if frame is None else len(frame)
                logger.warning("prefetch: %s returned %d bars (<%d); skipping", code, n, min_bars)
                summary["skipped_short" if frame is not None else "failed"] += 1
                continue
            frame = _normalize_daily_index(frame, interval)
            frame.to_parquet(_frame_path(snap, code))
            summary["fetched"] += 1
    manifest = {
        "start_date": start_date,
        "end_date": end_date,
        "interval": interval,
        "symbols": sorted(p.stem for p in snap.glob("*.parquet")),
    }
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return summary


def _normalize_daily_index(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Align 1D calendars across providers (OKX 16:00 vs yfinance midnight).

    Mixed timestamps on daily bars inflate the union calendar and trip the
    portfolio drawdown breaker with fake gaps. Normalize to calendar dates.
    """
    if frame is None or frame.empty:
        return frame
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame = frame.copy()
        frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()
    if interval == "1D":
        frame = frame.copy()
        frame.index = pd.to_datetime(frame.index).normalize()
        frame = frame[~frame.index.duplicated(keep="last")]
    return frame


def load_snapshot(snapshot: str = "default", codes: list[str] | None = None) -> Dict[str, pd.DataFrame]:
    """Load persisted frames. Missing symbols are silently omitted."""
    snap = snapshot_dir(snapshot)
    out: Dict[str, pd.DataFrame] = {}
    # Infer interval from snapshot name (daemon__1H → 1H; bare → 1D).
    interval = "1D"
    if "__" in snapshot:
        interval = snapshot.rsplit("__", 1)[-1]
    paths = sorted(p for p in snap.glob("*.parquet") if not p.name.startswith("._"))
    want = set(codes) if codes is not None else None
    for path in paths:
        # None of the curated universe symbols contain "/", so the stem is the
        # code verbatim (the "/" -> "_" mapping in _frame_path is identity).
        code = path.stem
        if want is not None and code not in want:
            continue
        frame = pd.read_parquet(path)
        out[code] = _normalize_daily_index(frame, interval)
    return out


def write_frames(
    frames: Dict[str, pd.DataFrame], *, snapshot: str
) -> None:
    """Persist externally-constructed frames (used by tests/fixtures)."""
    snap = snapshot_dir(snapshot)
    for code, frame in frames.items():
        frame.to_parquet(_frame_path(snap, code))
