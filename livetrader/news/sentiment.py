"""LLM macro assessment: turn the raw news + macro tape into a market view.

Every ``llm_assess_every_s`` the daemon sends the latest headlines (grouped
by topic: geopolitics, energy, fed/rates, weather, crypto) plus the macro
tape (oil, VIX, dollar, yields) to Kimi-K3 (Bedrock fallback) and receives a
bounded JSON assessment:

    {"risk_bias": -1.0..1.0,          # global risk-on/off tilt
     "crypto_bias": -1.0..1.0,
     "equity_bias": -1.0..1.0,
     "avoid_symbols": ["TIC", ...],   # names in the blast radius of bad news
     "favor_symbols": ["TIC", ...],
     "notes": "..."}

The engine consumes the biases as a *scaling* input on gross exposure —
never as a direct trade signal, so a hallucinated view can tilt sizing but
can never place a trade by itself. Local keyword sentiment (rss.py) keeps
working between assessments and when both LLM providers are down.
"""

from __future__ import annotations

import json
import logging
import time

from autotrader import llm as llm_router
from autotrader.config import LlmConfig

from livetrader.feeds.base import MacroSnapshot
from livetrader.news.rss import NewsStore

logger = logging.getLogger("livetrader.news.sentiment")

SYSTEM_PROMPT = """You are the macro/news desk for an autonomous PAPER-trading
system (simulation only; no real money). Given live headlines and the macro
tape, produce a conservative, bounded market assessment as JSON only:

{"risk_bias": float in [-1,1], "crypto_bias": float in [-1,1],
 "equity_bias": float in [-1,1], "avoid_symbols": [tickers],
 "favor_symbols": [tickers], "notes": "<= 80 words"}

Rules: biases scale exposure, they do not trade. -1 = maximum defensiveness,
+1 = fully constructive. Only list symbols plausibly affected by the cited
news. If evidence is thin, return values near 0 and say so in notes."""


def _clamp(x, lo=-1.0, hi=1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


def validate_assessment(raw: dict | None) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {
        "risk_bias": _clamp(raw.get("risk_bias", 0.0)),
        "crypto_bias": _clamp(raw.get("crypto_bias", 0.0)),
        "equity_bias": _clamp(raw.get("equity_bias", 0.0)),
        "avoid_symbols": [str(s).upper()[:6] for s in (raw.get("avoid_symbols") or [])][:20],
        "favor_symbols": [str(s).upper()[:6] for s in (raw.get("favor_symbols") or [])][:20],
        "notes": str(raw.get("notes", ""))[:600],
        "ts": time.time(),
    }
    return out


def build_payload(store: NewsStore, macro: MacroSnapshot) -> str:
    by_topic: dict[str, list[str]] = {}
    general: list[str] = []
    for h in store.recent(max_age_s=7200.0, limit=80):
        line = f"[{h.source}] {h.title}"
        if h.topics:
            for t in h.topics:
                by_topic.setdefault(t, []).append(line)
        else:
            general.append(line)
    sections = [f"MACRO TAPE: {macro.describe()}"]
    pressure = store.topic_pressure()
    if pressure:
        sections.append(
            "TOPIC SENTIMENT (local scorer): "
            + ", ".join(f"{t}={s:+.2f}" for t, s in sorted(pressure.items()))
        )
    for topic in ("geopolitics", "energy", "fed_rates", "weather", "crypto"):
        lines = by_topic.get(topic, [])
        if lines:
            sections.append(f"== {topic.upper()} ==\n" + "\n".join(lines[:12]))
    if general:
        sections.append("== GENERAL ==\n" + "\n".join(general[:15]))
    return "\n\n".join(sections)[:12000]


def assess_with_llm(cfg: LlmConfig, store: NewsStore, macro: MacroSnapshot) -> dict:
    """One LLM macro assessment; stores + returns validated result ({} on failure)."""
    payload = build_payload(store, macro)
    reply = llm_router.chat(cfg, SYSTEM_PROMPT, payload, max_tokens=700)
    assessment = validate_assessment(llm_router.extract_json_block(reply or ""))
    if assessment:
        store.set_llm_assessment(assessment)
        logger.info(
            "LLM macro assessment: risk %+.2f equity %+.2f crypto %+.2f — %s",
            assessment["risk_bias"], assessment["equity_bias"],
            assessment["crypto_bias"], assessment["notes"][:120],
        )
    else:
        logger.info("LLM assessment unavailable; keyword sentiment stays in charge")
    return assessment


def exposure_scalar(store: NewsStore, kind: str) -> float:
    """Blend LLM bias + local keyword sentiment into an exposure scale 0.4..1.2.

    Neutral = 1.0. Never goes to zero (the arena must keep learning) and
    never above 1.2 (news can't make the system reckless).
    """
    assessment = store.get_llm_assessment()
    llm_bias = 0.0
    if assessment and time.time() - assessment.get("ts", 0) < 4 * 3600:
        key = "crypto_bias" if kind == "crypto" else "equity_bias"
        llm_bias = 0.5 * assessment.get("risk_bias", 0.0) + 0.5 * assessment.get(key, 0.0)
    keyword = store.market_sentiment()
    combined = 0.6 * llm_bias + 0.4 * keyword
    return max(0.4, min(1.2, 1.0 + 0.4 * combined))


def blocked_symbols(store: NewsStore) -> set[str]:
    assessment = store.get_llm_assessment()
    if assessment and time.time() - assessment.get("ts", 0) < 4 * 3600:
        return set(assessment.get("avoid_symbols", []))
    return set()
