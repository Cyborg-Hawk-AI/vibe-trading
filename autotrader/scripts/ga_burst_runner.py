"""On-instance GA burst runner: one massive evolution sweep per sleeve.

Runs ON the EC2 spot instance after code+data sync. Not for local use.

    python -m autotrader.scripts.ga_burst_runner --population 256 --generations 32

Writes results into ``autotrader/state/daytrade_registry.db`` (fresh per
burst) plus ``burst_summary.json``; the user-data script uploads both to S3.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import time
from pathlib import Path

from autotrader.bootstrap import state_dir
from autotrader.config import PipelineConfig
from autotrader.optimize_loop import evolve_sleeve
from autotrader.registry import Registry
from autotrader.strategy_space import apply_gene_bounds

logger = logging.getLogger("autotrader.burst")

DAYTRADE_TIMEFRAME_BOUNDS = [3, 4]  # 15m / 5m


def main() -> int:
    parser = argparse.ArgumentParser(description="EC2 spot GA burst runner")
    parser.add_argument("--sleeves", default="equities,crypto,equities_penny")
    parser.add_argument("--population", type=int, default=256)
    parser.add_argument("--generations", type=int, default=32)
    parser.add_argument("--workers", type=int, default=max(mp.cpu_count() - 2, 1))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = PipelineConfig()
    cfg.data.intervals = ("15m", "5m")
    cfg.evolution.population_size = args.population
    cfg.evolution.generations = args.generations
    cfg.llm.disabled = True  # burst is pure compute; LLM stays on the Mac
    apply_gene_bounds({"timeframe": DAYTRADE_TIMEFRAME_BOUNDS})
    overrides = {
        "data": {"intervals": ["15m", "5m"]},
        "evolution": {
            "population_size": args.population,
            "generations": args.generations,
        },
    }

    registry = Registry(state_dir() / "daytrade_registry.db")
    summary: dict = {
        "started": time.time(),
        "population": args.population,
        "generations": args.generations,
        "workers": args.workers,
        "sleeves": {},
    }
    try:
        for sleeve in [s.strip() for s in args.sleeves.split(",") if s.strip()]:
            t0 = time.time()
            try:
                s = evolve_sleeve(
                    sleeve, cfg, overrides, registry,
                    snapshot="daytrade", workers=args.workers,
                )
                best = s.get("best_ever") or {}
                summary["sleeves"][sleeve] = {
                    "minutes": round((time.time() - t0) / 60, 1),
                    "gate_passers": len(s.get("gate_passers", [])),
                    "best_oos_pass": best.get("oos_pass_rate"),
                    "best_oos_mean": best.get("oos_mean_window_ret"),
                    "skipped": s.get("skipped", False),
                }
                logger.info(
                    "burst sleeve %s done in %.1f min: %d gate passers",
                    sleeve, (time.time() - t0) / 60, len(s.get("gate_passers", [])),
                )
            except Exception as exc:  # noqa: BLE001 — record and continue
                logger.exception("burst sleeve %s failed", sleeve)
                summary["sleeves"][sleeve] = {"error": str(exc)}
    finally:
        registry.export_json(state_dir() / "daytrade_registry_export.json")
        registry.close()

    summary["elapsed_min"] = round((time.time() - summary["started"]) / 60, 1)
    (state_dir() / "burst_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    logger.info("burst complete: %s", json.dumps(summary["sleeves"], default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
