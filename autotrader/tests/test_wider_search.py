"""v2 search space: new genes, penny universe, timeframe resolution, bounds."""

from __future__ import annotations

import random

from autotrader.config import PipelineConfig
from autotrader.simulator import bars_per_trading_day, genome_interval
from autotrader.strategy_space import (
    GENE_SPACE,
    HARD_GENE_SPACE,
    SMALL_TRADE_MAX_WEIGHT,
    SMALL_TRADE_MIN_POSITIONS,
    TIMEFRAMES,
    _repair,
    apply_gene_bounds,
    clamp_to_risk_bounds,
    mutate_gene_bias,
    random_genome,
    reset_gene_bounds,
)
from autotrader.universe import get_universe


def test_new_genes_present_and_bounded():
    rng = random.Random(7)
    for _ in range(50):
        g = random_genome(rng)
        for gene in ("timeframe", "min_holding_bars", "small_trade_mode"):
            assert gene in g
        low, high, _k = GENE_SPACE["timeframe"]
        assert low <= g["timeframe"] <= high
        assert g["min_holding_bars"] < g["max_holding_bars"]
        if g["small_trade_mode"]:
            assert g["max_positions"] >= SMALL_TRADE_MIN_POSITIONS
            assert g["max_weight"] <= SMALL_TRADE_MAX_WEIGHT


def test_repair_backfills_v1_genomes():
    """Genomes recorded before v2 gain the new genes instead of crashing."""
    rng = random.Random(3)
    g = random_genome(rng)
    for gene in ("timeframe", "min_holding_bars", "small_trade_mode"):
        g.pop(gene)
    repaired = _repair(g)
    assert set(repaired) == set(GENE_SPACE)


def test_small_trade_mode_respects_config_cap():
    cfg = PipelineConfig()
    g = random_genome(random.Random(1))
    g["small_trade_mode"] = 1
    g["max_weight"] = 0.5
    clamped = clamp_to_risk_bounds(_repair(g), cfg)
    assert clamped["max_weight"] <= cfg.risk.max_weight_small_trade


def test_genome_interval_falls_back_to_daily():
    g = {"timeframe": 2}  # wants 1H
    assert genome_interval(g, ("1D",)) == "1D"          # 1H snapshot missing
    assert genome_interval(g, ("1D", "4H", "1H")) == "1H"
    assert genome_interval({"timeframe": 0}, ("1D",)) == "1D"
    assert genome_interval({}, ("1D",)) == "1D"          # gene absent
    assert TIMEFRAMES[0] == "1D"


def test_bars_per_trading_day():
    assert bars_per_trading_day("1D", "equities") == 1
    assert bars_per_trading_day("1H", "crypto") == 24
    assert bars_per_trading_day("1H", "equities") == 7
    assert bars_per_trading_day("unknown", "equities") == 1


def test_penny_universe_loads():
    codes = get_universe("penny_liquid", 20)
    assert len(codes) == 20
    assert all(c.endswith(".US") for c in codes)
    # Disjoint enough from the main universe to widen the search
    main = set(get_universe("us_liquid", 60))
    assert len(set(codes) - main) >= 15


def test_apply_gene_bounds_never_widens_past_hard_limits():
    try:
        apply_gene_bounds({"atr_stop_mult": [0.0, 100.0], "nope": [0, 1]})
        hard = HARD_GENE_SPACE["atr_stop_mult"]
        assert GENE_SPACE["atr_stop_mult"] == hard  # clamped back to shipped bounds
        apply_gene_bounds({"atr_stop_mult": [2.0, 3.0]})
        assert GENE_SPACE["atr_stop_mult"] == (2.0, 3.0, "f")  # narrowing allowed
        g = random_genome(random.Random(5))
        assert 2.0 <= g["atr_stop_mult"] <= 3.0
    finally:
        reset_gene_bounds()
    assert GENE_SPACE["atr_stop_mult"] == HARD_GENE_SPACE["atr_stop_mult"]


def test_mutate_gene_bias_pushes_direction():
    rng = random.Random(11)
    base = random_genome(rng)
    base["breakout_lookback"] = 20
    pushed_up = sum(
        mutate_gene_bias(base, {"breakout_lookback": 1}, random.Random(i))["breakout_lookback"]
        > 20 for i in range(20)
    )
    assert pushed_up >= 12  # positive bias dominates despite the random layer
