"""LLM router: provider chain, fallback, and JSON extraction. All offline."""

from __future__ import annotations

import autotrader.llm as llm_mod
from autotrader.config import LlmConfig
from autotrader.llm import chat, extract_json_block


def _cfg(**kw) -> LlmConfig:
    cfg = LlmConfig()
    cfg.api_key = kw.get("api_key", "sk-test")
    cfg.disabled = kw.get("disabled", False)
    cfg.provider_order = kw.get("provider_order", ("kimi", "bedrock"))
    return cfg


def test_default_chain_is_kimi_only():
    # User directive 2026-08-31: Kimi K3 only, no Bedrock fallback by default.
    assert LlmConfig().provider_order == ("kimi",)


def test_chat_uses_primary_when_it_answers(monkeypatch):
    monkeypatch.setattr(llm_mod, "_PROVIDERS", {
        "kimi": lambda llm, s, u, max_tokens: "kimi says hi",
        "bedrock": lambda llm, s, u, max_tokens: "bedrock says hi",
    })
    assert chat(_cfg(), "sys", "user") == "kimi says hi"


def test_chat_falls_back_when_primary_fails(monkeypatch):
    monkeypatch.setattr(llm_mod, "_PROVIDERS", {
        "kimi": lambda llm, s, u, max_tokens: None,      # simulated outage
        "bedrock": lambda llm, s, u, max_tokens: "bedrock says hi",
    })
    assert chat(_cfg(), "sys", "user") == "bedrock says hi"


def test_kimi_only_chain_never_touches_bedrock(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_mod, "_PROVIDERS", {
        "kimi": lambda *a, **k: calls.append("kimi") or None,
        "bedrock": lambda *a, **k: calls.append("bedrock") or "bedrock says hi",
    })
    cfg = _cfg(provider_order=("kimi",))
    assert chat(cfg, "sys", "user") is None
    assert calls == ["kimi"]


def test_chat_returns_none_when_all_fail(monkeypatch):
    monkeypatch.setattr(llm_mod, "_PROVIDERS", {
        "kimi": lambda llm, s, u, max_tokens: None,
        "bedrock": lambda llm, s, u, max_tokens: None,
    })
    assert chat(_cfg(), "sys", "user") is None


def test_disabled_config_never_calls_providers(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_mod, "_PROVIDERS", {
        "kimi": lambda *a, **k: calls.append("kimi") or "x",
        "bedrock": lambda *a, **k: calls.append("bedrock") or "x",
    })
    assert chat(_cfg(disabled=True), "sys", "user") is None
    assert calls == []


def test_extract_json_block():
    assert extract_json_block('memo text ```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_block('[{"b": 2}] trailing') == [{"b": 2}]
    assert extract_json_block("no json here") is None
    # Recovers the object even after an earlier brace that is not JSON
    assert extract_json_block('{broken then {"c": 3}') == {"c": 3}
