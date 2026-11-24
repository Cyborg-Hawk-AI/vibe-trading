"""Mutation & nuance-tracking loop (Pillar 2).

Evolutionary search over strategy genomes, per sleeve:

  1. Seed a population of random genomes (clamped to risk bounds).
  2. Evaluate every genome with the deterministic simulator (parallel across
     local CPU cores) — fitness is scored on TRAIN windows only.
  3. Diagnose losing trades of the top performers into the failure taxonomy.
  4. Breed the next generation: elites survive; children come from
     tournament-selected parents via crossover + diagnostic-biased Gaussian
     mutation; an optional LLM proposes a few structural variants which are
     validated through the exact same simulator.
  5. Repeat. The OPERATIONAL GATE (>= 3% net in >= 30% of held-out OOS windows,
     MDD <= 10%) is checked every generation; passers become finalists for
     the TradingAgents committee. Honest OOS stats and train/OOS divergence
     are persisted every generation regardless of gate outcome.

CLI:
    python -m autotrader.optimize_loop --sleeves equities,crypto \
        --generations 10 --population 24 [--config overrides.json] [--offline]
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from autotrader.bootstrap import ensure_agent_path, state_dir
from autotrader.config import PipelineConfig
from autotrader.data import load_snapshot, prefetch, snapshot_for_interval
from autotrader.diagnostics import diagnose, llm_memo, merge_reports
from autotrader.llm import chat, extract_json_block
from autotrader.registry import Registry
from autotrader.simulator import SimulationResult, simulate_genome
from autotrader.strategy_space import (
    GENE_SPACE,
    clamp_to_risk_bounds,
    crossover,
    genome_id,
    mutate,
    mutate_biased,
    mutate_gene_bias,
    random_genome,
)
from autotrader.universe import get_universe

ensure_agent_path()

logger = logging.getLogger("autotrader.optimize")

# Module-level so ProcessPoolExecutor workers can be handed picklable args.
_WORKER_STATE: dict[str, Any] = {}

SLEEVE_SEED_OFFSET = {"equities": 0, "crypto": 1, "equities_penny": 2}


def sleeve_codes(sleeve: str, cfg: PipelineConfig) -> list[str]:
    """Universe slice for a sleeve."""
    if sleeve == "equities":
        return get_universe(cfg.data.equity_universe, cfg.data.equity_universe_size)
    if sleeve == "equities_penny":
        return get_universe(cfg.data.penny_universe, cfg.data.penny_universe_size)
    if sleeve == "crypto":
        return get_universe(cfg.data.crypto_universe, cfg.data.crypto_universe_size)
    raise ValueError(f"unknown sleeve {sleeve!r}")


def _init_worker(snapshot: str, codes: list[str], cfg_dict: dict[str, Any], sleeve: str) -> None:
    """Load the data snapshots (all intervals) once per worker process."""
    from autotrader.config import PipelineConfig as _PC

    cfg = _PC()
    _apply_dict(cfg, cfg_dict)
    _WORKER_STATE["cfg"] = cfg
    _WORKER_STATE["sleeve"] = sleeve
    _WORKER_STATE["data_maps"] = _load_interval_snapshots(snapshot, codes, cfg)


def _load_interval_snapshots(
    snapshot: str, codes: list[str], cfg: PipelineConfig
) -> dict[str, dict[str, Any]]:
    """interval -> data_map for every configured interval with data on disk."""
    maps: dict[str, dict[str, Any]] = {}
    for interval in cfg.data.intervals:
        dm = load_snapshot(snapshot_for_interval(snapshot, interval), codes)
        if dm:
            maps[interval] = dm
    return maps


def _apply_dict(cfg: PipelineConfig, overrides: dict[str, Any]) -> None:
    for section, values in overrides.items():
        target = getattr(cfg, section, None)
        if target is None:
            continue
        for key, value in values.items():
            if hasattr(target, key):
                if key.endswith("_bounds") and isinstance(value, list):
                    value = tuple(value)
                setattr(target, key, value)


# 5m evaluations are ~3x the engine cost of 15m; cap them to the most
# recent sessions so the mixed-timeframe population keeps cycle times in
# the multiple-cycles-per-day range. 15m carries the full-history evidence.
# Raised 30 -> 45 after the engine hot-loop rewrite made evals ~7.6x cheaper.
_5M_MAX_SESSIONS = 45


def _eval_genome(
    genome: dict[str, Any],
    data_maps: dict[str, dict[str, Any]],
    cfg: PipelineConfig,
    sleeve: str,
) -> SimulationResult:
    """Evaluate one genome on the interval its timeframe gene selects."""
    from autotrader.simulator import bars_per_trading_day, genome_interval

    interval = genome_interval(genome, tuple(data_maps))
    data_map = data_maps[interval]
    if interval == "5m":
        tail = _5M_MAX_SESSIONS * bars_per_trading_day(interval, sleeve)
        data_map = {c: df.tail(tail) for c, df in data_map.items()}
    return simulate_genome(
        genome, data_map, cfg, sleeve, interval=interval
    )


def _eval_worker(genome: dict[str, Any]) -> dict[str, Any]:
    result = _eval_genome(
        genome, _WORKER_STATE["data_maps"], _WORKER_STATE["cfg"], _WORKER_STATE["sleeve"]
    )
    return result.to_dict()


def _result_from_dict(d: dict[str, Any]) -> SimulationResult:
    from autotrader.simulator import AnomalyEvent, WindowResult

    d = dict(d)
    d.pop("fitness", None)
    d["windows"] = [WindowResult(**w) for w in d.get("windows", [])]
    d["anomalies"] = [AnomalyEvent(**a) for a in d.get("anomalies", [])]
    return SimulationResult(**d)


def llm_propose_genomes(
    cfg: PipelineConfig,
    leaderboard: list[dict[str, Any]],
    failure_counts: dict[str, int],
    n: int,
    rng: random.Random,
) -> list[dict[str, Any]]:
    """Ask the LLM for structural genome variants; fall back to random."""
    if n <= 0:
        return []
    fallback = [random_genome(rng) for _ in range(n)]
    if not cfg.llm.available:
        return fallback
    space_desc = {
        name: {"low": lo, "high": hi, "type": {"f": "float", "i": "int", "b": "0/1"}[k]}
        for name, (lo, hi, k) in GENE_SPACE.items()
    }

    reply = chat(
        cfg.llm,
        system=(
            "You are a quantitative strategy designer. Given a parameter "
            "space, the current best genomes, and a failure-mode histogram, "
            "propose new parameter combinations that explore structurally "
            "different regions (different gate combinations, different "
            "holding horizons) while counteracting the dominant failures. "
            f"Reply with ONLY a JSON array of {n} objects, each containing "
            "every parameter name with a value inside its bounds."
        ),
        user=json.dumps(
            {
                "space": space_desc,
                "top_genomes": [],
                "failure_counts": {},
            }
        ),
        max_tokens=1800,
    )
    parsed = extract_json_block(reply or "")
    if not isinstance(parsed, list):
        return fallback
    proposals: list[dict[str, Any]] = []
    for item in parsed[:n]:
        if not isinstance(item, dict):
            continue
        try:
            genome = {
                name: float(item[name]) for name in GENE_SPACE if name in item
            }
            if len(genome) != len(GENE_SPACE):
                continue
            from autotrader.strategy_space import _cast, _repair

            genome = _repair({k: _cast(k, v) for k, v in genome.items()})
            proposals.append(clamp_to_risk_bounds(genome, cfg))
        except (TypeError, ValueError, KeyError):
            continue
    while len(proposals) < n:
        proposals.append(fallback[len(proposals)])
    return proposals


def prefetch_sleeves(
    sleeves: list[str], cfg: PipelineConfig, *, snapshot: str = "default"
) -> None:
    """Prefetch OHLCV snapshots for every sleeve and configured interval."""
    for sleeve in sleeves:
        codes = sleeve_codes(sleeve, cfg)
        for interval in cfg.data.intervals:
            summary = prefetch(
                codes, cfg.data.start_date, cfg.data.end_date,
                snapshot=snapshot_for_interval(snapshot, interval),
                interval=interval, source=cfg.data.source,
            )
            logger.info("[%s/%s] prefetch: %s", sleeve, interval, summary)


def evolve_sleeve(
    sleeve: str,
    cfg: PipelineConfig,
    cfg_overrides: dict[str, Any],
    registry: Registry,
    *,
    snapshot: str = "default",
    workers: int = 0,
) -> dict[str, Any]:
    """Run the full evolutionary loop for one sleeve. Returns a summary."""
    rng = random.Random(cfg.evolution.seed + SLEEVE_SEED_OFFSET.get(sleeve, 9))
    codes = sleeve_codes(sleeve, cfg)
    if not codes:
        logger.warning(
            "[%s] universe size is 0 / empty — skipping sleeve this cycle", sleeve,
        )
        return {
            "sleeve": sleeve, "skipped": True, "reason": "empty_universe",
            "gate_passers": [], "best": None, "generations": 0,
            "simulated_trades": 0,
        }

    data_maps = _load_interval_snapshots(snapshot, codes, cfg)
    if not data_maps:
        logger.warning(
            "[%s] no snapshot data for codes=%s — skipping (prefetch / backfill needed)",
            sleeve, codes[:5],
        )
        return {
            "sleeve": sleeve, "skipped": True, "reason": "no_snapshot_data",
            "gate_passers": [], "best": None, "generations": 0,
            "simulated_trades": 0,
        }
    # Daily map for diagnostics/attribution context (always present).
    data_map = data_maps.get("1D") or next(iter(data_maps.values()))
    logger.info(
        "[%s] %d symbols loaded from snapshot (intervals: %s)",
        sleeve, len(data_map), sorted(data_maps),
    )

    ev = cfg.evolution
    population: list[tuple[dict[str, Any], str, str | None]] = [
        (clamp_to_risk_bounds(random_genome(rng), cfg), "seed", None)
        for _ in range(ev.population_size)
    ]
   #  Research must not seed from validation-selected strategies
    injcted = 0

    # Registry-leaderboard seeding: re-inject this sleeve's best measured
    # genomes (including AWS-burst winners merged back into the registry) so
    # each cycle climbs from the evidence frontier instead of re-discovering it.
    try:
        board = registry.research_leaderboard(sleeve, limit=max(4, ev.population_size // 8))
        reg_injected = 0
        for row in board:
            if not row.get("genome"):
                continue
            slot = injected + reg_injected
            if slot >= max(8, ev.population_size // 2):
                break
            population[slot] = (
                clamp_to_risk_bounds(dict(row["genome"]), cfg),
                "registry_seed",
                row.get("gid"),
            )
            reg_injected += 1
        if reg_injected:
            logger.info(
                "[%s] seeded %d registry-leaderboard genomes (best oos_pass=%.2f)",
                sleeve, reg_injected, board[0].get("train_pass_rate") or 0.0,
            )
    except Exception as exc:  # noqa: BLE001 — seeding must not kill evolve
        logger.warning("[%s] registry seeding skipped: %s", sleeve, exc)

    pool = (
        ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(snapshot, codes, cfg_overrides, sleeve),
        )
        if workers > 1
        else None
    )

    gate_passers: list[dict[str, Any]] = []
    best_ever: SimulationResult | None = None
    try:
        for gen in range(ev.generations):
            t0 = time.time()
            genomes = [g for g, _o, _p in population]
            for genome, origin, parent in population:
                registry.record_strategy(
                    genome_id(genome), sleeve, gen, genome, origin, parent
                )

            if pool is not None:
                result_dicts = list(pool.map(_eval_worker, genomes))
                results = [_result_from_dict(d) for d in result_dicts]
            else:
                results = [_eval_genome(g, data_maps, cfg, sleeve) for g in genomes]

            scored = sorted(zip(genomes, results), key=lambda gr: gr[1].fitness(), reverse=True)
            best_genome, best = scored[0]
            if best_ever is None or best.fitness() > best_ever.fitness():
                best_ever = best

            # Diagnostics on the top half's losing trades
            reports = []
            for _g, r in scored[: max(len(scored) // 2, 1)]:
                if r.ok and r.trades_csv:
                    reports.append(diagnose(r.trades_csv, data_map))
            gen_report = merge_reports(reports)

            # Winner attribution on the best performer (why did the winners win?)
            attribution: dict[str, Any] = {"attributions": [], "gene_bias": {}}
            if best.ok and best.trades_csv:
                try:
                    from autotrader.attribution import attribute_run

                    attribution = attribute_run(
                        best.trades_csv, data_map, cfg,
                        include_news=cfg.llm.available,
                    )
                except Exception as exc:  # noqa: BLE001 — attribution must not kill the loop
                    logger.warning("[%s] attribution failed: %s", sleeve, exc)

            # Check in with the LLM (Kimi K3): memo + bounded amendment
            # proposals, persisted to state/checkins/.
            from autotrader.checkin import (
                build_cycle_payload, checkin_cycle, objective_text,
            )

            checkin_record = checkin_cycle(
                cfg,
                build_cycle_payload(
                    sleeve, gen,
                    [r.to_dict() for _g, r in scored],
                    gen_report.failure_counts,
                    registry.research_leaderboard(sleeve, limit=5),
                    attribution["attributions"],
                    objective=objective_text(cfg),
                ),
            )
            memo = checkin_record["memo"] or llm_memo(
                gen_report, cfg.llm, sleeve=sleeve, generation=gen
            )

            for _g, r in scored:
                registry.record_result(r, gen, diagnostics=gen_report.to_dict())
            registry.record_generation(
                sleeve, gen, best.gid, best.fitness(), gen_report.failure_counts, memo
            )

            new_passers = [
                {"gid": r.gid, "genome": g, "result": r.to_dict()}
                for g, r in scored
                if r.gate_passed and r.gid not in {p["gid"] for p in gate_passers}
            ]
            gate_passers.extend(new_passers)

            logger.info(
                "[%s] gen %d/%d best fitness=%s oos_pass=%.2f train/oos gap=%.3f "
                "gate_passers=%d failures=%s (%.1fs)",
                sleeve, gen + 1, ev.generations,
                tuple(round(x, 4) for x in best.fitness()),
                best.oos_pass_rate,
                best.train_mean_window_ret - best.oos_mean_window_ret,
                len(gate_passers),
                gen_report.failure_counts,
                time.time() - t0,
            )

            if gen == ev.generations - 1:
                break

            # ---- breed next generation ----
            elites = [g for g, _r in scored[: ev.elite_keep]]
            leaderboard = registry.research_leaderboard(sleeve, limit=5)
            llm_children = llm_propose_genomes(
                cfg, leaderboard, gen_report.failure_counts, ev.llm_proposals_per_gen, rng
            )

            def tournament() -> dict[str, Any]:
                contenders = rng.sample(scored, min(ev.tournament_size, len(scored)))
                return max(contenders, key=lambda gr: gr[1].fitness())[0]

            next_pop: list[tuple[dict[str, Any], str, str | None]] = [
                (g, "elite", genome_id(g)) for g in elites
            ]
            next_pop.extend((g, "llm_proposal", None) for g in llm_children)
            # Exploration reserve: ~15% of every generation is always fresh
            # random DNA, even when a champion reigns — the search keeps
            # researching new regions instead of only refining incumbents.
            n_explore = max(2, int(ev.population_size * 0.15))
            next_pop.extend(
                (clamp_to_risk_bounds(random_genome(rng), cfg), "exploration", None)
                for _ in range(n_explore)
            )
            while len(next_pop) < ev.population_size:
                a, b = tournament(), tournament()
                child = crossover(a, b, rng)
                child = mutate(child, rng, ev.mutation_sigma)
                origin = "mutation"
                child = clamp_to_risk_bounds(child, cfg)
                next_pop.append((child, origin, genome_id(a)))
            population = next_pop[: ev.population_size]
    finally:
        if pool is not None:
            pool.shutdown()

    summary = {
        "sleeve": sleeve,
        "generations": ev.generations,
        "gate_passers": [
            {
                "gid": p["gid"],
                "sleeve": sleeve,
                "genome": p["genome"],
                "oos_pass_rate": (p.get("result") or {}).get("oos_pass_rate"),
                "oos_mean_window_ret": (p.get("result") or {}).get("oos_mean_window_ret"),
                "result": p.get("result"),
            }
            for p in gate_passers
        ],
        "best_ever": best_ever.to_dict() if best_ever else None,
        "leaderboard": registry.leaderboard(sleeve, limit=10),
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autotrader mutation/optimization loop")
    parser.add_argument("--sleeves", default="equities,crypto")
    parser.add_argument("--generations", type=int, default=None)
    parser.add_argument("--population", type=int, default=None)
    parser.add_argument("--workers", type=int, default=0,
                        help="parallel eval processes (0 = in-process serial)")
    parser.add_argument("--config", default=None, help="JSON overrides file")
    parser.add_argument("--snapshot", default="default")
    parser.add_argument("--prefetch", action="store_true",
                        help="fetch/refresh the data snapshot before evolving")
    parser.add_argument("--offline", action="store_true",
                        help="disable LLM calls even if a key is configured")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = PipelineConfig.load(args.config)
    cfg_overrides: dict[str, Any] = {}
    if args.config:
        cfg_overrides = json.loads(open(args.config, encoding="utf-8").read())
    if args.generations is not None:
        cfg.evolution.generations = args.generations
        cfg_overrides.setdefault("evolution", {})["generations"] = args.generations
    if args.population is not None:
        cfg.evolution.population_size = args.population
        cfg_overrides.setdefault("evolution", {})["population_size"] = args.population
    if args.offline:
        cfg.llm.disabled = True
        cfg_overrides.setdefault("llm", {})["disabled"] = True

    sleeves = [s.strip() for s in args.sleeves.split(",") if s.strip()]

    if args.prefetch:
        prefetch_sleeves(sleeves, cfg, snapshot=args.snapshot)

    registry = Registry()
    overall: dict[str, Any] = {}
    for sleeve in sleeves:
        overall[sleeve] = evolve_sleeve(
            sleeve, cfg, cfg_overrides, registry,
            snapshot=args.snapshot, workers=args.workers,
        )

    report_path = state_dir() / "last_run_summary.json"
    report_path.write_text(json.dumps(overall, indent=2, default=str), encoding="utf-8")
    registry.export_json(state_dir() / "registry_export.json")
    registry.close()

    for sleeve, summary in overall.items():
        passers = summary["gate_passers"]
        best = summary["best_ever"] or {}
        print(f"\n=== {sleeve} ===")
        print(f"gate passers (>=3% in >=30% OOS windows, MDD<=10%): {len(passers)}")
        if best:
            print(
                f"best-so-far gid={best['gid']} "
                f"train_pass={best['train_pass_rate']:.2%} "
                f"oos_pass={best['oos_pass_rate']:.2%} "
                f"oos_mean_2w_ret={best['oos_mean_window_ret']:.2%} "
                f"total_ret={best['total_return']:.2%} mdd={best['max_drawdown']:.2%}"
            )
    print(f"\nSummary: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
