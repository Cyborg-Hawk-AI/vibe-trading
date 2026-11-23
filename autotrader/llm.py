"""Provider-routing chat client for the outer loop.

Sole provider by default: Kimi K3 via Moonshot's OpenAI-compatible API
(user directive 2026-08-31). The AWS Bedrock (Claude Opus) path via boto3
``converse`` is retained but excluded from the default provider chain.

Every outer-loop LLM touchpoint (check-ins, diagnostics memos, mutation
proposals, attribution deep-dives, daemon amendments) goes through
:func:`chat`. The inner simulation loop never calls this. When no provider
is reachable the pipeline degrades to fully deterministic no-ops.

Credentials are loaded from ``~/.config/autotrader/llm.env`` (never from the
repo) by ``LlmConfig`` in config.py; AWS credentials come from the standard
``~/.aws`` chain.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from autotrader.config import LlmConfig

logger = logging.getLogger(__name__)


def _chat_kimi(
    llm: LlmConfig, system: str, user: str, *, max_tokens: int
) -> str | None:
    """OpenAI-compatible completion against Moonshot/Kimi. None on failure."""
    if not llm.api_key:
        return None
    import urllib.error
    import urllib.request

    url = llm.base_url.rstrip("/") + "/chat/completions"
    # No temperature: Kimi chat models reject anything but the default (1).
    # reasoning_effort "low": kimi-k3 is reasoning-only (valid: low/high/max,
    # default max); left high, it burns the token budget (and minutes of
    # latency) on reasoning_content and can return an empty visible reply.
    # Outer-loop memos don't need deep reasoning.
    payload = {
        "model": llm.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "reasoning_effort": "low",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llm.api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=llm.timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"].get("content") or ""
        if not content.strip():
            # Reasoning budget exhausted before any visible output.
            logger.warning("Kimi returned empty content; trying next provider")
            return None
        return content
    except Exception as exc:  # noqa: BLE001 — any failure routes to the next provider
        logger.warning("Kimi call failed (%s); trying next provider", exc)
        return None


def _chat_bedrock(
    llm: LlmConfig, system: str, user: str, *, max_tokens: int
) -> str | None:
    """Bedrock Claude completion via the Converse API. None on failure."""
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        logger.warning("boto3 not installed; Bedrock fallback unavailable")
        return None
    try:
        client = boto3.client(
            "bedrock-runtime",
            region_name=llm.bedrock_region,
            config=BotoConfig(
                read_timeout=llm.timeout_s, connect_timeout=15, retries={"max_attempts": 2}
            ),
        )
        response = client.converse(
            modelId=llm.bedrock_model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.6},
        )
        parts = response["output"]["message"]["content"]
        return "".join(p.get("text", "") for p in parts)
    except Exception as exc:  # noqa: BLE001 — degrade to deterministic pipeline
        logger.warning("Bedrock call failed (%s); continuing without LLM", exc)
        return None


_PROVIDERS = {"kimi": _chat_kimi, "bedrock": _chat_bedrock}


def chat(
    llm: LlmConfig,
    system: str,
    user: str,
    *,
    max_tokens: int = 1200,
) -> str | None:
    """One chat completion through the provider chain. None if all fail."""
    if not llm.available:
        return None
    for name in llm.provider_order:
        fn = _PROVIDERS.get(name)
        if fn is None:
            continue
        reply = fn(llm, system, user, max_tokens=max_tokens)
        if reply:
            if name != llm.provider_order[0]:
                logger.info("LLM reply served by fallback provider %r", name)
            return reply
    return None


def extract_json_block(text: str) -> Any | None:
    """Pull the first JSON object/array out of an LLM reply."""
    if not text:
        return None
    start_candidates = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not start_candidates:
        return None
    start = min(start_candidates)
    decoder = json.JSONDecoder()
    for i in range(start, len(text)):
        if text[i] in "{[":
            try:
                obj, _end = decoder.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    return None
