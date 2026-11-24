"""Simulator: engine integration, friction injection, window gating."""

import random

import numpy as np
import pandas as pd

from autotrader.config import PipelineConfig
from autotrader.simulator import (
    detect_anomalies,
    simulate_genome,
    slice_windows,
    warmup_bars,
)
from autotrader.strategy_space import random_genome


def test_simulate_equities(data_map):
    cfg = PipelineConfig()
    g = random_genome(random.Random(5))
    r = simulate_genome(g, data_map, cfg, "equities")
    assert r.ok, r.error
    assert r.windows, "expected rolling windows"
    assert 0.0 <= r.train_pass_rate <= 1.0
    assert 0.0 <= r.oos_pass_rate <= 1.0
    # OOS flag must be chronologically last
    flips = [w.is_oos for w in r.windows]
    assert flips == sorted(flips), "OOS windows must come after train windows"


def test_simulate_crypto_uses_crypto_engine(crypto_map):
    cfg = PipelineConfig()
    g = random_genome(random.Random(6))
    r = simulate_genome(g, crypto_map, cfg, "crypto")
    assert r.ok, r.error
    assert r.sleeve == "crypto"


def test_friction_configuration():
    cfg = PipelineConfig()
    eq = cfg.friction.equity_engine_keys()
    assert eq["slippage_us"] >= 0.0005
    cr = cfg.friction.crypto_engine_keys()
    assert cr["taker_rate"] >= 0.00075
    assert cr["slippage"] >= 0.001


def test_slice_windows_gate_math():
    # Constant +30% growth per 10 bars, no drawdown -> every window passes.
    idx = pd.date_range("2024-01-01", periods=200, freq="B")
    growth = (1.30) ** (1 / 10)
    equity = pd.Series(100.0 * growth ** np.arange(200), index=idx)
    windows = slice_windows(
        equity,
        window_bars=10,
        step_bars=5,
        skip_bars=0,
        target_return=0.25,
        max_drawdown=0.10,
        oos_fraction=0.3,
    )
    assert windows and all(w.passed for w in windows)
    # Flat curve -> nothing passes.
    flat = pd.Series(100.0, index=idx)
    windows = slice_windows(
        flat, window_bars=10, step_bars=5, skip_bars=0,
        target_return=0.25, max_drawdown=0.10, oos_fraction=0.3,
    )
    assert windows and not any(w.passed for w in windows)


def test_detect_anomalies_flags_spikes(data_map):
    frames = {k: v.copy() for k, v in data_map.items()}
    frame = frames["AAPL.US"]
    loc = frame.index[100]
    frame.loc[loc, "volume"] = frame["volume"].median() * 50
    events = detect_anomalies(frames)
    assert any(
        e.symbol == "AAPL.US" and e.kind == "volume_spike" and e.date == str(loc.date())
        for e in events
    )


def test_warmup_covers_lookbacks():
    g = random_genome(random.Random(7))
    w = warmup_bars(g)
    assert w > max(g["momentum_lookback"], g["trend_ma_period"])

def test_train_oos_windows_do_not_overlap():
    idx = pd.date_range(
        "2024-01-01", periods=200, freq="B"
    )
    equity = pd.Series(
        100.0 * 1.01 ** np.arange(200), index=idx
    )

    windows = slice_windows(
        equity,
        window_bars=10,
        step_bars=5,
        skip_bars=0,
        target_return=0.05,
        max_drawdown=0.10,
        oos_fraction=0.3,
    )

    train = [w for w in windows if not w.is_oos]
    oos = [w for w in windows if w.is_oos]
    
    assert train and oos
    assert max(w.end for w in train) < min(
        w.start for w in oos
    ), "Training and OOS windows overlap"

def test_fitness_ignores_oos_performance():
    from autotrader.simulator import SimulationResult

    result = SimulationResult(
        genome={},
        gid="test",
        sleeve="equities",
        ok=True,
        train_pass_rate=0.8,
        train_mean_window_ret=0.05,
        train_sharpe=1.5,
        sharpe=2.0,
    )

    original_fitness = result.fitness()

    result.sharpe = 99.0
    result.oos_pass_rate = 1.0
    result.oos_mean_window_ret = 0.90

    assert result.fitness() == original_fitness
    assert result.fitness() == (0.8, 0.05, 1.5)
