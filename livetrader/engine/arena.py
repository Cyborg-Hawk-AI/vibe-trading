"""The arena: a live tournament of genome-driven paper traders.

Dozens of traders run the same live tape simultaneously, each with its own
paper account. On an evolution tick (default every 30 min) the arena ranks
everyone by live fitness, culls the bottom quartile, and replaces them with
mutations/crossovers of the leaders plus fresh random genomes — natural
selection running against reality instead of history.

Fitness (live): compounded return, penalized by drawdown beyond the mission
cap and by inactivity (a trader that never trades can't prove anything).

Champion seeding: genomes from the historical autotrader's champion/state
are injected at startup so live evolution starts from what the overnight
search already learned.
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path

from autotrader.strategy_space import (
    _repair,
    apply_gene_bounds,
    crossover,
    genome_id,
    mutate,
    random_genome,
)

# Day-desk discipline: every genome born or mutated in THIS process stays on
# the 5m/15m live horizons (live TF_MINUTES indices 0-1). Historical genomes
# carrying autotrader intraday indices (3=15m, 4=5m) are remapped in _seed.
apply_gene_bounds({"timeframe": [0, 1]})

from livetrader.config import ArenaConfig, SimConfig, state_dir
from livetrader.engine import journal
from livetrader.engine.trader import LiveTrader
from livetrader.feeds.base import MarketData
from livetrader.news.rss import NewsStore
from livetrader.sim.broker import PaperBook

logger = logging.getLogger("livetrader.arena")

SLEEVES = ("mixed", "crypto", "equities", "penny", "stocks_in_play")
HUNTER_SLEEVE = "sip_hunter"


def live_fitness(stats: dict, arena_cfg: ArenaConfig) -> float:
    ret = stats.get("return", 0.0)
    dd = stats.get("max_drawdown", 0.0)
    trades = stats.get("closed_trades", 0)
    fitness = ret
    if dd > arena_cfg.max_drawdown:
        fitness -= (dd - arena_cfg.max_drawdown) * 2.0
    if trades == 0 and stats.get("age_s", 0) > 3600:
        # Idle penalty forces exploration — except for hunters, whose
        # no-trade days ARE the strategy (empty hot list = stay flat).
        if stats.get("sleeve") != HUNTER_SLEEVE:
            fitness -= 0.005
    return fitness


def hunter_fitness(stats: dict, arena_cfg: ArenaConfig) -> float:
    """Net $/trade expectancy with a drawdown penalty (no idle penalty)."""
    trades = stats.get("closed_trades", 0)
    if trades >= 3:
        fit = stats.get("realized_pnl", 0.0) / trades
    else:
        # Not enough evidence yet: fall back to book return (scaled so a
        # +1% book move ~ $1/trade on the $100 books).
        fit = stats.get("return", 0.0) * 100.0
    dd = stats.get("max_drawdown", 0.0)
    if dd > arena_cfg.max_drawdown:
        fit -= (dd - arena_cfg.max_drawdown) * 50.0
    return fit


class Arena:
    def __init__(
        self,
        market: MarketData,
        news: NewsStore,
        arena_cfg: ArenaConfig,
        sim_cfg: SimConfig,
        seed_genomes: list[dict] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.market = market
        self.news = news
        self.cfg = arena_cfg
        self.sim_cfg = sim_cfg
        self.rng = rng or random.Random()
        self.traders: list[LiveTrader] = []
        self.generation = 0
        self.total_evaluations = 0
        self.total_fills = 0
        self.champion: dict | None = None       # best live snapshot ever seen
        self.retired: list[dict] = []           # culled trader stats (bounded)
        self._seed(seed_genomes or [])

    # ------------------------------------------------------------------ setup
    def _new_trader(self, genome: dict, sleeve: str | None = None) -> LiveTrader:
        genome = _repair(dict(genome))
        gid = genome_id(genome)
        sleeve = sleeve or self.rng.choice(SLEEVES)
        cash = (
            getattr(self.cfg, "hunter_cash", 100.0)
            if sleeve == HUNTER_SLEEVE else self.sim_cfg.starting_cash
        )
        book = PaperBook(cash=cash, sim=self.sim_cfg)
        trader = LiveTrader(genome=genome, gid=gid, book=book, sleeve=sleeve)
        trader.arena_cfg = self.cfg  # type: ignore[attr-defined]
        if sleeve == HUNTER_SLEEVE:
            trader.max_entries_per_day = int(
                getattr(self.cfg, "hunter_max_entries_per_day", 25)
            )
        return trader

    @staticmethod
    def _remap_timeframe(genome: dict) -> dict:
        """Map historical timeframe indices onto the live horizons.

        Live TF_MINUTES index: 0=5m, 1=15m, 2=60m. Daytrade genomes carry
        autotrader TIMEFRAMES indices 3 (15m) and 4 (5m); legacy daily-bar
        champions are forced to <=15m.
        """
        g = dict(genome)
        hist_tf = int(g.get("timeframe", 0))
        g["timeframe"] = {3: 1, 4: 0}.get(hist_tf, min(hist_tf, 1))
        return g

    @staticmethod
    def _hunter_seed_genomes(limit: int = 20) -> list[dict]:
        """Post-mortem winner genomes (yesterday's best DNA), newest first."""
        try:
            from autotrader.bootstrap import state_dir as auto_state_dir
            from autotrader.validation_ladder import load_gate_passer_seeds

            rows = load_gate_passer_seeds(
                limit=limit, path=auto_state_dir() / "postmortem_winners.json",
            )
            return [r["genome"] for r in rows if isinstance(r.get("genome"), dict)]
        except Exception:  # noqa: BLE001 — seeds are optional
            return []

    def _seed(self, seed_genomes: list[dict]) -> None:
        n = self.cfg.n_traders
        sip_n = max(2, int(n * getattr(self.cfg, "sip_trader_fraction", 0.2)))
        for genome in seed_genomes[: n // 2]:
            try:
                self.traders.append(self._new_trader(self._remap_timeframe(genome)))
            except Exception:  # noqa: BLE001 — a malformed seed must not stop the arena
                continue
        # Guarantee a SIP cohort so the sleeve gets selection pressure.
        while sum(1 for t in self.traders if t.sleeve == "stocks_in_play") < sip_n:
            self.traders.append(self._new_trader(random_genome(self.rng), sleeve="stocks_in_play"))
        while len(self.traders) < n:
            self.traders.append(self._new_trader(random_genome(self.rng)))
        # Trim if over (seeds + SIP overshoot)
        self.traders = self.traders[:n]
        # Hunter cohort on top of n_traders: dedicated books, post-mortem DNA
        # preferred, random genomes otherwise.
        n_hunters = int(getattr(self.cfg, "hunter_traders", 0))
        hunter_seeds = self._hunter_seed_genomes(limit=n_hunters)
        for i in range(n_hunters):
            try:
                genome = (
                    self._remap_timeframe(hunter_seeds[i])
                    if i < len(hunter_seeds) else random_genome(self.rng)
                )
                self.traders.append(self._new_trader(genome, sleeve=HUNTER_SLEEVE))
            except Exception:  # noqa: BLE001
                self.traders.append(
                    self._new_trader(random_genome(self.rng), sleeve=HUNTER_SLEEVE)
                )
        logger.info(
            "arena seeded: %d traders (%d from historical champions, %d SIP, "
            "%d hunters at $%.0f books, %d from post-mortem winners)",
            len(self.traders), min(len(seed_genomes), n // 2),
            sum(1 for t in self.traders if t.sleeve == "stocks_in_play"),
            sum(1 for t in self.traders if t.sleeve == HUNTER_SLEEVE),
            getattr(self.cfg, "hunter_cash", 100.0),
            min(len(hunter_seeds), n_hunters),
        )

    # ------------------------------------------------------------------ ticks
    def evaluate_all(self, regime: str = "neutral") -> int:
        """One evaluation pass over every trader. Returns action count."""
        # Cohort-wide held counts so hunters respect the pile-on cap.
        held: dict[str, int] = {}
        for t in self.traders:
            if t.sleeve != HUNTER_SLEEVE:
                continue
            for sym in t.book.positions:
                held[sym] = held.get(sym, 0) + 1
        for t in self.traders:
            if t.sleeve == HUNTER_SLEEVE:
                t._cohort_held = held  # type: ignore[attr-defined]

        n_actions = 0
        for trader in self.traders:
            try:
                actions = trader.evaluate(self.market, self.news, regime=regime)
            except Exception:  # noqa: BLE001 — one broken trader must not stall the tick
                logger.exception("trader %s evaluate failed", trader.gid)
                continue
            self.total_evaluations += 1
            if actions:
                self.total_fills += sum(
                    1 for a in actions
                    if a.startswith(("BUY", "SHORT", "EXIT", "STOP", "HARD",
                                     "TAKE", "TIME", "LOST"))
                )
                for a in actions[:4]:
                    logger.info("[%s/%s] %s", trader.gid[:8], trader.sleeve, a)
                n_actions += len(actions)
                # Refresh held counts after hunter fills this tick.
                if trader.sleeve == HUNTER_SLEEVE:
                    held = {}
                    for t in self.traders:
                        if t.sleeve != HUNTER_SLEEVE:
                            continue
                        for sym in t.book.positions:
                            held[sym] = held.get(sym, 0) + 1
                    for t in self.traders:
                        if t.sleeve == HUNTER_SLEEVE:
                            t._cohort_held = held  # type: ignore[attr-defined]
        return n_actions

    def _retire(self, t: LiveTrader, stats: dict) -> None:
        """Liquidate a culled trader and journal its forced exits."""
        n_fills = len(t.book.fills)
        t.book.liquidate(self.market, reason="culled")
        for fill in t.book.fills[n_fills:]:
            if fill.side in ("sell", "cover"):
                journal.record_closed_trade(
                    fill, gid=t.gid, sleeve=t.sleeve,
                    kind=self.market.kind(fill.symbol), news=self.news,
                )
        self.retired.append({"gid": t.gid, "sleeve": t.sleeve,
                             **{k: stats[k] for k in
                                ("return", "max_drawdown", "closed_trades", "win_rate")}})

    def _evolve_hunters(self, now: float) -> tuple[list[LiveTrader], int]:
        """In-cohort tournament for the hunter desk.

        Hunters cull/breed ONLY against other hunters so the concentrated
        scalp style can never be bred out by the diversified sleeves.
        Fitness is net $/trade expectancy (see ``hunter_fitness``); newborns
        keep the dedicated hunter book size and prefer post-mortem winner
        DNA when it exists.
        """
        hunters = [t for t in self.traders if t.sleeve == HUNTER_SLEEVE]
        if not hunters:
            return [], 0
        scored: list[tuple[float, LiveTrader, dict]] = []
        immune: list[LiveTrader] = []
        for t in hunters:
            stats = t.snapshot(self.market)
            # Open-position immunity: never cull mid-trade (brackets must finish).
            if t.book.positions or now - t.born_ts < self.cfg.min_track_s:
                immune.append(t)
                continue
            scored.append((hunter_fitness(stats, self.cfg), t, stats))
        scored.sort(reverse=True, key=lambda x: x[0])
        if len(scored) < 4:
            return hunters, 0

        n_cull = max(1, min(int(len(scored) * self.cfg.cull_fraction), len(scored) - 1))
        if n_cull <= 0:
            return hunters, 0
        survivors = [t for _, t, _ in scored[:-n_cull]] + immune
        for _, t, stats in scored[-n_cull:]:
            self._retire(t, stats)

        elites = [_repair(dict(t.genome)) for _, t, _ in scored[: max(3, len(scored) // 4)]]
        for t in survivors:
            t.genome = _repair(dict(t.genome))
        pm_seeds = self._hunter_seed_genomes(limit=8)
        newborns: list[LiveTrader] = []
        for _ in range(n_cull):
            roll = self.rng.random()
            if pm_seeds and roll < 0.30:
                child = self._remap_timeframe(self.rng.choice(pm_seeds))
            elif not elites or roll < 0.30 + self.cfg.fresh_random_fraction * 0.5:
                child = random_genome(self.rng)
            elif len(elites) >= 2 and roll < 0.75:
                child = crossover(self.rng.choice(elites), self.rng.choice(elites), self.rng)
            else:
                child = mutate(self.rng.choice(elites), self.rng)
            newborns.append(self._new_trader(child, sleeve=HUNTER_SLEEVE))
        return survivors + newborns, n_cull

    def evolve(self) -> dict:
        """Tournament selection against live results."""
        now = time.time()
        scored: list[tuple[float, LiveTrader, dict]] = []
        immune: list[LiveTrader] = []
        for t in self.traders:
            if t.sleeve == HUNTER_SLEEVE:
                continue  # hunters evolve in their own cohort below
            stats = t.snapshot(self.market)
            # Open-position immunity: brackets finish before the trader is judged.
            if t.book.positions or now - t.born_ts < self.cfg.min_track_s:
                immune.append(t)
                continue
            scored.append((live_fitness(stats, self.cfg), t, stats))
        scored.sort(reverse=True, key=lambda x: x[0])

        # champion bookkeeping (must respect the mission drawdown cap)
        for fit, t, stats in scored[:5]:
            if stats["return"] > (self.champion or {}).get("return", -9e9) and \
                    stats["max_drawdown"] <= self.cfg.max_drawdown:
                self.champion = {**stats, "fitness": round(fit, 6),
                                 "when": f"{time.strftime('%Y-%m-%d %H:%M')}"}

        if len(scored) < 2:
            new_hunters, hunters_culled = self._evolve_hunters(now)
            self.traders = [t for t in self.traders if t.sleeve != HUNTER_SLEEVE] + new_hunters
            self.generation += 1
            return {
                "generation": self.generation, "culled": hunters_culled,
                "newborn": hunters_culled, "hunters": len(new_hunters),
                "best_fitness": 0.0, "best_gid": None, "best_return": 0.0,
                "champion": self.champion,
            }

        n_cull = max(1, min(int(len(scored) * self.cfg.cull_fraction), len(scored) - 1))
        survivors = [t for _, t, _ in scored[:-n_cull]] + immune
        culled = scored[-n_cull:]
        for _, t, stats in culled:
            self._retire(t, stats)
        self.retired = self.retired[-200:]

        # replacements: mutations/crossovers of leaders + fresh randoms
        elites = [_repair(dict(t.genome)) for _, t, _ in scored[: max(4, len(scored) // 4)]]
        # Keep survivors' DNA complete so the next evolve never KeyErrors.
        for t in survivors:
            t.genome = _repair(dict(t.genome))
        newborns: list[LiveTrader] = []
        for _ in range(n_cull):
            if not elites or self.rng.random() < self.cfg.fresh_random_fraction:
                child = random_genome(self.rng)
            elif len(elites) >= 2 and self.rng.random() < 0.4:
                child = crossover(self.rng.choice(elites), self.rng.choice(elites), self.rng)
            else:
                child = mutate(self.rng.choice(elites), self.rng)
            newborns.append(self._new_trader(child))

        new_hunters, hunters_culled = self._evolve_hunters(now)
        self.traders = survivors + newborns + new_hunters
        self.generation += 1

        best = scored[0] if scored else None
        summary = {
            "generation": self.generation,
            "culled": len(culled) + hunters_culled,
            "newborn": len(newborns) + hunters_culled,
            "hunters": len(new_hunters),
            "best_fitness": round(best[0], 6) if best else 0.0,
            "best_gid": best[1].gid if best else None,
            "best_return": best[2]["return"] if best else 0.0,
            "champion": self.champion,
        }
        logger.info(
            "evolve gen %d: culled %d (+%d hunters), best %s ret %.2f%% fit %.4f",
            self.generation, len(culled), hunters_culled,
            (best[1].gid[:8] if best else "-"),
            (best[2]["return"] * 100 if best else 0.0),
            (best[0] if best else 0.0),
        )
        return summary

    # ------------------------------------------------------------------ reporting
    def leaderboard(self, top: int = 10) -> list[dict]:
        stats = [t.snapshot(self.market) for t in self.traders]
        stats.sort(reverse=True, key=lambda s: s.get("return", 0.0))
        return stats[:top]

    def aggregate(self) -> dict:
        stats = [t.snapshot(self.market) for t in self.traders]
        total_eq = sum(s["equity"] for s in stats)
        # Books differ in size (hunter books vs standard); aggregate return
        # must be computed against each book's own starting cash.
        total_start = sum(t.book.starting_cash for t in self.traders)
        trades = sum(s["closed_trades"] for s in stats)
        sig_evals = sum(s["evaluations"] for s in stats)
        return {
            "traders": len(stats),
            "hunters": sum(1 for t in self.traders if t.sleeve == HUNTER_SLEEVE),
            "generation": self.generation,
            "aggregate_return": round(total_eq / total_start - 1.0, 6) if total_start else 0.0,
            "closed_trades": trades,
            "open_positions": sum(s["open_positions"] for s in stats),
            "signal_evaluations": sig_evals,
            "champion": self.champion,
        }

    # ------------------------------------------------------------------ persistence
    def save(self, path: Path | None = None) -> Path:
        path = path or (state_dir() / "arena_state.json")
        payload = {
            "generation": self.generation,
            "champion": self.champion,
            "retired": self.retired[-50:],
            "traders": [
                {"genome": t.genome, "sleeve": t.sleeve,
                 **{k: v for k, v in t.snapshot(self.market).items()
                    if k not in ("genome",)}}
                for t in self.traders
            ],
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        tmp.replace(path)
        return path

    @staticmethod
    def load_seed_genomes() -> list[dict]:
        """Champion + leaderboard genomes from BOTH programs' saved state."""
        seeds: list[dict] = []
        # historical autotrader champion
        auto_state = Path(__file__).resolve().parents[2] / "autotrader" / "state"
        daemon_state = auto_state / "daemon_state.json"
        try:
            data = json.loads(daemon_state.read_text(encoding="utf-8"))
            champ = data.get("champion") or {}
            if champ.get("genome"):
                seeds.append(champ["genome"])
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        # registry export (best_ever per sleeve)
        try:
            reg = json.loads((auto_state / "registry_export.json").read_text(encoding="utf-8"))
            rows = reg if isinstance(reg, list) else reg.get("results", [])
            ranked = sorted(
                (r for r in rows if isinstance(r, dict) and r.get("genome")),
                key=lambda r: r.get("oos_pass_rate", 0.0), reverse=True,
            )
            seeds += [r["genome"] for r in ranked[:10]]
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            pass
        # previous livetrader arena state
        try:
            prev = json.loads((state_dir() / "arena_state.json").read_text(encoding="utf-8"))
            ranked = sorted(
                (t for t in prev.get("traders", []) if t.get("genome")),
                key=lambda t: t.get("return", 0.0), reverse=True,
            )
            seeds += [t["genome"] for t in ranked[:16]]
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        # Fresh gate-passers exported by the historical daemon
        try:
            from autotrader.validation_ladder import load_gate_passer_seeds
            seeds += [
                (r.get("genome") if isinstance(r.get("genome"), dict) else r)
                for r in load_gate_passer_seeds(16)
                if isinstance(r, dict)
            ]
        except Exception:  # noqa: BLE001
            pass
        # dedupe preserving order
        seen: set[str] = set()
        unique: list[dict] = []
        for g in seeds:
            if not isinstance(g, dict):
                continue
            try:
                g = _repair(dict(g))
            except Exception:  # noqa: BLE001
                continue
            gid = genome_id(g)
            if gid not in seen:
                seen.add(gid)
                unique.append(g)
        return unique
