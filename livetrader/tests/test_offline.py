"""Offline smoke tests: no network, no LLM. Synthetic tape -> trades happen."""

from __future__ import annotations

import math
import os
import random
import time

os.environ["AUTOTRADER_LLM_DISABLE"] = "1"

from livetrader.config import ArenaConfig, NewsConfig, SimConfig  # noqa: E402
from livetrader.engine.arena import Arena, live_fitness  # noqa: E402
from livetrader.engine.trader import LiveTrader  # noqa: E402
from livetrader.feeds.base import MarketData, Quote  # noqa: E402
from livetrader.news.rss import Headline, NewsStore, score_headline, tag_topics  # noqa: E402
from livetrader.sim.broker import PaperBook  # noqa: E402

from autotrader.strategy_space import genome_id, random_genome  # noqa: E402


def synth_market(n_symbols: int = 8, n_bars: int = 500, trend: float = 0.0008) -> MarketData:
    """Uptrending synthetic 1m tape so momentum strategies find entries."""
    market = MarketData()
    rng = random.Random(7)
    now = time.time()
    for i in range(n_symbols):
        sym = f"SYN{i}-USDT"
        market.register(sym, "crypto")
        px = 100.0 * (1 + i)
        bars = []
        for b in range(n_bars):
            drift = trend * (1.5 if i % 2 == 0 else 0.5)
            px *= math.exp(rng.gauss(drift, 0.002))
            o = px * (1 + rng.gauss(0, 0.0005))
            h = max(o, px) * (1 + abs(rng.gauss(0, 0.001)))
            l = min(o, px) * (1 - abs(rng.gauss(0, 0.001)))  # noqa: E741
            bars.append((now - (n_bars - b) * 60.0, o, h, l, px, 1000.0))
        market.push_bars(sym, bars)
        market.push_quote(sym, Quote(bid=px * 0.9995, ask=px * 1.0005, last=px, ts=now))
    return market


def test_tape_resample() -> None:
    market = synth_market(1, 300)
    a1 = market.arrays("SYN0-USDT", tf_minutes=1)
    a5 = market.arrays("SYN0-USDT", tf_minutes=5)
    assert a1 is not None and a5 is not None
    assert len(a5["close"]) < len(a1["close"])
    assert abs(a5["close"][-1] - a1["close"][-1]) < 1e-9
    assert (a5["high"] >= a5["low"]).all()
    print("PASS tape resample")


def test_broker_roundtrip() -> None:
    market = synth_market(1, 50)
    sim = SimConfig(starting_cash=200.0)
    book = PaperBook(cash=200.0, sim=sim)
    fill = book.buy(market, "SYN0-USDT", 100.0, reason="test")
    assert fill is not None and fill.qty > 0
    assert book.cash < 200.0
    sell = book.sell(market, "SYN0-USDT", reason="test")
    assert sell is not None
    # full friction stack: round trip must cost something
    assert book.equity(market) < 200.0
    assert book.equity(market) > 195.0
    stats = book.stats(market)
    assert stats["closed_trades"] == 1
    print(f"PASS broker roundtrip (friction cost {200 - book.equity(market):.4f})")


def test_trader_trades_on_synthetic_tape() -> None:
    market = synth_market(8, 600)
    news = NewsStore(NewsConfig())
    sim = SimConfig(starting_cash=200.0)
    rng = random.Random(11)
    # permissive genome so the test is deterministic-ish: no gates, 5m tf
    g = random_genome(rng)
    g.update({"use_rsi_gate": 0, "use_trend_gate": 0, "use_breakout_gate": 0,
              "use_regime_gate": 0, "use_volume_gate": 0,
              "use_pullback_entry": 0, "use_range_compress": 0,
              "mom_skip_bars": 0,
              "timeframe": 0, "momentum_lookback": 10, "max_positions": 5,
              "max_weight": 0.3, "target_vol_annual": 0.8, "vol_lookback": 10})
    trader = LiveTrader(genome=g, gid=genome_id(g),
                        book=PaperBook(cash=200.0, sim=sim), sleeve="crypto")
    actions = trader.evaluate(market, news)
    assert trader.evaluations > 0
    assert any(a.startswith("BUY") for a in actions), f"no buys: {actions}"
    assert len(trader.book.positions) > 0
    print(f"PASS trader entries ({len(trader.book.positions)} positions opened)")


def test_arena_evolution() -> None:
    market = synth_market(8, 600)
    news = NewsStore(NewsConfig())
    arena_cfg = ArenaConfig(n_traders=12, min_track_s=0.0)
    arena = Arena(market, news, arena_cfg, SimConfig(starting_cash=200.0),
                  seed_genomes=[random_genome(random.Random(3))])
    assert len(arena.traders) == 12
    arena.evaluate_all()
    summary = arena.evolve()
    assert summary["generation"] == 1
    assert len(arena.traders) == 12  # culled replaced 1:1
    assert summary["culled"] >= 1
    board = arena.leaderboard(5)
    assert len(board) == 5
    agg = arena.aggregate()
    assert agg["signal_evaluations"] > 0
    path = arena.save()
    assert path.exists()
    print(f"PASS arena evolution (gen 1, {agg['signal_evaluations']} evals, "
          f"{agg['closed_trades']} closed trades)")


def test_news_scoring() -> None:
    assert score_headline("Stocks surge to record as Fed cuts rates loom") > 0
    assert score_headline("Oil plunges as war escalates; sanctions widen") < 0
    topics = tag_topics("Hurricane threatens Gulf refineries, crude jumps")
    assert "weather" in topics and "energy" in topics
    store = NewsStore(NewsConfig())
    store.add(Headline(ts=time.time(), source="t", title="NVDA beats, shares surge",
                       sentiment=0.8, topics=[], symbols=["NVDA"]))
    assert store.symbol_sentiment("NVDA") > 0
    assert store.market_sentiment() > 0
    print("PASS news scoring")


def test_fitness_ranks_dd() -> None:
    cfg = ArenaConfig()
    good = {"return": 0.10, "max_drawdown": 0.05, "closed_trades": 20, "age_s": 5000}
    reckless = {"return": 0.12, "max_drawdown": 0.30, "closed_trades": 20, "age_s": 5000}
    assert live_fitness(good, cfg) > live_fitness(reckless, cfg)
    print("PASS fitness drawdown penalty")


if __name__ == "__main__":
    test_tape_resample()
    test_broker_roundtrip()
    test_trader_trades_on_synthetic_tape()
    test_arena_evolution()
    test_news_scoring()
    test_fitness_ranks_dd()
    print("\nALL OFFLINE TESTS PASSED")
