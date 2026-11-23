"""Daytrade replay daemon: millions of simulated intraday trades per cycle.

    python -m autotrader.daytrade_daemon --workers 4

The intraday twin of ``autotrader.daemon``, repurposing the same GA
machinery (``optimize_loop.evolve_sleeve``) onto the 5m/15m session library
under ``autotrader/state/data/daytrade__*``:

  - every genome is backtested across ~60 recorded sessions with full
    friction and forced session-EOD flat (equities never hold overnight);
  - the gate is "makes money daily": net-positive P&L on enough HELD-OUT
    sessions, a real mean daily edge, capped intraday drawdown, and enough
    trades per session to actually be day-trading (DaytradeGateConfig);
  - passers are exported to ``daytrade_gate_passers_seed.json`` which the
    livetrader day desk seeds with top priority;
  - an escalating target ladder raises the return bar whenever the champion
    holds the gate for ``cycles_per_rung`` consecutive cycles — the search
    never idles at "good enough" (drawdown caps NEVER loosen);
  - after every cycle the lessons miner distills measured evidence into the
    knowledge base so each generation is bred smarter than the last.

State: ``daytrade_state.json`` / ``daytrade_registry.db`` /
``daytrade_track_record.md`` — fully separate from the daily-bar daemon.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from autotrader.bootstrap import ensure_agent_path, state_dir
from autotrader.checkin import build_cycle_payload, checkin_cycle, objective_text
from autotrader.config import PipelineConfig
from autotrader.daemon import _update_champion, apply_amendments, merge_amendments, _StopFlag
from autotrader.intraday_data import SNAPSHOT_BASE, ingest_live_tape, refresh_recent
from autotrader.optimize_loop import evolve_sleeve
from autotrader.registry import Registry
from autotrader.strategy_space import apply_gene_bounds
from autotrader.validation_ladder import export_gate_passers_for_livetrader

ensure_agent_path()

logger = logging.getLogger("autotrader.daytrade")

STATE_NAME = "daytrade_state.json"
REGISTRY_NAME = "daytrade_registry.db"
TRACK_RECORD_NAME = "daytrade_track_record.md"
SEED_EXPORT_NAME = "daytrade_gate_passers_seed.json"

# Timeframe gene indices for the intraday horizons (see strategy_space.TIMEFRAMES).
DAYTRADE_TIMEFRAME_BOUNDS = [3, 4]           # 15m / 5m
DAYTRADE_INTERVALS = ("15m", "5m")


def _state_path() -> Path:
    return state_dir() / STATE_NAME


def load_state() -> dict[str, Any]:
    path = _state_path()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "cycle": 0,
        "amendments": {},
        "gene_bounds": {},
        "cycle_history": [],
        "champion": None,
        "ladder_rung": 0,              # index into DaytradeGateConfig.ladder
        "cycles_at_rung": 0,           # consecutive champion gate-passes at this rung
        "simulated_trades_total": 0,   # running count across all cycles
    }


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def apply_daytrade_profile(
    cfg: PipelineConfig,
    state: dict[str, Any],
    file_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Configure cfg for intraday replay; return worker overrides.

    Precedence: profile throughput defaults < LLM amendments < explicit
    config-file values (``file_overrides`` marks what the file pinned).
    """
    file_overrides = file_overrides or {}
    overrides = apply_amendments(cfg, state)  # amendments + stored gene bounds

    cfg.data.intervals = DAYTRADE_INTERVALS
    overrides.setdefault("data", {})["intervals"] = list(DAYTRADE_INTERVALS)

    # Throughput defaults for the (engine-)expensive intraday evals — an
    # intraday bar costs the same engine work as a daily bar and there are
    # 26-78x more of them. LLM amendments can still retune these.
    amended = state.get("amendments", {})
    defaults = {
        # Supersized after the engine hot-loop rewrite (7.6x faster evals,
        # bit-identical results) — ~7.5x more genomes per cycle at roughly
        # the old cycle wall-clock when run with --workers 7.
        ("evolution", "population_size"): 96,
        ("evolution", "generations"): 20,
        ("data", "equity_universe_size"): 25,
        ("data", "crypto_universe_size"): 12,
        ("data", "penny_universe_size"): 12,
    }
    for (section, key), value in defaults.items():
        if key in amended or key in file_overrides.get(section, {}):
            continue  # amendments / explicit config beat the profile default
        setattr(getattr(cfg, section), key, value)
        overrides.setdefault(section, {})[key] = value

    # Escalating target ladder: return bars rise with the rung; DD caps fixed.
    ladder = cfg.daytrade_gate.ladder
    rung = min(int(state.get("ladder_rung", 0)), len(ladder) - 1)
    green, mean_daily = ladder[rung]
    cfg.daytrade_gate.min_green_rate = float(green)
    cfg.daytrade_gate.min_mean_daily_ret = float(mean_daily)
    overrides.setdefault("daytrade_gate", {}).update({
        "min_green_rate": cfg.daytrade_gate.min_green_rate,
        "min_mean_daily_ret": cfg.daytrade_gate.min_mean_daily_ret,
        "session_max_drawdown": cfg.daytrade_gate.session_max_drawdown,
        "max_drawdown": cfg.daytrade_gate.max_drawdown,
        "min_trades_per_session": cfg.daytrade_gate.min_trades_per_session,
    })

    # Search only the day-trade horizons (after amendments' gene bounds so
    # the LLM can never widen timeframe back out of the intraday band).
    apply_gene_bounds({"timeframe": DAYTRADE_TIMEFRAME_BOUNDS})
    return overrides


