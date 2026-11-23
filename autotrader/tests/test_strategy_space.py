"""Genome space: rendering, sandbox compliance, mutation invariants."""

import random
import tempfile
from pathlib import Path

from autotrader.config import PipelineConfig
from autotrader.strategy_space import (
    GENE_SPACE,
    clamp_to_risk_bounds,
    crossover,
    genome_id,
    mutate,
    mutate_biased,
    random_genome,
    render_signal_engine,
)


def test_random_genome_within_bounds():
    rng = random.Random(0)
    for _ in range(50):
        g = random_genome(rng)
        for name, (low, high, _kind) in GENE_SPACE.items():
            assert low <= g[name] <= high, name
        assert g["rsi_entry_low"] < g["rsi_entry_high"]
        assert g["rsi_exit"] > g["rsi_entry_high"]


def test_mutation_and_crossover_stay_valid():
    rng = random.Random(1)
    a, b = random_genome(rng), random_genome(rng)
    for _ in range(100):
        child = crossover(mutate(a, rng), mutate_biased(b, {"premature_stop": 5}, rng), rng)
        for name, (low, high, _kind) in GENE_SPACE.items():
            assert low <= child[name] <= high, name


def test_clamp_to_risk_bounds():
    rng = random.Random(2)
    cfg = PipelineConfig()
    g = random_genome(rng)
    g["dd_breaker_pct"] = 0.5
    g = clamp_to_risk_bounds(g, cfg)
    assert g["dd_breaker_pct"] <= cfg.gate.max_drawdown
    assert g["max_weight"] <= cfg.risk.max_weight_per_symbol


def test_render_passes_runner_sandbox():
    from backtest.runner import (
        _load_module_from_file,
        _validate_signal_engine_class,
        _validate_signal_engine_source,
    )

    rng = random.Random(3)
    src = render_signal_engine(random_genome(rng))
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "signal_engine.py"
        path.write_text(src)
        _validate_signal_engine_source(path)
        module = _load_module_from_file(path, "sig_sandbox_test")
        _validate_signal_engine_class(module.SignalEngine)


def test_genome_id_stable():
    rng = random.Random(4)
    g = random_genome(rng)
    assert genome_id(g) == genome_id(dict(g))
    assert genome_id(g) != genome_id(mutate(g, rng))
