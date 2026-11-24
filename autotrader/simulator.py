"""Autonomous simulation engine.

Evaluates one strategy genome against one sleeve (equities or crypto) by
running vibe-trading's real market engines in-process over the full history,
then scoring rolling 2-week execution windows from the resulting equity curve.

Design decisions (see ARCHITECTURE.md):
  - One full-period engine run per evaluation, windows scored on the equity
    curve. This models continuously compounding capital ($100 -> $500 -> ...)
    and is ~50x cheaper than a fresh backtest per window.
  - Point-in-time integrity is inherited from the engine (next-bar-open
    signal shift, causal rolling features only, OHLC sanity guard at the
    loader boundary) plus the offline snapshot store (settled bars only).
  - Execution friction comes from ``FrictionConfig`` and is injected into the
    engine config per sleeve (equity slippage floor 0.05%, zero commission;
    crypto taker 0.075% both sides + 0.10% slippage).
  - Anomaly events (volume/volatility spikes) are detected deterministically
    per bar and logged for outer-loop research; no LLM in this loop.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import math
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

from autotrader.bootstrap import ensure_agent_path, runs_dir
from autotrader.config import PipelineConfig
from autotrader.strategy_space import genome_id, render_signal_engine

ensure_agent_path()

logger = logging.getLogger(__name__)

SLEEVES = ("equities", "crypto", "equities_penny")

# Approximate tradable bars per day per interval, used to express the
# 2-week gate window in bars for intraday timeframes. Crypto trades 24/7;
# equities get regular-trading-hours counts.
_BARS_PER_DAY = {
    "1D": {"equity": 1, "crypto": 1},
    "4H": {"equity": 2, "crypto": 6},
    "1H": {"equity": 7, "crypto": 24},
    "15m": {"equity": 26, "crypto": 96},
    "5m": {"equity": 78, "crypto": 288},
}

# Intervals scored per-SESSION with the daytrade gate (see DaytradeGateConfig).
INTRADAY_INTERVALS = ("5m", "15m")


def bars_per_trading_day(interval: str, sleeve: str) -> int:
    kind = "crypto" if sleeve == "crypto" else "equity"
    return _BARS_PER_DAY.get(interval, {"equity": 1, "crypto": 1})[kind]


def genome_interval(genome: dict[str, Any], available: tuple[str, ...] = ("1D",)) -> str:
    """Resolve the timeframe gene to an interval that has snapshot data."""
    from autotrader.strategy_space import TIMEFRAMES

    idx = int(genome.get("timeframe", 0))
    idx = min(max(idx, 0), len(TIMEFRAMES) - 1)
    interval = TIMEFRAMES[idx]
    if interval in available:
        return interval
    # Fall back to daily when present (legacy behavior); otherwise to the
    # first available interval (daytrade snapshots carry no 1D data).
    return "1D" if "1D" in available or not available else available[0]


@dataclass
class WindowResult:
    start: str
    end: str
    ret: float
    max_dd: float
    passed: bool
    is_oos: bool


@dataclass
class AnomalyEvent:
    date: str
    symbol: str
    kind: str        # "volume_spike" | "volatility_spike"
    magnitude: float


@dataclass
class SimulationResult:
    genome: dict[str, Any]
    gid: str
    sleeve: str
    ok: bool
    error: str = ""
    run_dir: str = ""
    # Full-period stats
    total_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    train_sharpe: float = 0.0
    trade_count: int = 0
    win_rate: float = 0.0
    # Window stats
    windows: list[WindowResult] = field(default_factory=list)
    train_pass_rate: float = 0.0
    oos_pass_rate: float = 0.0
    train_mean_window_ret: float = 0.0
    oos_mean_window_ret: float = 0.0
    gate_passed: bool = False
    # Stretch diagnostic (legacy 25%/80%) — never blocks promotion.
    stretch_25_pass_rate: float = 0.0
    stretch_25_passed: bool = False
    # Day-trade session scoring (intraday intervals only; windows == sessions)
    interval: str = "1D"
    sessions: int = 0
    trades_per_session: float = 0.0
    daytrade_gate_used: bool = False
    # Diagnostics inputs
    trades_csv: str = ""
    anomalies: list[AnomalyEvent] = field(default_factory=list)

    def fitness(self) -> tuple[float, float, float]:
        """Lexicographic fitness on TRAIN windows (OOS is reserved for the gate)."""
        if not self.ok:
            return (-1.0, -1.0, -1.0)
        return (
            self.train_pass_rate,
            self.train_mean_window_ret,
            self.train_sharpe if math.isfinite(self.train_sharpe) else -1.0,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fitness"] = list(self.fitness())
        return d


def _finite(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def warmup_bars(genome: dict[str, Any]) -> int:
    return int(
        max(
            genome["momentum_lookback"] + int(genome.get("mom_skip_bars", 0)),
            genome["trend_ma_period"],
            genome["vol_lookback"],
            genome["breakout_lookback"],
            int(genome.get("regime_ma_period", 50)),
            int(genome.get("compress_lookback", 10)),
        )
        + 10
    )


def slice_windows(
    equity: pd.Series,
    *,
    window_bars: int,
    step_bars: int,
    skip_bars: int,
    target_return: float,
    max_drawdown: float,
    oos_fraction: float,
) -> list[WindowResult]:
    """Score rolling execution windows on an equity curve.

    Each window is ``window_bars`` trading bars; a window passes when its
    net return meets the target AND its internal drawdown stays under the cap.
    The final ``oos_fraction`` of windows (chronologically) is flagged OOS.
    """
    eq = equity.dropna()
    n = len(eq)
    results: list[WindowResult] = []
    starts = list(range(skip_bars, n - window_bars, step_bars))
    if not starts:
        return results
    oos_from = math.floor(len(starts) * (1.0 - oos_fraction))
    for k, s in enumerate(starts):
        e = s + window_bars

        if k < oos_from < len(starts):
          if e >= starts[oos_from]: 
            continue
        
        w = eq.iloc[s : e + 1]
        base = float(w.iloc[0])
        if base <= 0:
            continue
        ret = float(w.iloc[-1]) / base - 1.0
        peak = w.cummax()
        mdd = float((w / peak - 1.0).min()) * -1.0
        results.append(
            WindowResult(
                start=str(w.index[0].date()),
                end=str(w.index[-1].date()),
                ret=ret,
                max_dd=mdd,
                passed=(ret >= target_return and mdd <= max_drawdown),
                is_oos=(k >= oos_from),
            )
        )
    return results


def slice_sessions(
    equity: pd.Series,
    *,
    skip_bars: int,
    session_max_drawdown: float,
    oos_fraction: float,
) -> list[WindowResult]:
    """Score each trading SESSION (calendar day of the bar index) as a window.

    A session passes when its net P&L is positive (a green day) AND its
    intraday drawdown stays under the session cap. The final
    ``oos_fraction`` of sessions (chronologically) is flagged OOS — the
    gate only ever grades held-out days.
    """
    eq = equity.dropna()
    if len(eq) <= skip_bars:
        return []
    eq = eq.iloc[skip_bars:]
    results: list[WindowResult] = []
    days = sorted(set(eq.index.normalize()))
    oos_from = math.floor(len(days) * (1.0 - oos_fraction))
    prev_close: float | None = None
    for k, day in enumerate(days):
        w = eq[eq.index.normalize() == day]
        if w.empty:
            continue
        base = prev_close if prev_close and prev_close > 0 else float(w.iloc[0])
        prev_close = float(w.iloc[-1])
        if base <= 0:
            continue
        ret = float(w.iloc[-1]) / base - 1.0
        peak = w.cummax()
        mdd = float((w / peak - 1.0).min()) * -1.0
        results.append(
            WindowResult(
                start=str(day.date()),
                end=str(day.date()),
                ret=ret,
                max_dd=mdd,
                passed=(ret > 0.0 and mdd <= session_max_drawdown),
                is_oos=(k >= oos_from),
            )
        )
    return results


def detect_anomalies(
    data_map: Dict[str, pd.DataFrame],
    *,
    vol_spike_mult: float = 5.0,
    ret_spike_sigma: float = 4.0,
    lookback: int = 20,
    max_events: int = 200,
) -> list[AnomalyEvent]:
    """Deterministic per-bar anomaly scan (volume + volatility spikes).

    These are the 'on-demand research' triggers: the events are logged during
    simulation and batched for outer-loop enrichment (news lookup / LLM
    summary) instead of blocking the tick loop.
    """
    events: list[AnomalyEvent] = []
    for code, df in data_map.items():
        if df is None or len(df) < lookback + 2:
            continue
        vol = df["volume"].astype(float)
        med = vol.rolling(lookback).median().shift(1)
        vol_ratio = vol / med.replace(0.0, np.nan)
        ret = df["close"].astype(float).pct_change()
        sig = ret.rolling(lookback).std().shift(1)
        ret_z = (ret / sig.replace(0.0, np.nan)).abs()

        for ts in df.index[(vol_ratio > vol_spike_mult).fillna(False)]:
            events.append(
                AnomalyEvent(str(ts.date()), code, "volume_spike", round(float(vol_ratio.loc[ts]), 2))
            )
        for ts in df.index[(ret_z > ret_spike_sigma).fillna(False)]:
            events.append(
                AnomalyEvent(str(ts.date()), code, "volatility_spike", round(float(ret_z.loc[ts]), 2))
            )
    events.sort(key=lambda e: (e.date, -e.magnitude))
    return events[:max_events]


def _build_config(
    codes: list[str],
    cfg: PipelineConfig,
    sleeve: str,
    interval: str | None = None,
) -> dict[str, Any]:
    is_equity = sleeve in ("equities", "equities_penny")
    base: dict[str, Any] = {
        "codes": codes,
        "start_date": cfg.data.start_date,
        "end_date": cfg.data.end_date,
        "interval": interval or cfg.data.interval,
        "source": cfg.data.source,
        "initial_cash": (
            cfg.capital.equities_usd if is_equity else cfg.capital.crypto_usd
        ),
        "engine": "daily",
    }
    if is_equity:
        base.update(cfg.friction.equity_engine_keys())
    else:
        base.update(cfg.friction.crypto_engine_keys())
    return base


class _LightArtifacts:
    """Patch that skips per-symbol OHLCV CSV dumps during sweep evaluations.

    A population sweep runs hundreds of engine evaluations; writing ~60 OHLCV
    CSVs per evaluation would burn gigabytes for data that already lives in
    the parquet snapshot. Equity/trades/positions/metrics are still written —
    diagnostics needs them.
    """

    def __init__(self, engine: Any):
        self._engine = engine
        self._orig = engine._write_artifacts

    def __enter__(self) -> None:
        engine = self._engine
        orig = self._orig

        def light(run_dir, data_map, dates, equity_series, bench_equity,
                  bench_ret, target_pos, metrics, codes):
            return orig(
                run_dir, {}, dates, equity_series, bench_equity,
                bench_ret, target_pos, metrics, codes,
            )

        engine._write_artifacts = light

    def __exit__(self, *exc: Any) -> None:
        self._engine._write_artifacts = self._orig


def simulate_genome(
    genome: dict[str, Any],
    data_map: Dict[str, pd.DataFrame],
    cfg: PipelineConfig,
    sleeve: str,
    *,
    interval: str = "1D",
    keep_run_dir: bool = False,
    scan_anomalies: bool = False,
) -> SimulationResult:
    """Run one genome against one sleeve's data and score the gate windows.

    ``data_map`` must be the offline snapshot slice for this sleeve at
    ``interval`` (callers resolve the genome's timeframe gene via
    :func:`genome_interval`). The run directory (config + rendered strategy +
    artifacts) lives under ``runs/autotrader/`` and is deleted after scoring
    unless ``keep_run_dir``.
    """
    from backtest.metrics import calc_bars_per_year
    from backtest.runner import _AutoLoader, _create_market_engine, _detect_primary_source

    gid = genome_id(genome)
    result = SimulationResult(genome=genome, gid=gid, sleeve=sleeve, ok=False)
    codes = sorted(data_map)
    if not codes:
        result.error = "empty data_map"
        return result

    config = _build_config(codes, cfg, sleeve, interval=interval)
    run_dir = runs_dir() / f"{sleeve}_{gid}_{uuid.uuid4().hex[:6]}"
    (run_dir / "code").mkdir(parents=True, exist_ok=True)
    # Execution-context params for the active interval (not genome DNA):
    # correct vol annualization for sizing + session EOD flat on the
    # intraday day-trade horizons (equities only; crypto trades 24/7).
    from autotrader.strategy_space import bars_per_year_for

    is_intraday = interval in INTRADAY_INTERVALS
    extra_params: dict[str, Any] = {"bars_per_year": bars_per_year_for(interval, sleeve)}
    if is_intraday and sleeve != "crypto":
        extra_params["session_eod_flat"] = 1
    (run_dir / "code" / "signal_engine.py").write_text(
        render_signal_engine(genome, extra_params), encoding="utf-8"
    )
    (run_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    result.run_dir = str(run_dir)

    # Instantiate the rendered strategy through the runner's own validators so
    # sweep evaluations get exactly the sandbox the production path enforces.
    from backtest.runner import (
        _load_module_from_file,
        _validate_signal_engine_class,
    )

    try:
        module = _load_module_from_file(
            run_dir / "code" / "signal_engine.py", f"signal_engine_{gid}"
        )
        _validate_signal_engine_class(module.SignalEngine)
        signal_engine = module.SignalEngine()
    except Exception as exc:  # noqa: BLE001
        result.error = f"strategy load failed: {exc}"
        return result

    effective_source = _detect_primary_source(codes, "auto")
    bars_per_year = calc_bars_per_year(interval, effective_source)
    engine = _create_market_engine(effective_source, config, codes)
    loader = _AutoLoader(data_map)

    stdout_sink = io.StringIO()
    try:
        with _LightArtifacts(engine), contextlib.redirect_stdout(stdout_sink):
            metrics = engine.run_backtest(
                config, loader, signal_engine, run_dir, bars_per_year=bars_per_year
            )
    except SystemExit:
        result.error = f"engine aborted: {stdout_sink.getvalue().strip()[:300]}"
        return result
    except Exception as exc:  # noqa: BLE001 — a bad genome must not kill the sweep
        result.error = f"engine error: {exc}"
        return result

    result.ok = True
    result.total_return = _finite(metrics.get("total_return"))
    result.max_drawdown = abs(_finite(metrics.get("max_drawdown")))
    result.sharpe = _finite(metrics.get("sharpe"))
    result.trade_count = int(_finite(metrics.get("trade_count")))
    result.win_rate = _finite(metrics.get("win_rate"))
    result.trades_csv = str(run_dir / "artifacts" / "trades.csv")

    equity_path = run_dir / "artifacts" / "equity.csv"
    equity = pd.read_csv(equity_path, index_col=0, parse_dates=True)["equity"]

    result.interval = interval
    if is_intraday:
        # Day-trade scoring: every trading SESSION is a window; the gate is
        # "makes money daily" — green held-out days with a real daily edge,
        # capped intraday drawdown, and enough trades to be day-trading.
        dt_gate = cfg.daytrade_gate
        result.daytrade_gate_used = True
        result.windows = slice_sessions(
            equity,
            skip_bars=warmup_bars(genome),
            session_max_drawdown=dt_gate.session_max_drawdown,
            oos_fraction=dt_gate.oos_fraction,
        )
        train = [w for w in result.windows if not w.is_oos]
        oos = [w for w in result.windows if w.is_oos]
        result.sessions = len(result.windows)
        result.trades_per_session = (
            result.trade_count / result.sessions if result.sessions else 0.0
        )
        result.train_pass_rate = (
            sum(w.passed for w in train) / len(train) if train else 0.0
        )
        result.oos_pass_rate = sum(w.passed for w in oos) / len(oos) if oos else 0.0
        result.train_mean_window_ret = (
            float(np.mean([w.ret for w in train])) if train else 0.0
        )
        result.oos_mean_window_ret = float(np.mean([w.ret for w in oos])) if oos else 0.0
        result.gate_passed = bool(
            oos
            and len(oos) >= dt_gate.min_oos_sessions
            and result.oos_pass_rate >= dt_gate.min_green_rate
            and result.oos_mean_window_ret >= dt_gate.min_mean_daily_ret
            and result.max_drawdown <= dt_gate.max_drawdown
            and result.trades_per_session >= dt_gate.min_trades_per_session
        )
    else:
        gate = cfg.gate
        # Express the 2-week gate window in bars for the active timeframe so
        # intraday genomes are held to the same calendar-time target.
        bpd = bars_per_trading_day(interval, sleeve)
        result.windows = slice_windows(
            equity,
            window_bars=gate.window_trading_days * bpd,
            step_bars=gate.window_step_days * bpd,
            skip_bars=warmup_bars(genome),
            target_return=gate.target_window_return,
            max_drawdown=gate.max_drawdown,
            oos_fraction=gate.oos_fraction,
        )
        train = [w for w in result.windows if not w.is_oos]
        oos = [w for w in result.windows if w.is_oos]
        result.train_pass_rate = (
            sum(w.passed for w in train) / len(train) if train else 0.0
        )
        result.oos_pass_rate = sum(w.passed for w in oos) / len(oos) if oos else 0.0
        result.train_mean_window_ret = (
            float(np.mean([w.ret for w in train])) if train else 0.0
        )
        result.oos_mean_window_ret = float(np.mean([w.ret for w in oos])) if oos else 0.0

        # Operational gate: >= target return in >= min pass rate of HELD-OUT
        # windows, plus the account-level drawdown cap over the whole run.
        result.gate_passed = bool(
            oos
            and result.oos_pass_rate >= gate.min_window_pass_rate
            and result.max_drawdown <= gate.max_drawdown
        )

        # Stretch metric (informational): same windows re-scored at 25%/80%.
        stretch_windows = slice_windows(
            equity,
            window_bars=gate.window_trading_days * bpd,
            step_bars=gate.window_step_days * bpd,
            skip_bars=warmup_bars(genome),
            target_return=gate.stretch_target_return,
            max_drawdown=gate.max_drawdown,
            oos_fraction=gate.oos_fraction,
        )
        stretch_oos = [w for w in stretch_windows if w.is_oos]
        result.stretch_25_pass_rate = (
            sum(w.passed for w in stretch_oos) / len(stretch_oos) if stretch_oos else 0.0
        )
        result.stretch_25_passed = bool(
            stretch_oos
            and result.stretch_25_pass_rate >= gate.stretch_min_pass_rate
            and result.max_drawdown <= gate.max_drawdown
        )
    
  train_windows = [w for w in result.windows if not w.is_oos]
  result.train_sharpe = -1.0

  if train_windows: 
      train_end = pd.Timestamp(train_windows[-1].end).date()
      train_equity = equity.dropna()
      train_equity = train_equity[
        train_equity.index.date < train_end
    ]
      returns = train_equity.pct_change().dropna()

      if len(returns) > 1 and returns.std() > 0: 
        result.train_sharpe = float(
          returns.mean() / returns.std()
          * np.sqrt(bars_per_year)
        )
      
    if scan_anomalies:
        result.anomalies = detect_anomalies(data_map)

    if not keep_run_dir:
        # Keep trades.csv (diagnostics reads it) but drop the rest.
        trades_src = run_dir / "artifacts" / "trades.csv"
        keep = trades_src.read_bytes() if trades_src.exists() else b""
        shutil.rmtree(run_dir, ignore_errors=True)
        if keep:
            trades_src.parent.mkdir(parents=True, exist_ok=True)
            trades_src.write_bytes(keep)
    return result


def rerun_with_validation(
    genome: dict[str, Any],
    data_map: Dict[str, pd.DataFrame],
    cfg: PipelineConfig,
    sleeve: str,
) -> dict[str, Any]:
    """Full artifact + validation run for a finalist (walk-forward, Monte
    Carlo, bootstrap through vibe-trading's validation module)."""
    from backtest.runner import main as runner_main

    gid = genome_id(genome)
    run_dir = runs_dir() / f"final_{sleeve}_{gid}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "code").mkdir(parents=True, exist_ok=True)
    config = _build_config(sorted(data_map), cfg, sleeve)
    config["validation"] = {
        "monte_carlo": {"n_simulations": 1000, "seed": 42},
        "bootstrap": {"n_bootstrap": 1000, "confidence": 0.95},
        "walk_forward": {"n_windows": 5},
    }
    # The runner re-fetches through the registry; with the loader cache warm
    # this stays offline for settled ranges. Fall back to snapshot-backed
    # in-process execution if the environment blocks network entirely.
    (run_dir / "code" / "signal_engine.py").write_text(
        render_signal_engine(genome), encoding="utf-8"
    )
    (run_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    stdout_sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_sink):
            runner_main(run_dir)
    except SystemExit:
        logger.warning(
            "runner_main failed for finalist %s (%s); falling back to in-process run",
            gid,
            stdout_sink.getvalue().strip()[:200],
        )
        result = simulate_genome(
            genome, data_map, cfg, sleeve, keep_run_dir=True, scan_anomalies=True
        )
        return {"run_dir": result.run_dir, "metrics": result.to_dict()}

    metrics_line = stdout_sink.getvalue().strip().splitlines()
    metrics = json.loads(metrics_line[-1]) if metrics_line else {}
    return {"run_dir": str(run_dir), "metrics": metrics}
