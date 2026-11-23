"""Continuous validation ladder helpers (Stages A–D).

Stage A (historical OOS gate) lives in simulator.gate_passed.
Stage B (paper shadow) lives in paper_trader.
Stage C (live arena) is tracked by livetrader.
Stage D (stress): re-sim at 2× friction; must still clear operational gate.
Eval profile: funded-prop style scoreboard (+8% / 5% trailing DD).
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from autotrader.bootstrap import state_dir
from autotrader.config import PipelineConfig
from autotrader.simulator import simulate_genome

logger = logging.getLogger(__name__)


def stress_2x_friction(
    genome: dict[str, Any],
    data_map: dict[str, pd.DataFrame],
    cfg: PipelineConfig,
    sleeve: str,
) -> dict[str, Any]:
    """Re-run with doubled slippage/fees. Passes if still gate_passed."""
    stressed = copy.deepcopy(cfg)
    stressed.friction.equity_slippage = cfg.friction.equity_slippage * 2.0
    stressed.friction.crypto_slippage = cfg.friction.crypto_slippage * 2.0
    stressed.friction.crypto_taker_fee = cfg.friction.crypto_taker_fee * 2.0
    result = simulate_genome(genome, data_map, stressed, sleeve, keep_run_dir=False)
    return {
        "ok": result.ok,
        "gate_passed": result.gate_passed,
        "oos_pass_rate": result.oos_pass_rate,
        "oos_mean_window_ret": result.oos_mean_window_ret,
        "max_drawdown": result.max_drawdown,
        "total_return": result.total_return,
        "passed": bool(result.ok and result.gate_passed),
    }


def eval_profile_score(
    equity: pd.Series | None,
    cfg: PipelineConfig,
) -> dict[str, Any]:
    """Funded-eval scoreboard on an equity curve (peak-to-trough trailing DD)."""
    target = cfg.eval_profile.profit_target
    max_dd = cfg.eval_profile.trailing_max_dd
    out = {
        "profit_target": target,
        "trailing_max_dd": max_dd,
        "hit_target": False,
        "breached_dd": False,
        "passed": False,
        "max_trailing_dd": 0.0,
        "total_return": 0.0,
    }
    if equity is None or len(equity) < 5:
        out["error"] = "insufficient equity"
        return out
    eq = equity.astype(float)
    start = float(eq.iloc[0])
    if start <= 0:
        out["error"] = "bad start equity"
        return out
    out["total_return"] = float(eq.iloc[-1] / start - 1.0)
    peak = eq.cummax()
    trailing_dd = (eq / peak - 1.0).abs()
    out["max_trailing_dd"] = float(trailing_dd.max())
    out["hit_target"] = out["total_return"] >= target
    out["breached_dd"] = out["max_trailing_dd"] > max_dd
    # Pass = hit target without ever breaching trailing DD (prop-style).
    out["passed"] = bool(out["hit_target"] and not out["breached_dd"])
    return out


def eval_profile_from_sim_result(sim_result: Any, cfg: PipelineConfig) -> dict[str, Any]:
    """Build a synthetic equity from window returns if no curve on disk."""
    # Prefer total_return / max_drawdown as a coarse proxy when curve absent.
    proxy = {
        "profit_target": cfg.eval_profile.profit_target,
        "trailing_max_dd": cfg.eval_profile.trailing_max_dd,
        "hit_target": bool(sim_result.total_return >= cfg.eval_profile.profit_target),
        "breached_dd": bool(sim_result.max_drawdown > cfg.eval_profile.trailing_max_dd),
        "max_trailing_dd": float(sim_result.max_drawdown),
        "total_return": float(sim_result.total_return),
        "passed": False,
        "proxy": True,
    }
    proxy["passed"] = bool(proxy["hit_target"] and not proxy["breached_dd"])
    return proxy


def export_gate_passers_for_livetrader(
    passers: list[dict[str, Any]], *, path: Path | None = None
) -> Path:
    """Write genomes livetrader can seed on next restart."""
    path = path or state_dir() / "gate_passers_seed.json"
    payload = []
    for p in passers:
        genome = p.get("genome")
        if not genome:
            continue
        result = p.get("result") or {}
        payload.append({
            "gid": p.get("gid") or result.get("gid"),
            "sleeve": result.get("sleeve") or p.get("sleeve"),
            "genome": genome,
            "oos_pass_rate": result.get("oos_pass_rate", p.get("oos_pass_rate")),
            "oos_mean_window_ret": result.get("oos_mean_window_ret", p.get("oos_mean_window_ret")),
            "gate_passed": True,
        })
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def load_gate_passer_seeds(limit: int = 16, *, path: Path | None = None) -> list[dict]:
    """Return passer rows with at least ``genome`` (and gid/sleeve when present)."""
    path = path or state_dir() / "gate_passers_seed.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("genome"):
            out.append(r)
        elif any(k in r for k in ("momentum_lookback", "max_positions", "atr_stop_mult")):
            # Legacy: file stored bare genomes.
            out.append({"genome": r, "gate_passed": True})
        if len(out) >= limit:
            break
    return out
