"""livetrader daemon: the real-time twin of the autotrader daemon.

    python -m livetrader.daemon

One long-running process, five threads, zero human input:

  feeds   crypto (Binance, 24/7)  +  equities/pennies/movers (yfinance, RTH)
          + macro tape (oil, VIX, dollar, rates, gold)
  news    RSS (markets, geopolitics, energy, weather, Fed) + NWS alerts,
          keyword-scored locally, periodically assessed by Kimi/Bedrock
  arena   N genome-driven paper traders evaluating every few seconds,
          culled and re-bred against LIVE results every evolve interval

The daemon check-ins mirror the autotrader's: every ``checkin_every_s`` the
live leaderboard + news/macro context go to Kimi-K3 (Bedrock fallback),
which replies with a memo and bounded amendments (arena knobs only).
Everything is simulation — no order ever leaves this process.

Overnight behavior: crypto keeps the arena fully live 24/7; equity traders
idle while the market is closed and wake at 9:30 ET with warm history.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import signal
import sys
import threading
import time
from typing import Any

from autotrader import llm as llm_router
from autotrader.knowledge import prompt_suffix

from livetrader.config import LiveTraderConfig, state_dir
from livetrader.engine import recorder, reports
from livetrader.engine.arena import Arena
from livetrader.feeds.base import MarketData
from livetrader.feeds.crypto import CryptoFeed
from livetrader.feeds.equities import EquityFeed, market_open_now
from livetrader.feeds.macro import MacroFeed, risk_regime
from livetrader.news import sentiment
from livetrader.news.buzz import BuzzFeed, BuzzStore
from livetrader.news.rss import NewsFeed, NewsStore

logger = logging.getLogger("livetrader.daemon")

CHECKIN_SYSTEM = """You are the research director for a LIVE paper-trading
arena (simulation only). You receive the live leaderboard, aggregate stats,
news/macro context, and the current arena knobs. Reply with a short memo and
a JSON block of amendments, all optional, bounded:

{"evolve_every_s": 600..7200, "cull_fraction": 0.10..0.40,
 "eval_interval_s": 2..30, "notes": "<= 120 words"}

