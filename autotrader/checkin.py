"""After-run check-in: report results to Kimi-K3/Bedrock and collect amendments.

After every evolution cycle the system sends the LLM a structured account of
what happened (fitness trajectory, failure taxonomy, leaderboard delta,
attribution highlights) and asks for two things:

  1. a human-readable memo (what worked, what failed, why), and
  2. a bounded JSON amendment proposal for the next cycle.

Amendments are validated by ``validate_amendments`` before anything is
applied: the LLM may tune search knobs (mutation sigma, population size,
LLM-proposal count, gene bounds within hard limits, universe sizes) but can
NEVER touch friction floors, risk caps, capital, or the acceptance gate.
Rejected fields are logged and dropped, never applied.

Every check-in is persisted to ``autotrader/state/checkins/`` so the daemon
(and the user) can audit the full conversation history.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from autotrader.bootstrap import state_dir
from autotrader.config import PipelineConfig
from autotrader.knowledge import prompt_suffix
from autotrader.llm import chat, extract_json_block
from autotrader.strategy_space import HARD_GENE_SPACE

logger = logging.getLogger(__name__)

CHECKIN_SYSTEM_PROMPT = (
    "You are the research director of an autonomous trading-strategy search "
    "system. You receive the results of one evolution cycle: per-strategy "
    "out-of-sample statistics, a failure-mode histogram from losing "
    "trades, the current leaderboard, and attribution notes on winning "
    "symbols. The payload's `objective` field states the EXACT gate the "
    "search is graded on — that is the only bar that matters; never grade "
    "or advise against any other benchmark. Your job:\n"
    "1. MEMO: in <=8 sentences, explain what the search learned this cycle, "
    "what the dominant failure modes imply, and which strategy directions "
    "look most promising for the stated objective. If the payload shows "
    "zero gate-passers across many cycles, say so plainly and attack the "
    "binding constraint (the gate criterion furthest from being met).\n"
    "2. AMENDMENTS: propose bounded adjustments for the next cycle as a JSON "
    "object with any of these keys: mutation_sigma (0.05-0.6), "
    "population_size (8-128), llm_proposals_per_gen (0-4), "
    "equity_universe_size (10-120), crypto_universe_size (4-30), "
    "penny_universe_size (5-40), gene_bounds (map of gene name to [low, "
    "high] within the hard bounds you were given), focus_failures (list of "
    "failure modes to bias against), notes (string), and optionally "
    "hypotheses (list of at most 10 feature-predicate objects like "
    "{\"gap\": [0.05, 0.10], \"catalyst\": 1, \"tod_min\": [0, 90]} — "
    "each will be tested on the labeled setup library under a Bonferroni "
    "correction; only survivors become lessons).\n"
    "Reply with the memo first, then the JSON object in a fenced code block. "
    "Be a rigorous skeptic: call out overfitting risk when train/OOS gaps "
    "are large, and never claim performance the data does not show."
)


def objective_text(cfg: PipelineConfig, state: dict[str, Any] | None = None) -> str:
    """Plain-language statement of the gate the current search is graded on.

    Intraday profiles (5m/15m intervals) are graded on the daytrade session
    gate; daily-bar profiles on the rolling 2-week window gate. Included in
    every check-in payload so the LLM is never graded against a stale
    objective.
    """
    intervals = {str(i) for i in (getattr(cfg.data, "intervals", ()) or ())}
    if intervals and intervals <= {"5m", "15m"}:
        g = cfg.daytrade_gate
        text = (
            "DAYTRADE SESSION GATE — a strategy passes only if, on HELD-OUT "
            f"sessions (last {g.oos_fraction:.0%} of sessions), it is net "
            f"positive on >= {g.min_green_rate:.0%} of them, mean session net "
            f"return >= {g.min_mean_daily_ret:.2%}, intraday session "
            f"drawdown <= {g.session_max_drawdown:.0%}, account drawdown <= "
            f"{g.max_drawdown:.0%}, and >= {g.min_trades_per_session:.0f} "
            "trades per session. In the result rows, oos_pass_rate = "
            "fraction of held-out sessions net positive and "
            "oos_mean_window_ret = mean per-session net return."
        )
        if state:
            rung = int(state.get("ladder_rung", 0))
            text += (
                f" Ladder rung {rung}/{len(g.ladder) - 1}; the champion has "
                f"held this rung for {int(state.get('cycles_at_rung', 0))} of "
                f"{g.cycles_per_rung} cycles needed to escalate to a harder "
                "target. Drawdown caps never loosen."
            )
        return text
    g = cfg.gate
    return (
        "DAILY WINDOW GATE — a strategy passes only if it makes >= "
        f"{g.target_window_return:.0%} net in >= {g.min_window_pass_rate:.0%} "
        f"of rolling {g.window_trading_days}-day held-out windows with "
        f"account max drawdown <= {g.max_drawdown:.0%}. The legacy "
        f"{g.stretch_target_return:.0%}/{g.stretch_min_pass_rate:.0%} bar is "
        "an informational stretch diagnostic only and must not block "
        "promotion."
    )

# Hard limits the LLM cannot exceed regardless of what it proposes.
_AMENDMENT_LIMITS: dict[str, tuple[float, float]] = {
    "mutation_sigma": (0.05, 0.6),
    "population_size": (8, 128),
    "llm_proposals_per_gen": (0, 4),
    "equity_universe_size": (10, 120),
    "crypto_universe_size": (4, 30),
    "penny_universe_size": (5, 40),
}
_INT_FIELDS = {
    "population_size", "llm_proposals_per_gen",
    "equity_universe_size", "crypto_universe_size", "penny_universe_size",
}


def checkins_dir() -> Path:
    d = state_dir() / "checkins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_cycle_payload(
    sleeve: str,
    cycle: int,
    results: list[dict[str, Any]],
    failure_counts: dict[str, int],
    leaderboard: list[dict[str, Any]],
    attribution: list[dict[str, Any]] | None = None,
    objective: str | None = None,
) -> dict[str, Any]:
    """Compact, JSON-serializable account of one cycle for the LLM."""
  
    def _slim(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "gid": r.get("gid"),
            "train_pass_rate": r.get("train_pass_rate"),
            "train_mean_window_ret": r.get("train_mean_window_ret"),
            "train_sharpe": r.get("train_sharpe"),
        }
      
    return {
        "sleeve": sleeve,
        "cycle": cycle,
        "objective": objective or "",
        "results": [_slim(r) for r in results],
        "failure_counts": {},
        "leaderboard_top5": [
            _slim(r) for r in results[:5]
        ],
        "attribution": [],
        "gene_space_hard_bounds": {
            name: [low, high] for name, (low, high, _k) in HARD_GENE_SPACE.items()
        },
    }


def validate_amendments(raw: Any) -> dict[str, Any]:
    """Clamp/filter an LLM amendment proposal to the safe schema.

    Returns only the accepted fields. Anything outside the schema or the
    hard limits is dropped (logged), never applied.
    """
    if not isinstance(raw, dict):
        return {}
    accepted: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "notes" and isinstance(value, str):
            accepted["notes"] = value[:2000]
        elif key == "focus_failures" and isinstance(value, list):
            accepted["focus_failures"] = [str(v) for v in value[:5]]
        elif key == "gene_bounds" and isinstance(value, dict):
            bounds: dict[str, list[float]] = {}
            for gene, rng in value.items():
                if gene not in HARD_GENE_SPACE or not isinstance(rng, (list, tuple)) or len(rng) != 2:
                    logger.info("amendment rejected: gene_bounds[%r]", gene)
                    continue
                hard_low, hard_high, _kind = HARD_GENE_SPACE[gene]
                try:
                    low = max(float(rng[0]), hard_low)
                    high = min(float(rng[1]), hard_high)
                except (TypeError, ValueError):
                    continue
                if low < high:
                    bounds[gene] = [low, high]
            if bounds:
                accepted["gene_bounds"] = bounds
        elif key == "hypotheses" and isinstance(value, list):
            hyps = [h for h in value[:10] if isinstance(h, dict)]
            if hyps:
                accepted["hypotheses"] = hyps
        elif key in _AMENDMENT_LIMITS:
            lo, hi = _AMENDMENT_LIMITS[key]
            try:
                v = float(value)
            except (TypeError, ValueError):
                logger.info("amendment rejected (non-numeric): %s=%r", key, value)
                continue
            v = min(max(v, lo), hi)
            accepted[key] = int(round(v)) if key in _INT_FIELDS else v
        else:
            logger.info("amendment rejected (unknown/forbidden key): %r", key)
    return accepted


def checkin_cycle(
    cfg: PipelineConfig,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send one cycle's results to the LLM; return memo + validated amendments.

    Always persisted to state/checkins/ (including the no-LLM case, so the
    audit trail shows the cycle happened).
    """
    record: dict[str, Any] = {
        "ts": time.time(),
        "sleeve": payload.get("sleeve"),
        "cycle": payload.get("cycle"),
        "payload": payload,
        "memo": "",
        "amendments": {},
        "provider_used": None,
    }
    reply = chat(
        cfg.llm,
        CHECKIN_SYSTEM_PROMPT + prompt_suffix(),
        json.dumps(payload, default=str),
        max_tokens=1600,
    )
    if reply:
        record["memo"] = reply
        validated = validate_amendments(extract_json_block(reply))
        # Hypotheses are tested, not applied as config amendments.
        hyps = validated.pop("hypotheses", None)
        record["amendments"] = validated
        record["hypotheses"] = hyps or []
        record["provider_used"] = "llm"
        if hyps:
            try:
                from autotrader.hypotheses import evaluate_hypotheses

                record["hypothesis_results"] = evaluate_hypotheses(hyps)
            except Exception:  # noqa: BLE001
                logger.exception("hypothesis evaluation failed")
    else:
        record["memo"] = "(no LLM provider reachable; cycle ran deterministically)"
        record["hypotheses"] = []

    path = checkins_dir() / (
        f"{payload.get('sleeve','all')}_cycle{payload.get('cycle',0):04d}_{int(record['ts'])}.json"
    )
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    logger.info(
        "[checkin] %s cycle %s -> memo %d chars, %d amendments accepted",
        payload.get("sleeve"), payload.get("cycle"),
        len(record["memo"]), len(record["amendments"]),
    )
    return record
