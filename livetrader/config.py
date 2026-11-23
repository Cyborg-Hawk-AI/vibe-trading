"""Configuration for the live-simulation trader.

Reuses the autotrader's LLM configuration (Kimi primary / Bedrock fallback)
so both programs share credentials at ``~/.config/autotrader/llm.env``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from autotrader.config import LlmConfig

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "livetrader" / "state"


def state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


@dataclass
class FeedConfig:
    # Crypto: Binance public REST (no key). One bookTicker call covers all
    # symbols; klines refresh authoritative 1m bars per symbol.
    crypto_quote_poll_s: float = 3.0
    crypto_bar_refresh_s: float = 30.0
    crypto_warmup_bars: int = 600           # 10h of 1m bars
    crypto_universe_size: int = 24
    # Equities: yfinance batched 1m bars, polled during regular hours.
    equity_bar_poll_s: float = 60.0
    equity_warmup_days: int = 5
    equity_universe_size: int = 60
    penny_universe_size: int = 30
    # Dynamic discovery: pull Yahoo most-actives/gainers into the universe.
    discover_movers: bool = True
    discover_every_s: float = 900.0
    discover_count: int = 15
    # Stocks-in-play filters applied on discovered movers.
    sip_enabled: bool = True
    sip_min_gap_pct: float = 0.04          # |open/prev_close - 1|
    sip_min_rvol: float = 2.0              # vs 20-bar median volume
    sip_price_min: float = 1.0
    sip_price_max: float = 50.0
    sip_orb_minutes: int = 5
    sip_require_catalyst: bool = False     # soft: prefer catalyst when True
    sip_top_k: int = 12                    # size of the ranked hot list (A-list)
    sip_daily_rvol_min: float = 3.0        # rvol vs 20-day avg volume floor
    # Macro tape (oil, VIX, dollar, rates, gold) via yfinance.
    macro_poll_s: float = 300.0
    max_bar_history: int = 1600             # per-symbol 1m bars kept in memory


@dataclass
class NewsConfig:
    poll_s: float = 300.0
    max_headlines_kept: int = 400
    # LLM macro assessment cadence (uses Kimi/Bedrock; keep modest for cost).
    llm_assess_every_s: float = 1800.0
    headline_max_age_s: float = 21600.0     # ignore anything older than 6h
    # Buzz monitor (StockTwits trending + Reddit mentions) poll cadence.
    buzz_poll_s: float = 300.0


@dataclass
class SimConfig:
    starting_cash: float = 200.0            # $100 equities + $100 crypto intent
    fee_bps: float = 10.0                   # per side
    slippage_bps: float = 5.0               # on top of spread model
    # Equity spread model by price tier (no live L1 for free): price -> bps.
    equity_spread_bps_tiers: tuple = ((1.0, 30.0), (5.0, 15.0), (20.0, 8.0), (1e12, 3.0))
    max_trades_log_mb: float = 256.0
    # Short-side borrow: punitive extra fee on hard-to-borrow / SIP names.
    short_borrow_bps: float = 30.0          # extra per side on shorts
    short_borrow_price_max: float = 5.0     # always charge borrow below this price


@dataclass
class ArenaConfig:
    n_traders: int = 48
    eval_interval_s: float = 5.0            # signal evaluation cadence
    evolve_every_s: float = 1800.0          # tournament selection cadence
    cull_fraction: float = 0.25             # bottom quartile replaced
    fresh_random_fraction: float = 0.25     # of replacements, brand-new genomes
    min_track_s: float = 900.0              # immunity for newborn traders
    # Benchmarks (operational gate pace, not the legacy 25% stretch).
    target_2week_return: float = 0.03
    max_drawdown: float = 0.10
    daily_report_hour_utc: int = 21         # after US close
    sip_trader_fraction: float = 0.30       # ~30% of arena traders on SIP sleeve
    # Hunter desk: buzz-driven momentum scalpers with small dedicated books.
    hunter_traders: int = 20                # cohort size (on top of n_traders)
    hunter_cash: float = 100.0              # $ per hunter book (scale knob)
    hunter_max_entries_per_day: int = 25    # per-hunter daily entry cap
    # Discipline gates (evidence from 2026-07-24 live session).
    hunter_require_buzz: bool = True        # skip scan-only (no buzz/catalyst)
    hunter_max_ext_pct: float = 0.06        # skip long when >6% above session VWAP
    hunter_prime_end_et: str = "11:30"      # HH:MM ET; after this, stricter entry
    hunter_max_per_symbol: int = 5          # pile-on cap across the hunter cohort
    hunter_enable_short: bool = True        # fade extended pumps (hunter path only)
    hunter_use_edge_model: bool = True      # EV gate + edge-scaled sizing when model exists
    hunter_lost_vwap_exit: bool = True      # early exit if price loses VWAP while in profit
    # Aspirational monthly income scoreboard target (paper $, not a gate).
    monthly_income_goal_usd: float = 1000.0


@dataclass
class LiveTraderConfig:
    feeds: FeedConfig = field(default_factory=FeedConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    arena: ArenaConfig = field(default_factory=ArenaConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    checkin_every_s: float = 1800.0
    save_every_s: float = 120.0

    @classmethod
    def load(cls, path: str | None = None) -> "LiveTraderConfig":
        cfg = cls()
        if path:
            overrides = json.loads(Path(path).read_text(encoding="utf-8"))
            for section, values in overrides.items():
                target = getattr(cfg, section, None)
                if target is None or not isinstance(values, dict):
                    continue
                for key, value in values.items():
                    if hasattr(target, key):
                        setattr(target, key, value)
        return cfg