Focus on: are the surviving genomes actually distinct? is turnover eating
returns (fees vs realized pnl)? should selection pressure rise or fall?"""

AMEND_LIMITS = {
    "evolve_every_s": (600.0, 7200.0),
    "cull_fraction": (0.10, 0.40),
    "eval_interval_s": (2.0, 30.0),
}


def validate_amendments(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, (lo, hi) in AMEND_LIMITS.items():
        if key in raw:
            try:
                out[key] = max(lo, min(hi, float(raw[key])))
            except (TypeError, ValueError):
                continue
    return out


class LiveDaemon:
    def __init__(self, cfg: LiveTraderConfig) -> None:
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.market = MarketData(max_bar_history=cfg.feeds.max_bar_history)
        self.news = NewsStore(cfg.news)
        self.buzz = BuzzStore()
        self.crypto = CryptoFeed(self.market, cfg.feeds, self.stop_event)
        self.equity = EquityFeed(
            self.market, cfg.feeds, self.stop_event,
            spread_tiers=cfg.sim.equity_spread_bps_tiers,
            buzz=self.buzz, news=self.news,
        )
        self.newsfeed = NewsFeed(
            self.news, cfg.news, self.stop_event,
            universe_provider=self._bare_universe,
        )
        self.buzzfeed = BuzzFeed(
            self.buzz, self.news, cfg.news, self.stop_event,
            universe_provider=self._bare_universe,
        )
        self.macro = MacroFeed(self.market, cfg.feeds, self.stop_event)
        self.arena: Arena | None = None
        self.started_ts = time.time()
        self.checkin_notes = ""

    # ------------------------------------------------------------------ helpers
    def _bare_universe(self) -> set[str]:
        bare = set()
        for s in self.market.symbols():
            bare.add(s.split("-")[0])
        return bare

    def _install_signals(self) -> None:
        def handle(signum: int, _frame: Any) -> None:
            logger.info("signal %s — saving state and stopping", signum)
            self.stop_event.set()
        signal.signal(signal.SIGINT, handle)
        signal.signal(signal.SIGTERM, handle)

    # ------------------------------------------------------------------ LLM check-in
    def _checkin(self) -> None:
        if self.arena is None:
            return
        agg = self.arena.aggregate()
        board = self.arena.leaderboard(top=8)
        macro = self.market.macro
        assessment = self.news.get_llm_assessment()
        payload = json.dumps({
            "uptime_h": round((time.time() - self.started_ts) / 3600, 2),
            "market_open": market_open_now(),
            "aggregate": agg,
            "leaderboard": [
                {k: s[k] for k in ("gid", "sleeve", "timeframe_min", "return",
                                    "max_drawdown", "closed_trades", "win_rate",
                                    "open_positions")}
                for s in board
            ],
            "macro": macro.describe(),
            "news_sentiment": round(self.news.market_sentiment(), 3),
            "llm_macro_view": assessment.get("notes", ""),
            "current_knobs": {
                "evolve_every_s": self.cfg.arena.evolve_every_s,
                "cull_fraction": self.cfg.arena.cull_fraction,
                "eval_interval_s": self.cfg.arena.eval_interval_s,
            },
        }, default=str)[:11000]
        reply = llm_router.chat(
            self.cfg.llm, CHECKIN_SYSTEM + prompt_suffix(), payload, max_tokens=800,
        )
        if not reply:
            logger.info("check-in skipped (no LLM provider reachable)")
            return
        amendments = validate_amendments(llm_router.extract_json_block(reply))
        for key, value in amendments.items():
            setattr(self.cfg.arena, key, value)
        self.checkin_notes = reply.strip()[:1500]
        record = {
            "ts": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
            "aggregate": agg, "amendments": amendments, "memo": self.checkin_notes,
        }
        path = state_dir() / "checkins.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        logger.info("check-in done: %d amendments %s", len(amendments), amendments or "")

    # ------------------------------------------------------------------ reporting
    def _next_daily_report_ts(self) -> float:
        """Next occurrence of daily_report_hour_utc (a few min past the hour)."""
        now = dt.datetime.now(dt.timezone.utc)
        target = now.replace(
            hour=int(self.cfg.arena.daily_report_hour_utc),
            minute=5, second=0, microsecond=0,
        )
        if target <= now:
            target += dt.timedelta(days=1)
        return target.timestamp()

    def _daily_report(self) -> None:
        """After ET close: aggregate the session's realized P&L + scoreboard."""
        try:
            recorder.dump_tape(self.market)
        except Exception:  # noqa: BLE001
            logger.exception("session tape dump failed")
        day = reports.write_daily_report(self.cfg)
        board = reports.write_monthly_scoreboard(self.cfg)
        logger.info(
            "daily report %s: pnl $%+.2f over %d trades | MTD $%+.2f "
            "(%d green / %d red days)",
            day["date"], day["realized_pnl"], day["trades"],
            board["mtd_pnl"], board["win_days"], board["lose_days"],
        )

    def _write_status(self) -> None:
        agg = self.arena.aggregate() if self.arena else {}
        status = {
            "ts": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
            "uptime_s": round(time.time() - self.started_ts),
            "market_open": market_open_now(),
            "regime": risk_regime(self.market.macro),
            "feeds": {
                "crypto_healthy": self.crypto.healthy,
                "equity_healthy": self.equity.healthy,
                "news_healthy": self.newsfeed.healthy,
                "buzz_healthy": self.buzzfeed.healthy,
                "symbols": len(self.market.symbols()),
            },
            "news_sentiment": round(self.news.market_sentiment(), 3),
            "arena": agg,
        }
        (state_dir() / "status.json").write_text(
            json.dumps(status, indent=2, default=str), encoding="utf-8",
        )

    def _write_track_record(self) -> None:
        if self.arena is None:
            return
        agg = self.arena.aggregate()
        champ = self.arena.champion or {}
        uptime_days = max((time.time() - self.started_ts) / 86400.0, 1e-9)
        # Operational 3%/2wk ~ 0.21%/day compounded pace
        daily_pace_needed = self.cfg.arena.target_2week_return / 14.0
        lines = [
            "# Livetrader track record (real-time paper simulation)",
            "",
            f"_Generated {dt.datetime.now():%Y-%m-%d %H:%M}. All results are live-simulated "
            "fills against real quotes with fees, spread, and slippage charged. "
            "No real orders are ever sent._",
            "",
            "## Mission benchmark",
            f"- Operational target: >= {self.cfg.arena.target_2week_return:.0%} per rolling 2 weeks "
            f"(~{daily_pace_needed:.2%}/day pace), "
            f"max drawdown <= {self.cfg.arena.max_drawdown:.0%}.",
            "- Stretch 25%/2wk is diagnostic only and does not block promotion.",
            "",
            "## Arena",
            f"- Uptime: {uptime_days:.2f} days | generation {agg.get('generation')} | "
            f"{agg.get('traders')} live traders",
            f"- Aggregate return across all books: {agg.get('aggregate_return', 0.0):.2%}",
            f"- Closed trades: {agg.get('closed_trades', 0):,} | "
            f"signal evaluations: {agg.get('signal_evaluations', 0):,}",
            "",
            "## Live champion (drawdown-capped best book)",
        ]
        if champ:
            lines += [
                f"- `{champ.get('gid', '?')}` sleeve={champ.get('sleeve')} "
                f"tf={champ.get('timeframe_min')}m",
                f"- Return {champ.get('return', 0.0):.2%} | max DD "
                f"{champ.get('max_drawdown', 0.0):.2%} | trades "
                f"{champ.get('closed_trades', 0)} | win rate {champ.get('win_rate', 0.0):.0%}",
                f"- Recorded {champ.get('when', '?')}",
            ]
        else:
            lines.append("- none yet (no book has beaten the cap-respecting bar)")
        lines += ["", "## Leaderboard (top 10 live books)"]
        for s in self.arena.leaderboard(10):
            lines.append(
                f"- `{s['gid'][:10]}` {s['sleeve']:8s} tf={s['timeframe_min']:>2}m "
                f"ret {s['return']:+.2%} dd {s['max_drawdown']:.2%} "
                f"trades {s['closed_trades']} win {s['win_rate']:.0%}"
            )
        if self.checkin_notes:
            lines += ["", "## Latest research-director memo", "", self.checkin_notes]
        (state_dir() / "live_track_record.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8",
        )

    # ------------------------------------------------------------------ main loop
    def run(self) -> int:
        self._install_signals()
        logger.info("livetrader daemon starting (simulation only — no real orders)")
        self.crypto.start()
        self.equity.start()
        self.newsfeed.start()
        self.buzzfeed.start()
        self.macro.start()

        # Wait for the crypto tape (the 24/7 sleeve) before opening the arena.
        for _ in range(60):
            if self.stop_event.is_set():
                return 0
            if self.crypto.healthy and len(self.market.symbols("crypto")) > 5:
                break
            time.sleep(2)
        seeds = Arena.load_seed_genomes()
        self.arena = Arena(self.market, self.news, self.cfg.arena, self.cfg.sim,
                           seed_genomes=seeds)
        logger.info("arena open: %d traders (%d seeded from prior state)",
                    len(self.arena.traders), len(seeds))

        next_evolve = time.time() + self.cfg.arena.evolve_every_s
        next_checkin = time.time() + self.cfg.checkin_every_s
        next_assess = time.time() + 120.0     # first news assessment soon
        next_save = time.time() + self.cfg.save_every_s
        next_daily = self._next_daily_report_ts()
        last_hb = 0.0

        while not self.stop_event.is_set():
            tick_start = time.time()
            regime = risk_regime(self.market.macro)
            try:
                self.arena.evaluate_all(regime=regime)
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("evaluation tick failed")

            now = time.time()
            if now >= next_assess:
                try:
                    sentiment.assess_with_llm(self.cfg.llm, self.news, self.market.macro)
                except Exception:  # noqa: BLE001
                    logger.exception("news assessment failed")
                next_assess = now + self.cfg.news.llm_assess_every_s
            if now >= next_evolve:
                try:
                    self.arena.evolve()
                except Exception:  # noqa: BLE001
                    logger.exception("evolution tick failed")
                next_evolve = now + self.cfg.arena.evolve_every_s
            if now >= next_checkin:
                try:
                    self._checkin()
                except Exception:  # noqa: BLE001
                    logger.exception("check-in failed")
                next_checkin = now + self.cfg.checkin_every_s
            if now >= next_save:
                try:
                    self.arena.save()
                    self._write_status()
                    self._write_track_record()
                except Exception:  # noqa: BLE001
                    logger.exception("state save failed")
                next_save = now + self.cfg.save_every_s
            if now >= next_daily:
                try:
                    self._daily_report()
                except Exception:  # noqa: BLE001
                    logger.exception("daily report failed")
                next_daily = self._next_daily_report_ts()
            if now - last_hb > 300:
                agg = self.arena.aggregate()
                logger.info(
                    "heartbeat: gen %d | %d traders | agg ret %.2f%% | "
                    "%s trades | %s evals | regime %s | news %.2f | market %s",
                    agg["generation"], agg["traders"],
                    agg["aggregate_return"] * 100, f"{agg['closed_trades']:,}",
                    f"{agg['signal_evaluations']:,}", regime,
                    self.news.market_sentiment(),
                    "OPEN" if market_open_now() else "closed",
                )
                last_hb = now

            elapsed = time.time() - tick_start
            self.stop_event.wait(max(self.cfg.arena.eval_interval_s - elapsed, 0.2))

        # graceful shutdown
        try:
            self.arena.save()
            self._write_status()
            self._write_track_record()
            recorder.dump_tape(self.market)
        except Exception:  # noqa: BLE001
            logger.exception("final save failed")
        logger.info("livetrader daemon stopped")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live paper-trading arena daemon")
    parser.add_argument("--config", default=None, help="JSON overrides file")
    parser.add_argument("--traders", type=int, default=0, help="override arena size")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = LiveTraderConfig.load(args.config)
    if args.traders > 0:
        cfg.arena.n_traders = args.traders
    return LiveDaemon(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
