"""Quantum task runner: executes the research director's Braket queue.

Queue: ``autotrader/state/quantum/queue.json`` (entries stamped by
research_director). Results land in ``state/quantum/results/`` and every run
is cost-gated against the ``vibe-trading-braket`` monthly budget ($50).

    python -m autotrader.quantum.runner                 # drain the queue
    python -m autotrader.quantum.runner --task kernel_benchmark  # ad-hoc
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from autotrader.bootstrap import state_dir

logger = logging.getLogger("autotrader.quantum.runner")

BUDGET_NAME = "vibe-trading-braket"
BUDGET_USD = 50.0
SV1_TASK_USD = 0.075
SV1_SHOT_USD = 0.000275


def _queue_path() -> Path:
    d = state_dir() / "quantum"
    d.mkdir(parents=True, exist_ok=True)
    return d / "queue.json"


def _results_dir() -> Path:
    d = state_dir() / "quantum" / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_queue() -> list[dict[str, Any]]:
    path = _queue_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save_queue(q: list[dict[str, Any]]) -> None:
    _queue_path().write_text(json.dumps(q, indent=2, default=str), encoding="utf-8")


def _braket_spend_pct() -> float | None:
    try:
        import boto3

        resp = boto3.client("budgets", region_name="us-east-1").describe_budget(
            AccountId="465768367549", BudgetName=BUDGET_NAME
        )
        b = resp["Budget"]
        limit = float(b["BudgetLimit"]["Amount"])
        spend = float(b["CalculatedSpend"]["ActualSpend"]["Amount"])
        return 100.0 * spend / limit if limit > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _est_cost(tasks: int, shots: int) -> float:
    return tasks * (SV1_TASK_USD + shots * SV1_SHOT_USD)


def _edge_xy() -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Feature matrix + labels from the edge-model labeled library."""
    from autotrader.edge_model import FEATURE_KEYS, _features_row, load_labeled

    rows = load_labeled()
    if not rows:
        raise RuntimeError("no labeled rows for quantum tasks")
    X = np.vstack([_features_row(r) for r in rows])
    y = np.array([int(r.get("label", 0)) for r in rows], dtype=float)
    return X, y, list(FEATURE_KEYS)


def run_qubo_feature_selection(*, k: int = 4, qaoa: bool = True) -> dict[str, Any]:
    """Pick the k most informative edge features (QUBO; classical + QAOA bench)."""
    from autotrader.quantum.qubo import (
        build_feature_selection_qubo, solve_qubo_annealing, solve_qubo_qaoa_sv1,
    )

    X, y, names = _edge_xy()
    Q = build_feature_selection_qubo(X, y, k)
    classical = solve_qubo_annealing(Q)
    result: dict[str, Any] = {
        "type": "qubo_feature_selection", "k": k, "features": names,
        "classical": {
            "selected": [names[i] for i, b in enumerate(classical["x"]) if b],
            "energy": classical["energy"],
        },
    }
    if qaoa and len(names) <= 30:
        est = _est_cost(36, 1000)
        spend = _braket_spend_pct()
        if spend is not None and spend >= 80.0:
            result["qaoa"] = {"skipped": f"budget at {spend:.0f}%"}
        else:
            qaoa_res = solve_qubo_qaoa_sv1(
                Q, p=2, shots=1000, angle_steps=6,
                s3_folder=("amazon-braket-465768367549-results", "qubo-features"),
            )
            result["qaoa"] = {
                "selected": [names[i] for i, b in enumerate(qaoa_res["x"]) if b],
                "energy": qaoa_res["energy"],
                "tasks_run": qaoa_res["tasks_run"],
                "est_cost_usd": round(est, 2),
                "beats_classical": qaoa_res["energy"] <= classical["energy"],
            }
    return result


