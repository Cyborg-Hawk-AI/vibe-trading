"""TradingAgents committee review of gate finalists (Pillar 3).

Runs the multi-agent debate pipeline (``TradingAgentsGraph.propagate``) on
sampled historical decision dates for a finalist strategy's most-traded
symbols, and records the committee's BUY/SELL/HOLD verdicts in the registry.

Point-in-time safety: only the ``market``, ``news``, and ``fundamentals``
analysts are enabled. The ``social`` analyst is excluded because its data
sources (StockTwits/Reddit/Polymarket) fetch live state and would leak the
future into a historical review.

Prompt mutation: TradingAgents hardcodes agent prompts inline, but every
agent appends ``get_language_instruction()`` to its system message. We
monkeypatch that function — in ``agent_utils`` and in every agent module that
imported it by name — to also append a guidance overlay compiled from the
current failure taxonomy (``autotrader/prompts/overlays.json``). Prompts are
assembled inside the node functions at invocation time, so the patch takes
effect without rebuilding the graph.

This module degrades gracefully: if TradingAgents' heavy dependencies
(langchain/langgraph) or an LLM key are missing, reviews are skipped with an
explanatory status instead of failing the pipeline.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from autotrader.bootstrap import ensure_tradingagents_path
from autotrader.registry import Registry

logger = logging.getLogger(__name__)

PIT_SAFE_ANALYSTS = ("market", "news", "fundamentals")
OVERLAYS_PATH = Path(__file__).parent / "prompts" / "overlays.json"

_AGENT_MODULES = (
    "tradingagents.agents.utils.agent_utils",
    "tradingagents.agents.analysts.market_analyst",
    "tradingagents.agents.analysts.news_analyst",
    "tradingagents.agents.analysts.fundamentals_analyst",
    "tradingagents.agents.analysts.sentiment_analyst",
    "tradingagents.agents.researchers.bull_researcher",
    "tradingagents.agents.researchers.bear_researcher",
    "tradingagents.agents.managers.research_manager",
    "tradingagents.agents.managers.portfolio_manager",
    "tradingagents.agents.trader.trader",
    "tradingagents.agents.risk_mgmt.aggressive_debator",
    "tradingagents.agents.risk_mgmt.conservative_debator",
    "tradingagents.agents.risk_mgmt.neutral_debator",
)


def compile_overlay(failure_counts: dict[str, int], top_n: int = 3) -> str:
    """Build the prompt overlay text from the dominant failure modes."""
    if not failure_counts:
        return ""
    overlays = json.loads(OVERLAYS_PATH.read_text(encoding="utf-8"))
    lines = []
    for mode, _count in sorted(failure_counts.items(), key=lambda kv: -kv[1])[:top_n]:
        text = overlays.get(mode)
        if text:
            lines.append(f"- {text}")
    if not lines:
        return ""
    return (
        "\n\nIMPORTANT — lessons from recent systematic simulations of this "
        "strategy family:\n" + "\n".join(lines)
    )


def apply_prompt_overlay(overlay: str) -> None:
    """Monkeypatch ``get_language_instruction`` across all agent modules.

    Idempotent: re-applying replaces the previous overlay rather than
    stacking. The original function is stashed on first patch.
    """
    import importlib

    ensure_tradingagents_path()
    agent_utils = importlib.import_module(_AGENT_MODULES[0])
    original = getattr(agent_utils, "_autotrader_original_gli", None)
    if original is None:
        original = agent_utils.get_language_instruction
        agent_utils._autotrader_original_gli = original

    def patched() -> str:
        return original() + overlay

    for module_name in _AGENT_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if hasattr(module, "get_language_instruction"):
            module.get_language_instruction = patched


def _committee_ticker(code: str) -> tuple[str, str]:
    """Map a loader symbol to a (TradingAgents ticker, asset_type) pair."""
    if code.upper().endswith(".US"):
        return code[:-3], "stock"
    if code.upper().endswith("-USDT"):
        return code[:-5] + "-USD", "crypto"
    return code, "stock"


def review_finalist(
    gid: str,
    sleeve: str,
    result_dict: dict[str, Any],
    failure_counts: dict[str, int],
    registry: Registry,
    *,
    symbols: list[str],
    max_reviews: int = 3,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run committee reviews for one finalist. Returns a status summary."""
    ensure_tradingagents_path()
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except ImportError as exc:
        msg = f"TradingAgents unavailable ({exc}); committee review skipped"
        logger.warning(msg)
        return {"status": "skipped", "reason": msg}

    import os

    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("TRADINGAGENTS_LLM_PROVIDER")):
        msg = "no LLM provider configured for TradingAgents; committee review skipped"
        logger.warning(msg)
        return {"status": "skipped", "reason": msg}

    overlay = compile_overlay(failure_counts)
    apply_prompt_overlay(overlay)

    config = dict(DEFAULT_CONFIG)
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1
    if config_overrides:
        config.update(config_overrides)

    graph = TradingAgentsGraph(
        selected_analysts=list(PIT_SAFE_ANALYSTS),
        debug=False,
        config=config,
    )

    windows = result_dict.get("windows", [])
    oos = [w for w in windows if w.get("is_oos")]
    chosen = [w for w in oos if w.get("passed")] or oos
    if not chosen:
        return {"status": "skipped", "reason": "no OOS windows to review"}
    step = max(len(chosen) // max_reviews, 1)
    dates = [w["start"] for w in chosen[::step]][:max_reviews]

    reviews = []
    review_symbols = symbols[:2] if symbols else []
    for code in review_symbols:
        ticker, asset_type = _committee_ticker(code)
        for date in dates[: max(max_reviews // max(len(review_symbols), 1), 1)]:
            try:
                final_state, decision = graph.propagate(ticker, date, asset_type=asset_type)
                report = {
                    "final_trade_decision": str(
                        final_state.get("final_trade_decision", "")
                    )[:4000],
                }
                registry.record_committee_review(
                    gid, sleeve, ticker, date, str(decision), report
                )
                reviews.append({"ticker": ticker, "date": date, "decision": str(decision)})
                logger.info("[committee] %s %s %s -> %s", gid, ticker, date, decision)
            except Exception as exc:  # noqa: BLE001 — a failed review must not kill the run
                logger.warning("[committee] review failed for %s %s: %s", ticker, date, exc)
                reviews.append({"ticker": ticker, "date": date, "error": str(exc)[:300]})

    return {"status": "done", "overlay_applied": bool(overlay), "reviews": reviews}


def main(argv: list[str] | None = None) -> int:
    """CLI: review current gate passers (or top leaderboard entries).

    Usage: python -m autotrader.committee [--sleeve equities] [--top 1]
    """
    import argparse

    parser = argparse.ArgumentParser(description="TradingAgents committee review")
    parser.add_argument("--sleeve", default="equities", choices=["equities", "crypto"])
    parser.add_argument("--top", type=int, default=1, help="review top-N leaderboard entries")
    parser.add_argument("--max-reviews", type=int, default=3)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    registry = Registry()
    rows = registry.gate_passers(args.sleeve) or registry.leaderboard(args.sleeve, args.top)
    if not rows:
        print("No strategies in the registry; run the optimize loop first.")
        return 1

    from autotrader.universe import get_universe
    from autotrader.config import PipelineConfig

    cfg = PipelineConfig()
    symbols = get_universe(
        cfg.data.equity_universe if args.sleeve == "equities" else cfg.data.crypto_universe,
        4,
    )

    for row in rows[: args.top]:
        # Rebuild window details from the stored result for date sampling.
        import sqlite3

        conn = sqlite3.connect(str(registry.path))
        raw = conn.execute(
            "SELECT windows_json, diagnostics_json FROM results WHERE gid=? AND sleeve=?",
            (row["gid"], args.sleeve),
        ).fetchone()
        conn.close()
        windows = json.loads(raw[0]) if raw and raw[0] else []
        diag = json.loads(raw[1]) if raw and raw[1] else {}
        status = review_finalist(
            row["gid"],
            args.sleeve,
            {"windows": windows},
            diag.get("failure_counts", {}),
            registry,
            symbols=symbols,
            max_reviews=args.max_reviews,
        )
        print(json.dumps({"gid": row["gid"], **status}, indent=2))
    registry.close()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
