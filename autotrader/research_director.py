"""Research director: nightly Kimi K3 deep-dive over the full evidence base.

Runs in the daytrade daemon after post-mortem + edge-model training, before
the evolution sweep. Where the per-generation check-in reacts to one cycle,
the director sees everything at once: registry aggregates, post-mortem
oracle-gap reports, edge-model calibration, hunter gate rejects, the lessons
ledger, and the live desk's recent P&L — and returns structured directives:

  - ``hypotheses``     -> tested on the labeled setup library
                          (``hypotheses.evaluate_hypotheses``, Bonferroni)
  - search-knob amendments -> validated by ``checkin.validate_amendments``
                          (same hard limits as cycle check-ins)
  - ``hunter_gate_proposals`` -> STAGED to ``pending_gate_proposals.json``
                          for human review; never auto-applied to live config
  - ``braket_tasks``   -> queued to ``state/quantum/queue.json`` for the
                          weekly quantum runner

Every run is audited to ``state/research_director/YYYY-MM-DD.json``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any

from autotrader.bootstrap import state_dir
from autotrader.checkin import objective_text, validate_amendments
from autotrader.config import PipelineConfig
from autotrader.knowledge import prompt_suffix
from autotrader.llm import chat, extract_json_block

logger = logging.getLogger("autotrader.research_director")

LIVE_STATE = Path(__file__).resolve().parents[1] / "livetrader" / "state"

DIRECTOR_PROMPT = (
    "You are the chief research director of an autonomous day-trading "
    "research program with a $1,000 paper book. Once per night you receive "
    "a consolidated evidence pack: the EXACT gate the search is graded on "
    "(`objective`), GA registry aggregates, post-mortem oracle-gap reports "
    "(money the perfect trader could have made vs what we captured), "
    "edge-model calibration metrics, hunter gate-reject counts, the lessons "
    "ledger tail, and recent live paper P&L.\n"
    "Think like a quant head of research: find the binding constraint, name "
    "the single most promising pattern to attack next, and be brutally "
    "honest about what is NOT working. Then reply with a JSON object (fenced "
    "code block) with these keys:\n"
    "- memo: <=8 sentences, plain English, no hype.\n"
    "- hypotheses: <=10 feature-predicate objects (e.g. {\"gap\": [0.05, "
    "0.10], \"catalyst\": 1, \"tod_min\": [0, 90]}); each is tested on the "
    "labeled setup library under Bonferroni correction.\n"
    "- amendments: bounded search knobs (mutation_sigma 0.05-0.6, "
    "population_size 8-128, llm_proposals_per_gen 0-4, equity_universe_size "
    "10-120, crypto_universe_size 4-30, penny_universe_size 5-40, "
    "gene_bounds within the hard bounds given).\n"
    "- hunter_gate_proposals: <=3 objects {knob, current, proposed, "
    "rationale} for live hunter gates (e.g. hunter_max_ext_pct, "
    "hunter_prime_end_et, hunter_require_buzz). These are STAGED for human "
    "review, never auto-applied — propose only with evidence.\n"
    "- priority_patterns: ranked list of <=5 pattern buckets to attack "
    "(archetype, time-of-day, symbol class), each with why.\n"
    "- braket_tasks: <=2 quantum-job requests for the weekly Braket queue. "
    "The ONLY valid types are: \"qubo_feature_selection\" (pick the k most "
    "predictive edge features), \"qubo_allocation\" (split the $1,000 book "
    "across sleeves/signals), \"kernel_benchmark\" (quantum vs classical "
    "kernel on the edge labels). Format: {type: <one of those>, note: "
    "\"why now\"}. Request only with evidence classical search is stuck.\n"
    "Never claim performance the data does not show. If every metric is "
    "red, say what you would STOP doing, not just what to start."
)


def _registry_aggregates(registry: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        cur = registry._conn.execute(  # noqa: SLF001 — read-only aggregate
            "SELECT COUNT(*) FROM strategies"
        )
        out["strategies"] = int(cur.fetchone()[0])
        cur = registry._conn.execute(
            "SELECT sleeve, COUNT(*), COALESCE(SUM(gate_passed),0), "
            "MAX(oos_pass_rate), AVG(oos_mean_window_ret), "
            "COALESCE(SUM(trade_count),0) FROM results WHERE ok=1 "
            "GROUP BY sleeve"
        )
        out["per_sleeve"] = [
            {
                "sleeve": r[0], "results": r[1], "gate_passers": int(r[2]),
                "best_oos_green": round(float(r[3] or 0.0), 4),
                "mean_daily_ret": round(float(r[4] or 0.0), 5),
                "trades": int(r[5]),
            }
            for r in cur.fetchall()
        ]
    except Exception:  # noqa: BLE001 — evidence pack degrades, never dies
        logger.debug("registry aggregate failed", exc_info=True)
    return out


def _postmortem_tail(n: int = 5) -> list[dict[str, Any]]:
    pm_dir = state_dir() / "postmortem"
    if not pm_dir.exists():
        return []
    out = []
    for path in sorted(pm_dir.glob("*.json"))[-n:]:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "date": d.get("date"),
            "oracle_total_usd": (d.get("oracle") or {}).get("oracle_total_usd"),
            "actual_total_usd": (d.get("oracle") or {}).get("actual_total_usd"),
            "capture_rate": (d.get("oracle") or {}).get("capture_rate"),
            "ga_best_day_ret": (d.get("ga") or {}).get("best_day_ret"),
            "archetype_counts": (d.get("archetypes") or {}).get("counts"),
            "midday_pump_fade_rate": (d.get("archetypes") or {}).get(
                "midday_pump_fade_rate"
            ),
            "buzz_lead_lag": (d.get("buzz_lead_lag") or {}).get("by_source"),
        })
    return out


def _edge_summary() -> dict[str, Any]:
    path = state_dir() / "edge_model.json"
    if not path.exists():
        return {}
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: m.get(k) for k in (
        "trained_at", "n_train", "n_val", "brier_val", "brier_val_raw",
        "brier_base", "usable", "base_rate", "live_brier", "sizing_enabled",
        "label_stats", "calibration_bins",
    )}


def _gate_rejects_tail(days: int = 5) -> dict[str, int]:
    from livetrader.engine.snapshots import gate_reject_counts

    totals: dict[str, int] = {}
    today = dt.datetime.now().date()
    for i in range(1, days + 3):
        day = (today - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            for reason, n in gate_reject_counts(day).items():
                totals[reason] = totals.get(reason, 0) + n
        except Exception:  # noqa: BLE001
            continue
    return dict(sorted(totals.items(), key=lambda kv: -kv[1]))


def _lessons_tail(n: int = 10) -> list[dict[str, Any]]:
    path = state_dir() / "lessons.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            row = json.loads(line)
            out.append({"kind": row.get("kind"), "text": str(row.get("text"))[:300]})
        except json.JSONDecodeError:
            continue
    return out


def _daily_pnl_tail(n: int = 7) -> list[dict[str, Any]]:
    path = LIVE_STATE / "daily_pnl.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _hunter_knobs() -> dict[str, Any]:
    try:
        from livetrader.config import ArenaConfig

        cfg = ArenaConfig()
        return {
            k: getattr(cfg, k)
            for k in dir(cfg)
            if k.startswith("hunter_") or k.startswith("short_borrow")
        }
    except Exception:  # noqa: BLE001
        return {}


def build_evidence_pack(
    cfg: PipelineConfig, state: dict[str, Any], registry: Any,
) -> dict[str, Any]:
    champ = state.get("champion") or {}
    return {
        "generated": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "objective": objective_text(cfg, state),
        "cycle": state.get("cycle"),
        "simulated_trades_total": state.get("simulated_trades_total"),
        "champion": {
            "gid": champ.get("gid"),
            "sleeve": champ.get("sleeve"),
            "gate_passed": champ.get("gate_passed"),
            "oos_pass_rate": champ.get("oos_pass_rate"),
            "oos_mean_window_ret": champ.get("oos_mean_window_ret"),
            "max_drawdown": champ.get("max_drawdown"),
        },
        "registry": _registry_aggregates(registry),
        "postmortems": _postmortem_tail(),
        "edge_model": _edge_summary(),
        "hunter_gate_rejects_5d": _gate_rejects_tail(),
        "hunter_knobs": _hunter_knobs(),
        "lessons_tail": _lessons_tail(),
        "live_daily_pnl": _daily_pnl_tail(),
    }


def _director_dir() -> Path:
    d = state_dir() / "research_director"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stage_gate_proposals(proposals: list[dict[str, Any]]) -> None:
    if not proposals:
        return
    path = _director_dir() / "pending_gate_proposals.json"
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    stamped = [
        {**p, "proposed_at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
         "status": "pending_review"}
        for p in proposals if isinstance(p, dict)
    ]
    path.write_text(
        json.dumps((existing + stamped)[-100:], indent=2, default=str),
        encoding="utf-8",
    )


def _queue_braket_tasks(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        return
    qdir = state_dir() / "quantum"
    qdir.mkdir(parents=True, exist_ok=True)
    path = qdir / "queue.json"
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    _VALID_TYPES = {"qubo_feature_selection", "qubo_allocation", "kernel_benchmark"}
    stamped = [
        {**t, "queued_at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
         "status": "queued"}
        for t in tasks
        if isinstance(t, dict) and t.get("type") in _VALID_TYPES
    ]
    path.write_text(
        json.dumps((existing + stamped)[-50:], indent=2, default=str),
        encoding="utf-8",
    )


def run_research_director(
    cfg: PipelineConfig,
    state: dict[str, Any],
    registry: Any,
) -> dict[str, Any]:
    """One nightly director pass. Always writes an audit record."""
    record: dict[str, Any] = {
        "ts": time.time(),
        "cycle": state.get("cycle"),
        "memo": "",
        "amendments": {},
        "hypotheses": [],
        "hypothesis_results": [],
        "hunter_gate_proposals": [],
        "priority_patterns": [],
        "braket_tasks": [],
    }
    pack = build_evidence_pack(cfg, state, registry)
    record["evidence_pack"] = pack

    reply = chat(
        cfg.llm,
        DIRECTOR_PROMPT + prompt_suffix(),
        json.dumps(pack, default=str),
        max_tokens=3000,
    )
    directives: dict[str, Any] = {}
    if reply:
        record["memo"] = reply
        parsed = extract_json_block(reply)
        if isinstance(parsed, dict):
            directives = parsed
    else:
        record["memo"] = "(no LLM provider reachable; director pass skipped)"

    amendments = validate_amendments(directives.get("amendments"))
    amendments.pop("hypotheses", None)
    record["amendments"] = amendments

    hyps = directives.get("hypotheses")
    if isinstance(hyps, list):
        record["hypotheses"] = [h for h in hyps[:10] if isinstance(h, dict)]
        if record["hypotheses"]:
            try:
                from autotrader.hypotheses import evaluate_hypotheses

                record["hypothesis_results"] = evaluate_hypotheses(
                    record["hypotheses"]
                )
            except Exception:  # noqa: BLE001
                logger.exception("director hypothesis evaluation failed")

    proposals = directives.get("hunter_gate_proposals")
    if isinstance(proposals, list):
        record["hunter_gate_proposals"] = [
            p for p in proposals[:3] if isinstance(p, dict)
        ]
        _stage_gate_proposals(record["hunter_gate_proposals"])

    patterns = directives.get("priority_patterns")
    if isinstance(patterns, list):
        record["priority_patterns"] = patterns[:5]

    tasks = directives.get("braket_tasks")
    if isinstance(tasks, list):
        record["braket_tasks"] = [t for t in tasks[:2] if isinstance(t, dict)]
        _queue_braket_tasks(record["braket_tasks"])

    day = dt.datetime.now().strftime("%Y-%m-%d")
    path = _director_dir() / f"{day}.json"
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    logger.info(
        "[director] cycle %s -> memo %d chars, %d amendments, %d hypotheses, "
        "%d gate proposals, audit %s",
        state.get("cycle"), len(record["memo"]), len(amendments),
        len(record["hypotheses"]), len(record["hunter_gate_proposals"]), path,
    )
    return record