def run_qubo_allocation(*, k: int = 3, qaoa: bool = True) -> dict[str, Any]:
    """Allocate the $1,000 book across candidate books/signals (QUBO).

    Candidates: the three sleeves x (champion strategy) scored by measured
    edge (mean OOS daily return) and return covariance from the registry.
    """
    from autotrader.quantum.qubo import (
        build_allocation_qubo, solve_qubo_annealing, solve_qubo_qaoa_sv1,
    )

    edges, labels = _allocation_candidates()
    if len(edges) < 2:
        return {"type": "qubo_allocation", "skipped": "not enough candidates"}
    n = len(edges)
    cov = np.eye(n) * 0.01  # independent proxy until per-candidate series exist
    k_eff = min(k, n)
    Q = build_allocation_qubo(np.array(edges), cov, k_eff)
    classical = solve_qubo_annealing(Q)
    unit = 1000.0 / k_eff
    result: dict[str, Any] = {
        "type": "qubo_allocation", "unit_usd": unit,
        "candidates": labels,
        "classical": {
            "selected": [labels[i] for i, b in enumerate(classical["x"]) if b],
            "energy": classical["energy"],
            "allocation_usd": {
                labels[i]: unit for i, b in enumerate(classical["x"]) if b
            },
        },
    }
    if qaoa and n <= 30:
        est = _est_cost(36, 1000)
        spend = _braket_spend_pct()
        if spend is not None and spend >= 80.0:
            result["qaoa"] = {"skipped": f"budget at {spend:.0f}%"}
        else:
            qaoa_res = solve_qubo_qaoa_sv1(
                Q, p=2, shots=1000, angle_steps=6,
                s3_folder=("amazon-braket-465768367549-results", "qubo-alloc"),
            )
            result["qaoa"] = {
                "selected": [labels[i] for i, b in enumerate(qaoa_res["x"]) if b],
                "energy": qaoa_res["energy"],
                "est_cost_usd": round(est, 2),
                "beats_classical": qaoa_res["energy"] <= classical["energy"],
            }
    return result


def _allocation_candidates() -> tuple[list[float], list[str]]:
    """Measured edge per sleeve from the daytrade registry leaderboard."""
    from autotrader.registry import Registry

    edges: list[float] = []
    labels: list[str] = []
    try:
        reg = Registry(state_dir() / "daytrade_registry.db")
        for sleeve in ("equities", "crypto", "equities_penny"):
            board = reg.leaderboard(sleeve, limit=1)
            if board:
                r = board[0]
                edges.append(float(r.get("oos_mean_window_ret") or 0.0))
                labels.append(f"{sleeve}:{r.get('gid', '?')[:8]}")
        reg.close()
    except Exception:  # noqa: BLE001
        logger.exception("allocation candidates failed")
    return edges, labels


def run_kernel_benchmark(*, sv1_pairs: int = 3, sv1_shots: int = 1000) -> dict[str, Any]:
    """Quantum vs classical kernel on the edge labels (held-out Brier)."""
    from autotrader.edge_model import MIN_TRAIN
    from autotrader.quantum.kernel import benchmark_kernels

    X, y, names = _edge_xy()
    if len(y) < MIN_TRAIN:
        return {"type": "kernel_benchmark", "skipped": f"only {len(y)} rows"}
    # Chronological-ish split (rows arrive time-ordered).
    cut = int(len(y) * 0.8)
    spend = _braket_spend_pct()
    sv1_ok = sv1_pairs > 0 and (spend is None or spend < 80.0)
    res = benchmark_kernels(
        X[:cut], y[:cut], X[cut:], y[cut:],
        sv1_check_pairs=sv1_pairs if sv1_ok else 0, sv1_shots=sv1_shots,
    )
    res["type"] = "kernel_benchmark"
    res["n_train"] = int(cut)
    res["n_val"] = int(len(y) - cut)
    res["features"] = names
    res["est_sv1_cost_usd"] = round(_est_cost(sv1_pairs, sv1_shots), 3) if sv1_ok else 0.0
    return res


_TASKS = {
    "qubo_feature_selection": run_qubo_feature_selection,
    "qubo_allocation": run_qubo_allocation,
    "kernel_benchmark": run_kernel_benchmark,
}


def drain_queue() -> list[dict[str, Any]]:
    queue = _load_queue()
    results = []
    changed = False
    for entry in queue:
        if entry.get("status") != "queued":
            continue
        ttype = entry.get("type")
        fn = _TASKS.get(ttype)
        if fn is None:
            entry["status"] = "unknown_type"
            changed = True
            continue
        logger.info("running quantum task: %s", ttype)
        try:
            res = fn()
        except Exception as exc:  # noqa: BLE001 — one failure must not block the queue
            logger.exception("quantum task %s failed", ttype)
            res = {"type": ttype, "error": str(exc)}
        day = dt.datetime.now().strftime("%Y-%m-%d")
        out = _results_dir() / f"{day}_{ttype}.json"
        out.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        entry["status"] = "done"
        entry["result_file"] = str(out)
        results.append(res)
        changed = True
    if changed:
        _save_queue(queue)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Quantum task runner")
    parser.add_argument("--task", choices=sorted(_TASKS), default=None,
                        help="run one task ad-hoc instead of draining the queue")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.task:
        res = _TASKS[args.task]()
        print(json.dumps(res, indent=2, default=str))
        return 0
    results = drain_queue()
    print(json.dumps({"ran": len(results)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
