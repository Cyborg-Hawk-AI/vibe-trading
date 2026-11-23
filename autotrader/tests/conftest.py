"""Shared fixtures: a deterministic 2-symbol synthetic OHLCV panel."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autotrader.bootstrap import ensure_agent_path  # noqa: E402

ensure_agent_path()


def make_frame(seed: int, n: int = 520, drift: float = 0.0004) -> pd.DataFrame:
    rs = np.random.RandomState(seed)
    idx = pd.date_range("2024-07-01", periods=n, freq="B")
    close = 100 * np.exp(np.cumsum(rs.normal(drift, 0.018, n)))
    return pd.DataFrame(
        {
            "open": close * (1 + rs.normal(0, 0.002, n)),
            "high": close * (1 + np.abs(rs.normal(0, 0.008, n))),
            "low": close * (1 - np.abs(rs.normal(0, 0.008, n))),
            "close": close,
            "volume": rs.lognormal(13, 0.8, n),
        },
        index=idx,
    )


@pytest.fixture(scope="session")
def data_map() -> dict[str, pd.DataFrame]:
    return {
        "AAPL.US": make_frame(1, drift=0.0006),
        "MSFT.US": make_frame(2, drift=0.0003),
    }


@pytest.fixture(scope="session")
def crypto_map() -> dict[str, pd.DataFrame]:
    return {
        "BTC-USDT": make_frame(3, drift=0.0008),
        "ETH-USDT": make_frame(4, drift=0.0002),
    }
