"""Path/environment bootstrap so autotrader can import the vibe-trading backend.

The upstream package roots itself at ``agent/`` (see pyproject
``package-dir = {"" = "agent"}``), so ``backtest.*`` and ``src.*`` resolve once
``agent/`` is on ``sys.path``. TradingAgents is a separate repo checked out
under ``TradingAgents/`` and is added on demand by the committee module.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "agent"
TRADINGAGENTS_ROOT = REPO_ROOT / "TradingAgents"
STATE_ROOT = REPO_ROOT / "autotrader" / "state"
RUNS_ROOT = REPO_ROOT / "runs" / "autotrader"


def ensure_agent_path() -> None:
    """Make ``backtest.*`` / ``src.*`` importable and enable the loader cache."""
    p = str(AGENT_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)
    # Opt-in local OHLCV cache (settled bars only) — keeps repeated
    # simulation sweeps off the network. See agent/backtest/loaders/base.py.
    os.environ.setdefault("VIBE_TRADING_DATA_CACHE", "1")
    # Whitelist our run-dir root for backtest.runner's safe_run_dir guard.
    # Must be set before src.config.accessor builds its cached EnvConfig.
    existing = os.environ.get("VIBE_TRADING_ALLOWED_RUN_ROOTS", "")
    ours = str(RUNS_ROOT)
    if ours not in existing.split(","):
        os.environ["VIBE_TRADING_ALLOWED_RUN_ROOTS"] = (
            f"{existing},{ours}" if existing else ours
        )


def ensure_tradingagents_path() -> None:
    """Make ``tradingagents.*`` importable (committee review only)."""
    p = str(TRADINGAGENTS_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)


def state_dir() -> Path:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    return STATE_ROOT


def runs_dir() -> Path:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    return RUNS_ROOT
