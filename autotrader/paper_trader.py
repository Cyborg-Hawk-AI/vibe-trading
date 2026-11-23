"""Shadow paper-trading gate: the mandatory bridge between backtest and live.

A hard-gate finalist is re-run on the most RECENT ``paper.lookback_days`` of
market data — a window the evolutionary search never optimized against (the
search snapshot is frozen; this fetches fresh bars). Same engine, same
friction, no real orders. The candidate is "live-eligible" only if this
recent shadow run clears ``PaperConfig`` (net return, drawdown, minimum
activity).

Verdicts are persisted to ``autotrader/state/paper/<gid>.json``;
``live_connector`` refuses to arm without a passing record here.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd

from autotrader.bootstrap import state_dir
from autotrader.config import PipelineConfig
from autotrader.data import load_snapshot, prefetch, snapshot_for_interval
from autotrader.simulator import genome_interval, simulate_genome, warmup_bars
from autotrader.strategy_space import genome_id

logger = logging.getLogger(__name__)

PAPER_SNAPSHOT = "paper_recent"
DAEMON_SNAPSHOT = "daemon"


def paper_dir() -> Path:
    d = state_dir() / "paper"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_path(gid: str) -> Path:
    return paper_dir() / f"{gid}.json"


def _slice_recent(
    data_map: dict[str, pd.DataFrame],
    *,
    lookback_days: int,
    warmup: int,
) -> dict[str, pd.DataFrame]:
    """Keep warmup + lookback trading bars from the end of each series."""
    keep = max(lookback_days + warmup + 5, warmup + 20)
    out: dict[str, pd.DataFrame] = {}
    for code, frame in data_map.items():
        if frame is None or frame.empty:
            continue
        out[code] = frame.sort_index().iloc[-keep:]
    return out


def load_paper_data_map(
    genome: dict[str, Any],
    cfg: PipelineConfig,
    codes: list[str],
    *,
    refetch: bool = True,
) -> tuple[dict[str, pd.DataFrame], list[str], str]:
    """Load recent OHLCV for paper/stress.

    Prefers slicing the long daemon snapshot (avoids min_bars death on a short
    calendar window). Falls back to a widened paper_recent prefetch.
    Returns (data_map, window[start,end], interval).
    """
    end = dt.date.today()
    warmup = warmup_bars(genome)
    pad_days = max(120, 2 * warmup)
    start = end - dt.timedelta(days=cfg.paper.lookback_days + pad_days)
    interval = genome_interval(genome, tuple(cfg.data.intervals))
    window = [str(start), str(end)]

    # Preferred: long frozen daemon history, sliced to recent bars.
    daemon_snap = snapshot_for_interval(DAEMON_SNAPSHOT, interval)
    daemon_map = load_snapshot(daemon_snap, codes)
    if daemon_map:
        sliced = _slice_recent(
            daemon_map,
            lookback_days=cfg.paper.lookback_days,
            warmup=warmup,
        )
        if sliced:
            logger.info(
                "[paper] using daemon snapshot slice (%d symbols, warmup=%d)",
                len(sliced), warmup,
            )
            return sliced, window, interval

    # Fallback: dedicated recent prefetch with enough calendar pad to clear
    # min_bars and genome warmup (holidays shrink trading-bar counts).
    paper_snap = snapshot_for_interval(PAPER_SNAPSHOT, interval)
    expected_bars = cfg.paper.lookback_days + warmup
    min_bars = max(20, min(60, expected_bars))
    if refetch:
        prefetch(
            codes, str(start), str(end),
            snapshot=paper_snap, interval=interval, source=cfg.data.source,
            min_bars=min_bars,
        )
    data_map = load_snapshot(paper_snap, codes)
    if data_map:
        data_map = _slice_recent(
            data_map,
            lookback_days=cfg.paper.lookback_days,
            warmup=warmup,
        )
    return data_map, window, interval


def run_paper_validation(
    genome: dict[str, Any],
    cfg: PipelineConfig,
    sleeve: str,
    *,
    codes: list[str],
    refetch: bool = True,
) -> dict[str, Any]:
    """Shadow-run one finalist on recent data and persist the verdict."""
    gid = genome_id(genome)
    data_map, window, interval = load_paper_data_map(
        genome, cfg, codes, refetch=refetch,
    )

    record: dict[str, Any] = {
        "gid": gid,
        "sleeve": sleeve,
        "genome": genome,
        "interval": interval,
        "window": window,
        "ts": time.time(),
        "passed": False,
    }
    if not data_map:
        record["error"] = (
            "no recent data available for paper validation "
            "(daemon snapshot empty and paper_recent prefetch produced no frames)"
        )
        _record_path(gid).write_text(json.dumps(record, indent=2), encoding="utf-8")
        logger.warning("[paper] %s %s: %s", sleeve, gid, record["error"])
        return record

    paper_cfg = copy.deepcopy(cfg)
    paper_cfg.data.start_date = window[0]
    paper_cfg.data.end_date = window[1]

    result = simulate_genome(
        genome, data_map, paper_cfg, sleeve, interval=interval, keep_run_dir=False
    )
    record.update(
        ok=result.ok,
        error=result.error,
        total_return=result.total_return,
        max_drawdown=result.max_drawdown,
        sharpe=result.sharpe,
        trade_count=result.trade_count,
        win_rate=result.win_rate,
    )
    record["passed"] = bool(
        result.ok
        and result.total_return >= cfg.paper.min_return
        and result.max_drawdown <= cfg.paper.max_drawdown
        and result.trade_count >= cfg.paper.min_trades
    )
    _record_path(gid).write_text(json.dumps(record, indent=2), encoding="utf-8")
    logger.info(
        "[paper] %s %s: ret=%.2f%% mdd=%.2f%% trades=%d -> %s%s",
        sleeve, gid, record.get("total_return", 0.0) * 100,
        record.get("max_drawdown", 0.0) * 100, record.get("trade_count", 0),
        "PASS" if record["passed"] else "FAIL",
        f" error={record['error']}" if record.get("error") else "",
    )
    return record


def paper_verdict(gid: str) -> dict[str, Any] | None:
    """Latest persisted paper record for a genome, or None."""
    path = _record_path(gid)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def is_live_eligible(gid: str, max_age_days: float = 14.0) -> bool:
    """True only for a PASSING paper record fresh enough to still mean something."""
    record = paper_verdict(gid)
    if not record or not record.get("passed"):
        return False
    age_days = (time.time() - float(record.get("ts", 0))) / 86400.0
    return age_days <= max_age_days
