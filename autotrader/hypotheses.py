"""Hypothesis loop: LLM proposes feature buckets; we test them on labeled data.

A hypothesis is a small predicate over setup features, e.g.::

    {"gap": [0.05, 0.10], "catalyst": 1, "tod_min": [0, 90]}

The tester evaluates expectancy (label rate / EV proxy) vs the library
baseline. Only survivors (min 50 samples, expectancy > baseline + 2pp)
are written to the lessons ledger.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from autotrader.bootstrap import state_dir
from autotrader.edge_model import load_labeled
from autotrader.lessons import append_lesson

logger = logging.getLogger("autotrader.hypotheses")

MIN_SAMPLES = 50
MAX_PER_CYCLE = 10
EDGE_OVER_BASELINE = 0.02
FAMILY_ALPHA = 0.05
_STATE_NAME = "hypotheses_state.json"


def _load_test_count() -> int:
    try:
        return int(json.loads(
            (state_dir() / _STATE_NAME).read_text(encoding="utf-8")
        ).get("tests_run", 0))
    except Exception:  # noqa: BLE001 — missing/corrupt state starts fresh
        return 0


def _save_test_count(n: int) -> None:
    try:
        (state_dir() / _STATE_NAME).write_text(
            json.dumps({"tests_run": n}), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 — bookkeeping must not break the loop
        logger.debug("hypotheses state write failed", exc_info=True)


def _binom_pvalue(hit_rate: float, base_rate: float, n: int) -> float:
    """One-sided normal-approx p-value for hit_rate > base_rate given n."""
    if n <= 0 or not 0.0 < base_rate < 1.0:
        return 1.0
    var = base_rate * (1.0 - base_rate) / n
    if var <= 0.0:
        return 1.0
    z = (hit_rate - base_rate) / math.sqrt(var)
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _match(row: dict[str, Any], hyp: dict[str, Any]) -> bool:
    for key, rule in hyp.items():
        if key in ("name", "note", "side"):
            continue
        # Map aliases
        field = {
            "tod_min": "minutes_open",
            "ext": "ext_vwap",
        }.get(key, key)
        val = row.get(field)
        if field == "catalyst":
            val = 1.0 if row.get("catalyst") else float(row.get("catalyst", 0) or 0)
        if isinstance(rule, (list, tuple)) and len(rule) == 2:
            lo, hi = float(rule[0]), float(rule[1])
            try:
                if not (lo <= float(val or 0.0) <= hi):
                    return False
            except (TypeError, ValueError):
                return False
        else:
            try:
                if float(val or 0.0) != float(rule):
                    return False
            except (TypeError, ValueError):
                if val != rule:
                    return False
    side = hyp.get("side")
    if side and str(row.get("side", "long")) != str(side):
        return False
    return True


def test_hypothesis(
    hyp: dict[str, Any],
    rows: list[dict[str, Any]] | None = None,
    *,
    tests_run: int = 0,
) -> dict[str, Any]:
    """Test one hypothesis against labeled data.

    Survivors must clear BOTH a practical-significance floor
    (EDGE_OVER_BASELINE) and a Bonferroni-corrected binomial p-value —
    the more hypotheses we have ever tested, the tighter the alpha.
    """
    rows = rows if rows is not None else load_labeled()
    if not rows:
        return {"ok": False, "reason": "no_labeled_rows", "hypothesis": hyp}
    matched = [r for r in rows if _match(r, hyp)]
    n = len(matched)
    base_rate = sum(int(r.get("label", 0)) for r in rows) / len(rows)
    if n < MIN_SAMPLES:
        return {
            "ok": False, "reason": "insufficient_samples",
            "n": n, "min": MIN_SAMPLES, "hypothesis": hyp,
        }
    hit = sum(int(r.get("label", 0)) for r in matched) / n
    alpha = FAMILY_ALPHA / max(1, tests_run)
    pvalue = _binom_pvalue(hit, base_rate, n)
    survivor = hit >= base_rate + EDGE_OVER_BASELINE and pvalue < alpha
    return {
        "ok": survivor,
        "n": n,
        "hit_rate": round(hit, 4),
        "baseline": round(base_rate, 4),
        "edge": round(hit - base_rate, 4),
        "pvalue": round(pvalue, 6),
        "alpha": round(alpha, 6),
        "hypothesis": hyp,
    }


def evaluate_hypotheses(hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Test up to MAX_PER_CYCLE hypotheses; write survivors to the ledger."""
    if not hypotheses:
        return []
    rows = load_labeled()
    results = []
    tests_run = _load_test_count()
    for hyp in hypotheses[:MAX_PER_CYCLE]:
        if not isinstance(hyp, dict):
            continue
        tests_run += 1
        res = test_hypothesis(hyp, rows, tests_run=tests_run)
        results.append(res)
        if res.get("ok"):
            text = (
                f"Hypothesis survivor: {hyp} → hit_rate {res['hit_rate']:.1%} "
                f"vs baseline {res['baseline']:.1%} "
                f"(edge {res['edge']:+.1%}, n={res['n']})"
            )
            try:
                append_lesson({
                    "source": "hypothesis",
                    "kind": "hypothesis_survivor",
                    "text": text,
                    "evidence": res,
                })
            except Exception:  # noqa: BLE001
                logger.exception("failed to append hypothesis lesson")
            logger.info("hypothesis SURVIVED: %s", text)
        else:
            logger.info(
                "hypothesis rejected: %s (%s)",
                hyp, res.get("reason") or f"edge={res.get('edge')}",
            )
    _save_test_count(tests_run)
    return results