def _escalate_ladder(cfg: PipelineConfig, state: dict[str, Any]) -> None:
    """Champion held the gate this cycle? Climb toward the next rung."""
    champ = state.get("champion") or {}
    ladder = cfg.daytrade_gate.ladder
    rung = int(state.get("ladder_rung", 0))
    if champ.get("gate_passed"):
        state["cycles_at_rung"] = int(state.get("cycles_at_rung", 0)) + 1
    else:
        state["cycles_at_rung"] = 0
    if (
        state["cycles_at_rung"] >= cfg.daytrade_gate.cycles_per_rung
        and rung < len(ladder) - 1
    ):
        state["ladder_rung"] = rung + 1
        state["cycles_at_rung"] = 0
        green, mean_daily = ladder[rung + 1]
        state["ladder_note"] = (
            f"Target escalated to rung {rung + 1}: green-day rate >= {green:.0%}, "
            f"mean daily >= {mean_daily:.2%} (champion held rung {rung} for "
            f"{cfg.daytrade_gate.cycles_per_rung} cycles)."
        )
        logger.info("%s", state["ladder_note"])


def write_track_record(cfg: PipelineConfig, state: dict[str, Any]) -> Path:
    ladder = cfg.daytrade_gate.ladder
    rung = min(int(state.get("ladder_rung", 0)), len(ladder) - 1)
    champ = state.get("champion")
    lines = [
        "# Daytrade replay engine track record",
        "",
        f"_Generated {dt.datetime.now():%Y-%m-%d %H:%M} — cycle {state['cycle']}. "
        "Every result is simulated across recorded intraday sessions with full "
        "friction and forced session-EOD flat for equities. Held-out sessions "
        "only for the gate; this report never claims returns that were not "
        "measured._",
        "",
        "## Make-money-daily gate (current rung)",
        f"- Rung {rung}/{len(ladder) - 1}: net positive on >= "
        f"{cfg.daytrade_gate.min_green_rate:.0%} of held-out sessions, "
        f"mean daily net >= {cfg.daytrade_gate.min_mean_daily_ret:.2%}, "
        f"session DD <= {cfg.daytrade_gate.session_max_drawdown:.0%}, "
        f"account DD <= {cfg.daytrade_gate.max_drawdown:.0%}, "
        f">= {cfg.daytrade_gate.min_trades_per_session:.0f} trades/session.",
        f"- Consecutive champion passes at this rung: {state.get('cycles_at_rung', 0)} "
        f"(promotes at {cfg.daytrade_gate.cycles_per_rung})",
        f"- Simulated trades to date: {state.get('simulated_trades_total', 0):,}",
        "",
        "## Current champion",
    ]
    if champ:
        lines += [
            f"- Strategy `{champ['gid']}` on sleeve `{champ['sleeve']}`",
            f"- Gate: {'PASSED' if champ.get('gate_passed') else 'not passed'} "
            f"(green held-out days {champ.get('oos_pass_rate', 0.0):.0%}, "
            f"mean held-out daily return {champ.get('oos_mean_window_ret', 0.0):.2%}, "
            f"max drawdown {champ.get('max_drawdown', 0.0):.2%})",
        ]
    else:
        lines.append("- No champion yet.")
    lines += ["", "## Recent cycles"]
    for entry in state.get("cycle_history", [])[-10:]:
        lines.append(
            f"- cycle {entry['cycle']} ({entry['when']}): best green-day rate "
            f"{entry.get('best_oos_pass', 0.0):.0%}, gate passers "
            f"{entry.get('gate_passers', 0)}, ~{entry.get('simulated_trades', 0):,} "
            f"simulated trades, amendments {entry.get('amendments_accepted', 0)}"
        )
    if state.get("ladder_note"):
        lines += ["", f"_{state['ladder_note']}_"]
    if state.get("last_llm_notes"):
        lines += ["", "## Latest research-director notes", "", str(state["last_llm_notes"])]
    path = state_dir() / TRACK_RECORD_NAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_daemon(
    *,
    sleeves: list[str],
    config_path: str | None = None,
    workers: int = 0,
    max_cycles: int = 0,
    refresh_data_every: int = 3,
    cooldown_s: float = 30.0,
) -> int:
    state = load_state()
    stopper = _StopFlag()
    stopper.install()
    logger.info(
        "daytrade daemon starting at cycle %d (sleeves=%s, rung=%d)",
        state["cycle"], sleeves, state.get("ladder_rung", 0),
    )

    while not stopper.stop:
        cycle = state["cycle"]
        cfg = PipelineConfig.load(config_path)
        cfg_overrides: dict[str, Any] = {}
        if config_path:
            cfg_overrides = json.loads(Path(config_path).read_text(encoding="utf-8"))
        profile = apply_daytrade_profile(cfg, state, file_overrides=cfg_overrides)
        for section, values in profile.items():
            cfg_overrides.setdefault(section, {}).update(values)

        # Keep the session library fresh: new live-tape sessions every cycle,
        # full provider refresh every N cycles.
        try:
            if cycle % max(refresh_data_every, 1) == 0:
                refresh_recent(days=5)
            else:
                ingest_live_tape()
        except Exception as exc:  # noqa: BLE001 — offline cycles reuse the library
            logger.warning("data refresh failed (%s); reusing existing library", exc)

        # Yesterday post-mortem: once per completed ET session, before the
        # multi-session sweep — re-trade the day we just lived through
        # (GA replay + oracle capture + swing attribution), and export the
        # winning genomes as hunter seeds.
        try:
            from autotrader.postmortem import maybe_run_postmortem

            maybe_run_postmortem(cfg)
        except Exception:  # noqa: BLE001 — the loop must survive
            logger.exception("post-mortem failed")

        # Nightly edge-model retrain (labels from tape + library bootstrap).
        try:
            from autotrader.edge_model import train_edge_model

            train_edge_model(include_bootstrap=True)
        except Exception:  # noqa: BLE001
            logger.exception("edge model training failed")

        registry = Registry(state_dir() / REGISTRY_NAME)

        # Nightly research-director pass over the full evidence base
        # (post-mortem oracle gap, edge calibration, gate rejects, registry).
        # Its validated amendments merge into state for the NEXT cycle.
        try:
            from autotrader.research_director import run_research_director

            director = run_research_director(cfg, state, registry)
            if director.get("amendments"):
                merge_amendments(state, dict(director["amendments"]))
        except Exception:  # noqa: BLE001 — the loop must survive
            logger.exception("research director failed")

        # Weekly quantum pass (Saturday nights): drain the Braket queue the
        # director (or an operator) queued during the week. Cost-gated
        # internally against the vibe-trading-braket budget.
        if dt.datetime.now().weekday() == 5:
            try:
                from autotrader.quantum.runner import drain_queue

                ran = drain_queue()
                if ran:
                    logger.info("quantum runner completed %d task(s)", len(ran))
            except Exception:  # noqa: BLE001 — the loop must survive
                logger.exception("quantum runner failed")

        # Nightly hunter-gate A/B over the labeled setup library; winners are
        # staged as gate proposals for review, never auto-applied.
        try:
            from autotrader.gate_ab import run_gate_ab

            gate_ab = run_gate_ab()
            if gate_ab.get("winner"):
                logger.info("gate A/B winner: %s", gate_ab["winner"])
        except Exception:  # noqa: BLE001 — the loop must survive
            logger.exception("gate A/B failed")

        # Daily replay scoreboard: champions vs latest tape on the $1,000
        # book, appended to state/daily_replay.jsonl (+ .md report).
        try:
            from autotrader.scripts.daily_replay import run_replay

            replay = run_replay()
            logger.info(
                "daily replay: %s day P&L $%+.2f (target $%.0f)",
                replay["date"], replay["day_pnl_usd"], replay["target_usd"],
            )
        except Exception:  # noqa: BLE001 — the loop must survive
            logger.exception("daily replay failed")

        summaries: dict[str, Any] = {}
        cycle_trades = 0
        try:
            for sleeve in sleeves:
                if stopper.stop:
                    break
                try:
                    summaries[sleeve] = evolve_sleeve(
                        sleeve, cfg, cfg_overrides, registry,
                        snapshot=SNAPSHOT_BASE, workers=workers,
                    )
                except Exception:  # noqa: BLE001 — one bad sleeve must not kill the loop
                    if stopper.stop:
                        break
                    logger.exception("[%s] sleeve failed this cycle; continuing", sleeve)
        finally:
            registry.export_json(state_dir() / "daytrade_registry_export.json")

        if stopper.stop:
            registry.close()
            break

        # Rough simulated-trade counter: sum of trade_count over leaderboard-
        # recorded results is an undercount; use best-effort registry scan.
        try:
            cur = registry._conn.execute(  # noqa: SLF001 — internal daemon use
                "SELECT COALESCE(SUM(trade_count), 0) FROM results"
            )
            total_registry_trades = int(cur.fetchone()[0])
            cycle_trades = max(total_registry_trades - state.get("_registry_trades_seen", 0), 0)
            state["_registry_trades_seen"] = total_registry_trades
            state["simulated_trades_total"] = (
                int(state.get("simulated_trades_total", 0)) + cycle_trades
            )
        except Exception:  # noqa: BLE001
            pass

        _update_champion(state, summaries)

        # Export passers for the livetrader day desk (top-priority seeds).
        all_passers = [
            p for s in summaries.values() for p in s.get("gate_passers", [])
        ]
        if all_passers:
            export_gate_passers_for_livetrader(
                all_passers, path=state_dir() / SEED_EXPORT_NAME
            )
            logger.info("exported %d daytrade gate-passers for livetrader", len(all_passers))

        # Cycle-level LLM check-in + amendments.
        all_results = [
            r for s in summaries.values()
            for r in ([s["best_ever"]] if s.get("best_ever") else [])
        ]
        gate_passer_count = len(all_passers)
        record = checkin_cycle(
            cfg,
            build_cycle_payload(
                "daytrade:" + "+".join(sleeves), cycle, all_results, {},
                next(iter(summaries.values()), {}).get("leaderboard", []),
                objective=objective_text(cfg, state),
            ),
        )
        accepted = dict(record.get("amendments", {}))
        merge_amendments(state, dict(accepted))

        # Never let an amendment widen timeframe out of the day-trade band.
        state.setdefault("gene_bounds", {})["timeframe"] = DAYTRADE_TIMEFRAME_BOUNDS

        _escalate_ladder(cfg, state)

        # Continuous learning: distill this cycle's evidence into the ledger.
        try:
            from autotrader.lessons import mine_lessons

            mine_lessons(registry_path=state_dir() / REGISTRY_NAME)
        except Exception:  # noqa: BLE001 — learning must not break searching
            logger.exception("lessons mining failed")

        best_oos = max(
            (s["best_ever"]["oos_pass_rate"] for s in summaries.values() if s.get("best_ever")),
            default=0.0,
        )
        state["cycle_history"] = (state.get("cycle_history", []) + [{
            "cycle": cycle,
            "when": f"{dt.datetime.now():%Y-%m-%d %H:%M}",
            "best_oos_pass": best_oos,
            "gate_passers": gate_passer_count,
            "simulated_trades": cycle_trades,
            "amendments_accepted": len(accepted),
        }])[-100:]
        state["cycle"] = cycle + 1
        save_state(state)
        report = write_track_record(cfg, state)
        registry.close()
        logger.info(
            "daytrade cycle %d done: best green-day rate %.0f%%, gate passers %d, "
            "~%s simulated trades, report %s",
            cycle, best_oos * 100, gate_passer_count, f"{cycle_trades:,}", report,
        )

        if max_cycles and state["cycle"] >= max_cycles:
            logger.info("reached --max-cycles=%d; stopping", max_cycles)
            break
        if not stopper.stop and cooldown_s > 0:
            time.sleep(cooldown_s)

    save_state(state)
    logger.info("daytrade daemon stopped at cycle %d (state saved)", state["cycle"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Intraday daytrade replay daemon")
    parser.add_argument("--sleeves", default="equities,crypto,equities_penny")
    parser.add_argument("--config", default=None, help="JSON config overrides")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 = run forever")
    parser.add_argument("--refresh-data-every", type=int, default=3)
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
        workers=args.workers,
        max_cycles=args.max_cycles,
        refresh_data_every=args.refresh_data_every,
        cooldown_s=args.cooldown,
    )


if __name__ == "__main__":
    sys.exit(main())
