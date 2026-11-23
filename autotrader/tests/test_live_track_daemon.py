"""Paper gate, live interlocks, and daemon state/amendment plumbing. Offline."""

from __future__ import annotations

import json
import random
import time

import autotrader.paper_trader as paper_mod
from autotrader.config import PipelineConfig
from autotrader.daemon import apply_amendments, merge_amendments
from autotrader.live_connector import InterlockError, LiveConnector, OrderRequest, live_status
from autotrader.strategy_space import GENE_SPACE, random_genome, reset_gene_bounds


def _offline_cfg() -> PipelineConfig:
    cfg = PipelineConfig()
    cfg.llm.disabled = True
    return cfg


# ---------------------------------------------------------------------------
# Paper gate
# ---------------------------------------------------------------------------

def test_paper_validation_dry_run(tmp_path, monkeypatch, data_map):
    """Shadow run on the synthetic fixture, no network, verdict persisted."""
    monkeypatch.setattr(paper_mod, "paper_dir", lambda: tmp_path)
    monkeypatch.setattr(paper_mod, "load_snapshot", lambda snap, codes: dict(data_map))
    cfg = _offline_cfg()
    genome = random_genome(random.Random(42))
    genome["timeframe"] = 0
    record = paper_mod.run_paper_validation(
        genome, cfg, "equities", codes=list(data_map), refetch=False
    )
    assert record["ok"], record.get("error")
    assert isinstance(record["passed"], bool)
    on_disk = json.loads((tmp_path / f"{record['gid']}.json").read_text())
    assert on_disk["gid"] == record["gid"]
    # is_live_eligible only honors fresh PASSING records
    assert paper_mod.is_live_eligible(record["gid"]) == record["passed"]


def test_is_live_eligible_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_mod, "paper_dir", lambda: tmp_path)
    stale = {"gid": "old1", "passed": True, "ts": time.time() - 30 * 86400}
    (tmp_path / "old1.json").write_text(json.dumps(stale))
    assert not paper_mod.is_live_eligible("old1")


# ---------------------------------------------------------------------------
# Live interlocks
# ---------------------------------------------------------------------------

def test_live_interlocks_default_off(tmp_path, monkeypatch):
    import autotrader.live_connector as live_mod

    monkeypatch.setattr(paper_mod, "paper_dir", lambda: tmp_path)
    monkeypatch.setattr(live_mod, "_audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.delenv("AUTOTRADER_LIVE_ENABLE", raising=False)
    cfg = _offline_cfg()

    # 1. no gate pass
    assert "hard backtest gate" in live_status(cfg, "g1", gate_passed=False)["blocked_by"]
    # 2. gate pass but no paper record
    assert "paper-trading" in live_status(cfg, "g1", gate_passed=True)["blocked_by"]
    # 3. gate + paper pass but disarmed environment
    (tmp_path / "g1.json").write_text(json.dumps({"gid": "g1", "passed": True, "ts": time.time()}))
    assert "AUTOTRADER_LIVE_ENABLE" in live_status(cfg, "g1", gate_passed=True)["blocked_by"]

    # A refused order is audited, never routed
    connector = LiveConnector(cfg, "g1", gate_passed=True)
    try:
        connector.place_order(OrderRequest("AAPL", "buy", 10.0, "equities"))
        raise AssertionError("order must be refused while disarmed")
    except InterlockError:
        pass
    audit = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(audit) == 1 and "refused" in audit[0]


def test_per_order_notional_cap(tmp_path, monkeypatch):
    import autotrader.live_connector as live_mod

    monkeypatch.setattr(paper_mod, "paper_dir", lambda: tmp_path)
    monkeypatch.setattr(live_mod, "_audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setenv("AUTOTRADER_LIVE_ENABLE", "1")
    cfg = _offline_cfg()
    (tmp_path / "g2.json").write_text(json.dumps({"gid": "g2", "passed": True, "ts": time.time()}))
    connector = LiveConnector(cfg, "g2", gate_passed=True)
    big = OrderRequest("AAPL", "buy", cfg.live.max_order_notional_usd * 2, "equities")
    try:
        connector.place_order(big)
        raise AssertionError("oversized order must be refused")
    except InterlockError as exc:
        assert "per-order cap" in str(exc)


# ---------------------------------------------------------------------------
# Daemon state + amendments
# ---------------------------------------------------------------------------

def test_daemon_amendment_merge_and_apply():
    state = {
        "cycle": 0, "amendments": {}, "gene_bounds": {}, "cycle_history": [],
        "champion": None, "consecutive_paper_passes": 0, "funding_rung": 0,
        "hard_goal_reached": False,
    }
    merge_amendments(state, {
        "mutation_sigma": 0.4,
        "population_size": 16,
        "gene_bounds": {"atr_stop_mult": [2.0, 3.0]},
        "notes": "focus on wider stops",
        "focus_failures": ["premature_stop"],
    })
    assert state["amendments"] == {"mutation_sigma": 0.4, "population_size": 16}
    assert state["gene_bounds"] == {"atr_stop_mult": [2.0, 3.0]}
    assert state["last_llm_notes"] == "focus on wider stops"

    cfg = _offline_cfg()
    try:
        overrides = apply_amendments(cfg, state)
        assert cfg.evolution.mutation_sigma == 0.4
        assert cfg.evolution.population_size == 16
        assert overrides["evolution"]["population_size"] == 16
        assert GENE_SPACE["atr_stop_mult"] == (2.0, 3.0, "f")
    finally:
        reset_gene_bounds()
