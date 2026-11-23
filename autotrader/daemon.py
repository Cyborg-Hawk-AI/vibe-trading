"""Fully autonomous daemon: start it, stop it — nothing else.

    python -m autotrader.daemon            # or ./run_pipeline.sh --daemon

The infinite loop, per cycle:

  1. load central state (``autotrader/state/daemon_state.json``) and apply
     the accumulated, validated LLM amendments to the search config;
  2. run one evolution cycle across the configured sleeves (each generation
     inside already checks in with Kimi-K3/Bedrock and runs attribution);
  3. send the cycle-level results to the LLM and collect NEW amendment
     proposals (bounded search knobs only — never friction, risk caps, or
     the acceptance gate; see checkin.validate_amendments);
  4. paper-validate any hard-gate finalists on fresh recent data;
  5. regenerate the track-record proof report and save state.

Stop conditions: SIGINT/SIGTERM (graceful, state saved) or ``--max-cycles``.
Reaching the hard goal (a strategy passing the hard gate AND the paper gate)
does not stop the loop — the daemon keeps re-validating the champion each
cycle and keeps searching for better candidates, exactly what the funding
ladder needs as ongoing evidence.

Crash-safe: state is rewritten atomically after every cycle; the registry
persists every result as it happens, so a killed daemon resumes where it
stopped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from autotrader.bootstrap import ensure_agent_path, state_dir
from autotrader.checkin import build_cycle_payload, checkin_cycle, objective_text
from autotrader.config import PipelineConfig
from autotrader.live_connector import live_status
from autotrader.optimize_loop import evolve_sleeve, prefetch_sleeves, sleeve_codes
from autotrader.paper_trader import paper_verdict, run_paper_validation
from autotrader.registry import Registry
from autotrader.strategy_space import apply_gene_bounds, reset_gene_bounds
from autotrader.validation_ladder import (
    eval_profile_from_sim_result,
    export_gate_passers_for_livetrader,
    stress_2x_friction,
)

ensure_agent_path()

logger = logging.getLogger("autotrader.daemon")

STATE_PATH_NAME = "daemon_state.json"
TRACK_RECORD_NAME = "track_record.md"

# Funding ladder from the mission: each rung unlocks when the champion shows
# the required count of consecutive cycles with a fresh paper-gate pass.
FUNDING_LADDER_USD = (200, 500, 1_000, 2_000, 4_000, 8_000)
CYCLES_PER_RUNG = 3


def _state_path() -> Path:
    return state_dir() / STATE_PATH_NAME


def load_state() -> dict[str, Any]:
    path = _state_path()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "cycle": 0,
        "amendments": {},          # cumulative validated LLM knob adjustments
        "gene_bounds": {},         # cumulative validated gene-bound focus
        "cycle_history": [],       # compact per-cycle log
        "champion": None,          # best gate-passer seen so far (per sleeve)
        "consecutive_paper_passes": 0,
        "funding_rung": 0,         # index into FUNDING_LADDER_USD
        "hard_goal_reached": False,
    }


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def apply_amendments(cfg: PipelineConfig, state: dict[str, Any]) -> dict[str, Any]:
    """Apply the accumulated validated amendments to a config copy.

    Returns the overrides dict handed to worker processes so they see the
    same amended values.
    """
    amendments = state.get("amendments", {})
    overrides: dict[str, Any] = {}
    if "mutation_sigma" in amendments:
        cfg.evolution.mutation_sigma = float(amendments["mutation_sigma"])
        overrides.setdefault("evolution", {})["mutation_sigma"] = cfg.evolution.mutation_sigma
    if "population_size" in amendments:
        cfg.evolution.population_size = int(amendments["population_size"])
        overrides.setdefault("evolution", {})["population_size"] = cfg.evolution.population_size
    if "llm_proposals_per_gen" in amendments:
        cfg.evolution.llm_proposals_per_gen = int(amendments["llm_proposals_per_gen"])
        overrides.setdefault("evolution", {})["llm_proposals_per_gen"] = (
            cfg.evolution.llm_proposals_per_gen
        )
    if "equity_universe_size" in amendments:
        cfg.data.equity_universe_size = int(amendments["equity_universe_size"])
        overrides.setdefault("data", {})["equity_universe_size"] = cfg.data.equity_universe_size
    if "crypto_universe_size" in amendments:
        cfg.data.crypto_universe_size = int(amendments["crypto_universe_size"])
        overrides.setdefault("data", {})["crypto_universe_size"] = cfg.data.crypto_universe_size
    if "penny_universe_size" in amendments:
        # Floor at 5: size 0 empties the sleeve and every cycle raises
        # "No snapshot data for sleeve 'equities_penny'".
        cfg.data.penny_universe_size = max(5, int(amendments["penny_universe_size"]))
        overrides.setdefault("data", {})["penny_universe_size"] = cfg.data.penny_universe_size

    reset_gene_bounds()
    if state.get("gene_bounds"):
        apply_gene_bounds(state["gene_bounds"])
    return overrides


def merge_amendments(state: dict[str, Any], new: dict[str, Any]) -> None:
    """Fold a freshly validated amendment proposal into central state."""
    if not new:
        return
    bounds = new.pop("gene_bounds", None)
    notes = new.pop("notes", None)
    new.pop("focus_failures", None)  # informational; diagnostics drive the bias
    state.setdefault("amendments", {}).update(new)
    # Never persist a penny universe size that empties the sleeve.
    am = state["amendments"]
    if "penny_universe_size" in am:
        am["penny_universe_size"] = max(5, int(am["penny_universe_size"]))
    if bounds:
        state.setdefault("gene_bounds", {}).update(bounds)
    if notes:
        state["last_llm_notes"] = notes


def _update_champion(state: dict[str, Any], summaries: dict[str, Any]) -> None:
    """Track the best hard-gate passer (or best-so-far) across cycles."""
    key = lambda c: (  # noqa: E731
        bool(c.get("gate_passed")),
        float(c.get("oos_pass_rate") or 0.0),
        float(c.get("oos_mean_window_ret") or 0.0),
    )
    for sleeve, summary in summaries.items():
        current = state.get("champion")
        # Prefer an actual gate passer from this sleeve over train-fitness best.
        passer = None
        for p in summary.get("gate_passers") or []:
            result = p.get("result") or {}
            candidate = {
                "sleeve": sleeve,
                "gid": p.get("gid") or result.get("gid"),
                "genome": p.get("genome") or result.get("genome"),
                "gate_passed": True,
                "oos_pass_rate": result.get("oos_pass_rate", p.get("oos_pass_rate", 0.0)),
                "oos_mean_window_ret": result.get(
                    "oos_mean_window_ret", p.get("oos_mean_window_ret", 0.0)
                ),
                "max_drawdown": result.get("max_drawdown", p.get("max_drawdown", 0.0)),
                "stretch_25_pass_rate": result.get("stretch_25_pass_rate", 0.0),
                "stretch_25_passed": bool(result.get("stretch_25_passed")),
            }
            if passer is None or key(candidate) > key(passer):
                passer = candidate
        best = summary.get("best_ever")
        if passer is not None:
            challenger = passer
        elif best:
            challenger = {
                "sleeve": sleeve,
                "gid": best["gid"],
                "genome": best["genome"],
                "gate_passed": bool(best.get("gate_passed")),
                "oos_pass_rate": best.get("oos_pass_rate", 0.0),
                "oos_mean_window_ret": best.get("oos_mean_window_ret", 0.0),
                "max_drawdown": best.get("max_drawdown", 0.0),
                "stretch_25_pass_rate": best.get("stretch_25_pass_rate", 0.0),
                "stretch_25_passed": bool(best.get("stretch_25_passed")),
            }
        else:
            continue
        if current is None or key(challenger) > key(current):
            state["champion"] = challenger


def _paper_validate_champion(
    cfg: PipelineConfig, state: dict[str, Any], *, refetch: bool
) -> None:
    champ = state.get("champion")
    if not champ or not champ.get("gate_passed"):
        state["consecutive_paper_passes"] = 0
        return
    try:
        record = run_paper_validation(
            champ["genome"], cfg, champ["sleeve"],
            codes=sleeve_codes(champ["sleeve"], cfg), refetch=refetch,
        )
    except Exception as exc:  # noqa: BLE001 — network hiccups must not kill the daemon
        logger.warning("paper validation errored: %s", exc)
        return

    # Stage D: stress at 2× friction when paper data is available.
    try:
        from autotrader.paper_trader import load_paper_data_map

        data_map, _window, _interval = load_paper_data_map(
            champ["genome"], cfg, sleeve_codes(champ["sleeve"], cfg), refetch=False,
        )
        if data_map:
            stress = stress_2x_friction(champ["genome"], data_map, cfg, champ["sleeve"])
            record["stress_2x"] = stress
            champ["stress_2x_passed"] = bool(stress.get("passed"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("stress validation errored: %s", exc)

    # Funded-eval scoreboard (proxy from paper totals).
    class _Proxy:
        total_return = float(record.get("total_return", 0.0))
        max_drawdown = float(record.get("max_drawdown", 0.0))

    eval_score = eval_profile_from_sim_result(_Proxy(), cfg)
    record["eval_profile"] = eval_score
    champ["eval_profile_passed"] = bool(eval_score.get("passed"))
    state["champion"] = champ

    if record.get("passed"):
        state["consecutive_paper_passes"] = state.get("consecutive_paper_passes", 0) + 1
        state["hard_goal_reached"] = True
        rung = min(
            state["consecutive_paper_passes"] // CYCLES_PER_RUNG,
            len(FUNDING_LADDER_USD) - 1,
        )
        state["funding_rung"] = max(state.get("funding_rung", 0), rung)
    else:
        state["consecutive_paper_passes"] = 0


def write_track_record(cfg: PipelineConfig, state: dict[str, Any], registry: Registry) -> Path:
    """Regenerate the proof report the funding decisions are based on."""
    champ = state.get("champion")
    lines = [
        "# Autotrader track record",
        "",
        f"_Generated {dt.datetime.now():%Y-%m-%d %H:%M} — cycle {state['cycle']}. "
        "All returns are simulated out-of-sample unless marked live/paper. "
        "This report never claims returns that were not measured._",
        "",
        "## Hard goal",
        f"- Operational target: >= {cfg.gate.target_window_return:.0%} net return in "
        f">= {cfg.gate.min_window_pass_rate:.0%} of held-out rolling 2-week windows, "
        f"max drawdown <= {cfg.gate.max_drawdown:.0%}.",
        f"- Stretch diagnostic (does not block promotion): >= {cfg.gate.stretch_target_return:.0%} "
        f"in >= {cfg.gate.stretch_min_pass_rate:.0%} of OOS windows.",
        f"- Status: {'REACHED (gate + paper)' if state.get('hard_goal_reached') else 'not reached yet'}",
        "",
        "## Current champion",
    ]
    if champ:
        verdict = paper_verdict(champ["gid"]) or {}
        status = live_status(cfg, champ["gid"], gate_passed=champ["gate_passed"])
        lines += [
            f"- Strategy `{champ['gid']}` on sleeve `{champ['sleeve']}`",
            f"- Operational backtest gate: {'PASSED' if champ['gate_passed'] else 'not passed'} "
            f"(OOS 2-week window pass rate {champ['oos_pass_rate']:.0%}, "
            f"mean OOS window return {champ['oos_mean_window_ret']:.2%}, "
            f"max drawdown {champ['max_drawdown']:.2%})",
            f"- Stretch 25% diagnostic: pass rate {champ.get('stretch_25_pass_rate', 0.0):.0%} "
            f"({'CLEARED' if champ.get('stretch_25_passed') else 'not cleared'})",
            f"- Paper gate (recent {cfg.paper.lookback_days}d shadow run): "
            + (
                (
                    f"ERROR — {verdict.get('error')}"
                    if verdict.get("error") and not verdict.get("ok")
                    else (
                        f"{'PASSED' if verdict.get('passed') else 'FAILED'} "
                        f"(return {verdict.get('total_return', 0.0):.2%}, "
                        f"drawdown {verdict.get('max_drawdown', 0.0):.2%}, "
                        f"{verdict.get('trade_count', 0)} trades)"
                    )
                )
                if verdict else "not run yet"
            ),
            f"- Consecutive cycles with a paper pass: {state.get('consecutive_paper_passes', 0)}",
            f"- Stress 2× friction: "
            + ("PASSED" if champ.get("stress_2x_passed") else "not run / failed"),
            f"- Funded-eval profile (+{cfg.eval_profile.profit_target:.0%} / "
            f"{cfg.eval_profile.trailing_max_dd:.0%} trailing DD): "
            + ("PASSED" if champ.get("eval_profile_passed") else "not cleared"),
            f"- Live interlock status: "
            + ("ELIGIBLE (still requires AUTOTRADER_LIVE_ENABLE=1)" if status["live_eligible"]
               else f"blocked — {status['blocked_by']}"),
            "",
            "## Income path note",
            "- Own-capital compounding + funded-account eval readiness when Stages A–D are green.",
            "- Real orders stay off until AUTOTRADER_LIVE_ENABLE=1; no auto-enrollment.",
        ]
    else:
        lines.append("- No champion yet: nothing has passed the hard gate.")
    rung = state.get("funding_rung", 0)
    lines += [
        "",
        "## Funding ladder readiness",
        "- Ladder: " + " -> ".join(f"${v:,}" for v in FUNDING_LADDER_USD),
        f"- Evidence supports funding at: **${FUNDING_LADDER_USD[rung]:,}**"
        + (" (base rung — no validated champion yet)" if rung == 0 and not state.get("hard_goal_reached") else ""),
        f"- Rule: each rung requires {CYCLES_PER_RUNG} more consecutive cycles "
        "with a fresh paper-gate pass by the champion.",
        "",
        "## Recent cycles",
    ]
    for entry in state.get("cycle_history", [])[-10:]:
        lines.append(
            f"- cycle {entry['cycle']} ({entry['when']}): best OOS pass "
            f"{entry.get('best_oos_pass', 0.0):.0%}, gate passers {entry.get('gate_passers', 0)}, "
            f"amendments accepted {entry.get('amendments_accepted', 0)}"
        )
    if state.get("last_llm_notes"):
        lines += ["", "## Latest research-director notes", "", str(state["last_llm_notes"])]
    path = state_dir() / TRACK_RECORD_NAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _StopFlag:
    def __init__(self) -> None:
        self.stop = False

    def install(self) -> None:
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum: int, _frame: Any) -> None:
        logger.info("received signal %s — finishing this cycle then stopping", signum)
        self.stop = True


def run_daemon(
    *,
    sleeves: list[str],
    config_path: str | None = None,
    snapshot: str = "daemon",
    workers: int = 0,
    max_cycles: int = 0,
    refresh_data_every: int = 5,
    cooldown_s: float = 30.0,
) -> int:
    state = load_state()
    stopper = _StopFlag()
    stopper.install()
    logger.info(
        "daemon starting at cycle %d (sleeves=%s, snapshot=%s). Ctrl-C to stop.",
        state["cycle"], sleeves, snapshot,
    )

    while not stopper.stop:
        cycle = state["cycle"]
        cfg = PipelineConfig.load(config_path)
        cfg_overrides: dict[str, Any] = {}
        if config_path:
            cfg_overrides = json.loads(Path(config_path).read_text(encoding="utf-8"))
        for section, values in apply_amendments(cfg, state).items():
            cfg_overrides.setdefault(section, {}).update(values)
        # Always push operational gate into workers (defaults alone can drift
        # across long-lived processes / stale imports).
        cfg_overrides.setdefault("gate", {}).update({
            "target_window_return": cfg.gate.target_window_return,
            "min_window_pass_rate": cfg.gate.min_window_pass_rate,
            "max_drawdown": cfg.gate.max_drawdown,
            "stretch_target_return": cfg.gate.stretch_target_return,
            "stretch_min_pass_rate": cfg.gate.stretch_min_pass_rate,
        })

        # Refresh the frozen snapshot periodically so the search keeps
        # seeing recent market structure (cycle 0 always fetches).
        if cycle % max(refresh_data_every, 1) == 0:
            try:
                prefetch_sleeves(sleeves, cfg, snapshot=snapshot)
            except Exception as exc:  # noqa: BLE001 — offline cycles reuse the last snapshot
                logger.warning("prefetch failed (%s); reusing existing snapshot", exc)

        registry = Registry()
        summaries: dict[str, Any] = {}
        try:
            for sleeve in sleeves:
                if stopper.stop:
                    break
                try:
                    summaries[sleeve] = evolve_sleeve(
                        sleeve, cfg, cfg_overrides, registry,
                        snapshot=snapshot, workers=workers,
                    )
                except Exception:  # noqa: BLE001 — one bad sleeve must not kill the nightly loop
                    if stopper.stop:
                        break
                    logger.exception("[%s] sleeve failed this cycle; continuing", sleeve)
        finally:
            registry.export_json(state_dir() / "registry_export.json")

        if stopper.stop:
            registry.close()
            break

        _update_champion(state, summaries)

        # Export gate-passers so livetrader can seed them on next evolve/restart.
        all_passers = [
            p for s in summaries.values() for p in s.get("gate_passers", [])
        ]
        if all_passers:
            export_gate_passers_for_livetrader(all_passers)
            logger.info("exported %d gate-passers for livetrader seeding", len(all_passers))

        # Cycle-level check-in: full results to Kimi/Bedrock, amendments back.
        all_results = [
            r for s in summaries.values()
            for r in ([s["best_ever"]] if s.get("best_ever") else [])
        ]
        gate_passer_count = sum(len(s.get("gate_passers", [])) for s in summaries.values())
        record = checkin_cycle(
            cfg,
            build_cycle_payload(
                "+".join(sleeves), cycle, all_results, {},
                next(iter(summaries.values()), {}).get("leaderboard", []),
                objective=objective_text(cfg),
            ),
        )
        accepted = dict(record.get("amendments", {}))  # validated by checkin_cycle
        merge_amendments(state, dict(accepted))

        # Paper-validate the champion on fresh data (the hard-goal check).
        _paper_validate_champion(cfg, state, refetch=not stopper.stop)

        best_oos = max(
            (s["best_ever"]["oos_pass_rate"] for s in summaries.values() if s.get("best_ever")),
            default=0.0,
        )
        state["cycle_history"] = (state.get("cycle_history", []) + [{
            "cycle": cycle,
            "when": f"{dt.datetime.now():%Y-%m-%d %H:%M}",
            "best_oos_pass": best_oos,
            "gate_passers": gate_passer_count,
            "amendments_accepted": len(accepted),
        }])[-100:]
        state["cycle"] = cycle + 1
        save_state(state)
        report = write_track_record(cfg, state, registry)
        registry.close()
        logger.info(
            "cycle %d done: best OOS pass %.0f%%, gate passers %d, "
            "amendments %d, report %s",
            cycle, best_oos * 100, gate_passer_count, len(accepted), report,
        )

        if max_cycles and state["cycle"] >= max_cycles:
            logger.info("reached --max-cycles=%d; stopping", max_cycles)
            break
        if not stopper.stop and cooldown_s > 0:
            time.sleep(cooldown_s)

    save_state(state)
    logger.info("daemon stopped at cycle %d (state saved)", state["cycle"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autotrader autonomous daemon")
    parser.add_argument("--sleeves", default="equities,crypto,equities_penny")
    parser.add_argument("--config", default=None, help="JSON config overrides")
    parser.add_argument("--snapshot", default="daemon")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 = run forever")
    parser.add_argument("--refresh-data-every", type=int, default=5)
    parser.add_argument("--cooldown", type=float, default=30.0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    return run_daemon(
        sleeves=[s.strip() for s in args.sleeves.split(",") if s.strip()],
        config_path=args.config,
        snapshot=args.snapshot,
        workers=args.workers,
        max_cycles=args.max_cycles,
        refresh_data_every=args.refresh_data_every,
        cooldown_s=args.cooldown,
    )


if __name__ == "__main__":
    sys.exit(main())
