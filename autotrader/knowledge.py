"""Loader for the trading knowledge-base digest injected into LLM check-ins.

The digest (knowledge-base/applied/llm-digest.md) condenses the legends/
strategies/risk library into ~2.5KB of search priors, failure-mode fixes,
and sizing canon. Both the autotrader and livetrader check-ins append it to
their system prompts so amendment proposals draw on the documented
literature instead of reasoning from scratch.

Fails soft: if the file is missing the check-ins run exactly as before.
"""

from __future__ import annotations

from pathlib import Path

_KB_DIR = Path(__file__).resolve().parents[1] / "knowledge-base" / "applied"
_DIGEST_PATH = _KB_DIR / "llm-digest.md"
_LESSONS_PATH = _KB_DIR / "lessons-learned.md"
_MAX_CHARS = 3500
_LESSONS_MAX_CHARS = 2000
_cache: str | None = None
_lessons_cache: tuple[float, str] | None = None  # (mtime, text)


def digest() -> str:
    """The knowledge digest, cached for the process lifetime ('' if absent)."""
    global _cache
    if _cache is None:
        try:
            text = _DIGEST_PATH.read_text(encoding="utf-8")
            # Drop the file's own header commentary; keep from the first '##'.
            body_start = text.find("##")
            _cache = text[body_start:][:_MAX_CHARS] if body_start >= 0 else text[:_MAX_CHARS]
        except OSError:
            _cache = ""
    return _cache


def lessons() -> str:
    """The self-written lessons ledger (mtime-cached: daemons are long-lived
    and the ledger is regenerated after every cycle/session)."""
    global _lessons_cache
    try:
        mtime = _LESSONS_PATH.stat().st_mtime
    except OSError:
        return ""
    if _lessons_cache is None or _lessons_cache[0] != mtime:
        try:
            text = _LESSONS_PATH.read_text(encoding="utf-8")
            body_start = text.find("##")
            body = text[body_start:] if body_start >= 0 else text
            _lessons_cache = (mtime, body[:_LESSONS_MAX_CHARS])
        except OSError:
            return ""
    return _lessons_cache[1]


def prompt_suffix() -> str:
    """Digest + measured lessons, formatted for a check-in system prompt."""
    out = ""
    d = digest()
    if d:
        out += (
            "\n\nTRADING KNOWLEDGE BASE (condensed from the legends/strategy "
            "literature — ground your memo and amendments in it):\n" + d
        )
    lessons_text = lessons()
    if lessons_text:
        out += (
            "\n\nMEASURED LESSONS (mined from THIS system's own backtests and "
            "live paper trades — hard evidence, weight it above the "
            "literature when they conflict):\n" + lessons_text
        )
    return out
