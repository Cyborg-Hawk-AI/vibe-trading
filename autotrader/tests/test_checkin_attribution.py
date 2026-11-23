"""Check-in amendment validation, memo persistence, and winner attribution.

All offline: LLM replies are stubbed at the chat() boundary.
"""

from __future__ import annotations

import json

import autotrader.attribution as attribution_mod
import autotrader.checkin as checkin_mod
from autotrader.checkin import build_cycle_payload, checkin_cycle, validate_amendments
from autotrader.config import PipelineConfig
from autotrader.strategy_space import HARD_GENE_SPACE


def _offline_cfg() -> PipelineConfig:
    cfg = PipelineConfig()
    cfg.llm.disabled = True
    return cfg


def test_validate_amendments_clamps_and_rejects():
    accepted = validate_amendments({
        "mutation_sigma": 99.0,                       # clamped to 0.6
        "population_size": 4,                         # clamped up to 8
        "llm_proposals_per_gen": "2",                 # numeric string accepted
        "gate": {"target_window_return": 0.01},       # forbidden -> dropped
        "friction": {"equity_slippage": 0.0},         # forbidden -> dropped
        "max_drawdown": 0.5,                          # forbidden -> dropped
        "gene_bounds": {
            "atr_stop_mult": [0.1, 99.0],             # clamped into hard bounds
            "not_a_gene": [0, 1],                     # rejected
        },
        "focus_failures": ["premature_stop"],
        "notes": "n" * 9999,                          # truncated
    })
    assert accepted["mutation_sigma"] == 0.6
    assert accepted["population_size"] == 8
    assert accepted["llm_proposals_per_gen"] == 2
    assert "gate" not in accepted and "friction" not in accepted
    assert "max_drawdown" not in accepted
    hard_low, hard_high, _k = HARD_GENE_SPACE["atr_stop_mult"]
    assert accepted["gene_bounds"] == {"atr_stop_mult": [hard_low, hard_high]}
    assert len(accepted["notes"]) == 2000
    assert validate_amendments("not a dict") == {}


def test_checkin_persists_even_without_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(checkin_mod, "checkins_dir", lambda: tmp_path)
    cfg = _offline_cfg()
    payload = build_cycle_payload("equities", 3, [], {"late_exit": 2}, [])
    record = checkin_cycle(cfg, payload)
    assert "no LLM provider" in record["memo"]
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    on_disk = json.loads(files[0].read_text())
    assert on_disk["cycle"] == 3 and on_disk["amendments"] == {}


def test_checkin_parses_amendments_from_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(checkin_mod, "checkins_dir", lambda: tmp_path)
    reply = (
        "The search is over-stopping; widen stops and add more names.\n"
        '```json\n{"mutation_sigma": 0.4, "equity_universe_size": 80, '
        '"gate": "hack attempt"}\n```'
    )
    monkeypatch.setattr(checkin_mod, "chat", lambda *a, **k: reply)
    cfg = PipelineConfig()  # chat is stubbed, so provider availability is moot
    record = checkin_cycle(cfg, build_cycle_payload("equities", 0, [], {}, []))
    assert record["amendments"] == {"mutation_sigma": 0.4, "equity_universe_size": 80}
    assert "over-stopping" in record["memo"]


def _write_trades_csv(tmp_path) -> str:
    """Engine-format trades.csv: entry rows have pnl=0/reason=signal."""
    rows = [
        "code,timestamp,price,qty,pnl,return_pct,reason",
        "WIN.US,2025-01-02,10.0,100,0.0,0.0,signal",
        "WIN.US,2025-01-09,12.0,100,200.0,20.0,signal",
        "LOSE.US,2025-01-03,50.0,10,0.0,0.0,signal",
        "LOSE.US,2025-01-10,45.0,10,-50.0,-10.0,stop",
    ]
    path = tmp_path / "trades.csv"
    path.write_text("\n".join(rows) + "\n")
    return str(path)


def test_winning_symbols(tmp_path):
    trades_csv = _write_trades_csv(tmp_path)
    winners = attribution_mod.winning_symbols(trades_csv)
    assert [w[0] for w in winners] == ["WIN.US"]  # losers excluded
    symbol, pnl, count = winners[0]
    assert pnl > 0 and count == 1


def test_attribute_run_tags_and_biases(tmp_path, monkeypatch, data_map):
    trades_csv = _write_trades_csv(tmp_path)
    reply = json.dumps([{
        "symbol": "WIN.US", "driver": "momentum_burst", "captured": True,
        "explanation": "breakout with volume", "gene_bias": {"breakout_lookback": 1, "fake": 1},
    }])
    monkeypatch.setattr(attribution_mod, "chat", lambda *a, **k: reply)
    out = attribution_mod.attribute_run(
        trades_csv, dict(data_map), PipelineConfig(), include_news=False
    )
    assert out["attributions"][0]["driver"] == "momentum_burst"
    assert out["attributions"][0]["captured"] is True
    assert out["gene_bias"] == {"breakout_lookback": 1}  # unknown gene rejected


def test_attribute_run_degrades_without_llm(tmp_path, monkeypatch, data_map):
    trades_csv = _write_trades_csv(tmp_path)
    monkeypatch.setattr(attribution_mod, "chat", lambda *a, **k: None)
    out = attribution_mod.attribute_run(
        trades_csv, dict(data_map), _offline_cfg(), include_news=False
    )
    assert out["gene_bias"] == {}
    assert all(a["driver"] == "unknown" for a in out["attributions"])
