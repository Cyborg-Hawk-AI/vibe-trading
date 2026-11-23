"""Optimize loop: end-to-end mini evolution on a fixture snapshot."""

import pandas as pd

from autotrader.config import PipelineConfig
from autotrader.data import load_snapshot, write_frames
from autotrader.optimize_loop import evolve_sleeve
from autotrader.registry import Registry


def test_evolve_sleeve_end_to_end(data_map, tmp_path):
    # Fixture symbols must be members of the configured universe slice.
    write_frames(dict(data_map), snapshot="pytest_smoke")
    loaded = load_snapshot("pytest_smoke", list(data_map))
    assert set(loaded) == set(data_map)

    cfg = PipelineConfig()
    cfg.data.equity_universe_size = 2  # AAPL.US, MSFT.US head the list
    cfg.evolution.population_size = 4
    cfg.evolution.generations = 2
    cfg.evolution.elite_keep = 1
    cfg.evolution.llm_proposals_per_gen = 0
    cfg.llm.disabled = True  # offline: never contact Kimi or Bedrock

    registry = Registry(path=tmp_path / "registry.db")
    summary = evolve_sleeve(
        "equities", cfg, {}, registry, snapshot="pytest_smoke", workers=0
    )

    assert summary["best_ever"] is not None
    assert summary["leaderboard"], "leaderboard must not be empty"
    best = summary["best_ever"]
    # Honest reporting: OOS stats present regardless of gate outcome.
    assert "oos_pass_rate" in best and "train_pass_rate" in best
    assert isinstance(summary["gate_passers"], list)

    # Registry has strategies + results + generations recorded.
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "registry.db"))
    n_results = conn.execute("SELECT COUNT(*) FROM results WHERE ok=1").fetchone()[0]
    n_gens = conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
    conn.close()
    assert n_results >= 2
    assert n_gens == 2
    registry.close()


def test_registry_leaderboard_ordering(tmp_path):
    from autotrader.simulator import SimulationResult

    registry = Registry(path=tmp_path / "reg.db")
    for gid, oos in (("aaa", 0.1), ("bbb", 0.9)):
        genome = {"x": 1}
        registry.record_strategy(gid, "equities", 0, genome, "seed")
        r = SimulationResult(genome=genome, gid=gid, sleeve="equities", ok=True)
        r.oos_pass_rate = oos
        registry.record_result(r, 0)
    board = registry.leaderboard("equities")
    assert [row["gid"] for row in board] == ["bbb", "aaa"]
    registry.close()
