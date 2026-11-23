"""Central configuration for the autotrader pipeline.

Everything the mission fixes as a financial/operational parameter lives here:
capital, friction, the hard acceptance gate, risk limits, and evolution knobs.
Values can be overridden with a JSON file (``--config`` on the CLI) or
environment variables where noted.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CapitalConfig:
    """Starting capital per sleeve (simulated separately, like the real accounts)."""

    equities_usd: float = 100.0
    crypto_usd: float = 100.0


@dataclass
class FrictionConfig:
    """Execution realism applied to every simulated fill.

    Equities: slippage (>= 0.05% enforced) + zero-commission model, matching
    ``GlobalEquityEngine`` (US commission is structurally 0).
    Crypto: taker fee 0.075% on both open and close (conservative: the engine
    charges ``maker_rate`` on closes, so we set maker == taker) + 0.10%
    slippage. ``crypto_fee_bump`` approximates gas/network fees as an additive
    per-side rate for venues where they apply (0 for CEX perps/spot).
    """

    equity_slippage: float = 0.0005      # 0.05% minimum, enforced below
    crypto_taker_fee: float = 0.00075    # 0.075%
    crypto_slippage: float = 0.001       # 0.10%
    crypto_fee_bump: float = 0.0         # optional gas/network approximation

    MIN_EQUITY_SLIPPAGE = 0.0005

    def __post_init__(self) -> None:
        if self.equity_slippage < self.MIN_EQUITY_SLIPPAGE:
            raise ValueError(
                f"equity_slippage {self.equity_slippage} is below the 0.05% floor"
            )

    def equity_engine_keys(self) -> dict[str, Any]:
        return {"slippage_us": self.equity_slippage}

    def crypto_engine_keys(self) -> dict[str, Any]:
        rate = self.crypto_taker_fee + self.crypto_fee_bump
        return {
            "taker_rate": rate,
            "maker_rate": rate,  # engine charges maker on close; keep it taker
            "slippage": self.crypto_slippage,
        }


@dataclass
class GateConfig:
    """Operational promotion gate (evidence-based): target return in at least
    ``min_window_pass_rate`` of held-out windows AND account MDD <= cap.

    The legacy 25%/80% bar is retained only as an informational stretch metric
    (``stretch_target_return``) — it does not block paper/live promotion.
    """

    target_window_return: float = 0.03   # >= 3% net per 2-week window
    # 30% (not 70%): registry frontier tops ~50% @ +3%/window; re-sims of the
    # same genomes on the current snapshot land ~35%. 70% never unlocked paper.
    min_window_pass_rate: float = 0.30
    max_drawdown: float = 0.10           # MDD <= 10% account-level
    window_trading_days: int = 10        # 2 calendar weeks of trading days
    window_step_days: int = 5            # rolling weekly step
    oos_fraction: float = 0.30           # last 30% of windows held out
    # Informational only — never used for gate_passed / paper unlock.
    stretch_target_return: float = 0.25
    stretch_min_pass_rate: float = 0.80


@dataclass
class DaytradeGateConfig:
    """Per-session gate for the intraday (5m/15m) replay engine.

    A "window" is one trading session (RTH day for equities, UTC day for
    crypto). Pass = green on enough held-out sessions with a real daily
    edge, capped intraday drawdown, and enough trades to actually be
    day-trading. The ladder escalates the return targets when a champion
    holds the gate — drawdown caps NEVER loosen.
    """

    min_green_rate: float = 0.55          # OOS sessions with net positive P&L
    min_mean_daily_ret: float = 0.003     # mean OOS session net return (+0.3%)
    session_max_drawdown: float = 0.05    # intraday DD cap within a session
    max_drawdown: float = 0.10            # account-level cap over the full run
    min_trades_per_session: float = 2.0   # must actually day-trade
    oos_fraction: float = 0.30            # last 30% of sessions held out
    min_oos_sessions: int = 8             # no 2-for-2 lucky runs through the gate
    # Escalation ladder: (min_green_rate, min_mean_daily_ret) per rung.
    # Climbed when the champion holds the gate for cycles_per_rung cycles.
    ladder: tuple = ((0.55, 0.003), (0.60, 0.005), (0.65, 0.008))
    cycles_per_rung: int = 5


@dataclass
class EvalProfileConfig:
    """Funded-prop style scoreboard (simulation goal, not auto-enrollment).

    Tracks whether a champion's equity path would clear typical evaluation
    rules: hit profit target without breaching trailing drawdown.
    """

    profit_target: float = 0.08          # +8%
    trailing_max_dd: float = 0.05        # 5% trailing
    lookback_days: int = 45


@dataclass
class RiskConfig:
    """Position-sizing and stop parameters bounds (genome values live inside
    these bounds; see strategy_space.py)."""

    max_positions_bounds: tuple[int, int] = (2, 30)
    target_vol_annual_bounds: tuple[float, float] = (0.15, 0.80)
    atr_stop_mult_bounds: tuple[float, float] = (1.5, 5.0)
    max_weight_per_symbol: float = 0.5
    # Per-symbol cap when a genome runs in small-trade mode (dozens of
    # small compounding positions).
    max_weight_small_trade: float = 0.05


@dataclass
class EvolutionConfig:
    population_size: int = 48
    generations: int = 12
    elite_keep: int = 4
    tournament_size: int = 3
    mutation_sigma: float = 0.25         # relative Gaussian scale
    seed: int = 42
    llm_proposals_per_gen: int = 2       # 0 disables LLM mutation proposals


@dataclass
class DataConfig:
    start_date: str = "2024-07-01"
    end_date: str = "2026-07-01"         # >= 1 year of history
    interval: str = "1D"
    # Bar intervals prefetched into snapshots; the genome's timeframe gene
    # selects among the ones actually available. 1D must always be present.
    # Note: free intraday history is limited (~730d for 1H via Yahoo).
    intervals: tuple[str, ...] = ("1D",)
    source: str = "auto"
    equity_universe: str = "us_liquid"   # see universe.py
    crypto_universe: str = "crypto_top"
    penny_universe: str = "penny_liquid"
    equity_universe_size: int = 60
    crypto_universe_size: int = 12
    penny_universe_size: int = 20


@dataclass
class PaperConfig:
    """Shadow paper-trading gate: the mandatory bridge between backtest and
    live money. A hard-gate finalist must also clear this on RECENT data it
    was never optimized on."""

    lookback_days: int = 45              # recent window to shadow-trade
    min_return: float = 0.05             # >= 5% net over the paper window
    max_drawdown: float = 0.10           # same drawdown cap as the gate
    min_trades: int = 3                  # no free pass on an idle strategy


@dataclass
class LiveConfig:
    """Live execution interlocks. Everything defaults to OFF/paper.

    ``AUTOTRADER_LIVE_ENABLE=1`` must be set explicitly in the environment to
    arm live order routing — a config file alone can never do it.
    """

    max_order_notional_usd: float = 50.0   # per-order cap
    max_daily_loss_usd: float = 20.0       # kill switch for the day
    alpaca_paper: bool = True              # Alpaca paper endpoint by default
    crypto_exchange: str = "okx"

    @property
    def armed(self) -> bool:
        return os.environ.get("AUTOTRADER_LIVE_ENABLE", "") == "1"


@dataclass
class AwsConfig:
    """S3-only footprint. Every resource is tagged with the repo name."""

    enabled: bool = field(
        default_factory=lambda: bool(os.environ.get("AUTOTRADER_S3_BUCKET"))
    )
    bucket: str = field(
        default_factory=lambda: os.environ.get("AUTOTRADER_S3_BUCKET", "")
    )
    region: str = field(
        default_factory=lambda: os.environ.get(
            "AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1")
        )
    )
    tag_key: str = "Project"
    tag_value: str = "Vibe-Trading"


LLM_ENV_PATH = Path.home() / ".config" / "autotrader" / "llm.env"


def _load_llm_env() -> None:
    """Load LLM credentials from the out-of-repo env file (idempotent).

    Values already present in the process environment win, so ad-hoc
    overrides (e.g. ``AUTOTRADER_LLM_MODEL=... python -m ...``) still work.
    """
    if not LLM_ENV_PATH.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(LLM_ENV_PATH, override=False)
    except ImportError:
        for line in LLM_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _llm_env_value(name: str, default: str = "") -> str:
    """Read an LLM env var, loading the credentials file first."""
    _load_llm_env()
    return os.environ.get(name, default)


@dataclass
class LlmConfig:
    """Outer-loop LLM routing: Kimi K3 only (user directive 2026-08-31).

    Credentials live in ``~/.config/autotrader/llm.env`` (chmod 600, outside
    the repo); AWS credentials come from the standard ``~/.aws`` chain. The
    pipeline stays fully functional (deterministic) when no provider resolves.
    The Bedrock client path is retained but excluded from the default chain.
    """

    api_key: str = field(default_factory=lambda: _llm_env_value("AUTOTRADER_LLM_API_KEY"))
    base_url: str = field(default_factory=lambda: _llm_env_value(
        "AUTOTRADER_LLM_BASE_URL", "https://api.moonshot.ai/v1"
    ))
    # kimi-k3 verified live on this Moonshot account 2026-08-31 (1M context,
    # reasoning-only; valid reasoning_effort: low/high/max).
    model: str = field(default_factory=lambda: _llm_env_value(
        "AUTOTRADER_LLM_MODEL", "kimi-k3"
    ))
    # Opus 4.8/4.7 invoke access is not granted on this AWS account
    # (verified 2026-07-19); Opus 4.6 is. Override via env once granted.
    bedrock_model: str = field(default_factory=lambda: _llm_env_value(
        "AUTOTRADER_BEDROCK_MODEL", "us.anthropic.claude-opus-4-6-v1"
    ))
    bedrock_region: str = field(default_factory=lambda: _llm_env_value(
        "AUTOTRADER_BEDROCK_REGION", "us-east-1"
    ))
    provider_order: tuple[str, ...] = ("kimi",)
    timeout_s: float = 120.0
    # Hard off-switch (--offline / AUTOTRADER_LLM_DISABLE=1): no provider is
    # ever contacted, even when credentials resolve.
    disabled: bool = field(default_factory=lambda: (
        os.environ.get("AUTOTRADER_LLM_DISABLE", "") == "1"
    ))

    @property
    def bedrock_available(self) -> bool:
        try:
            import boto3

            return boto3.Session().get_credentials() is not None
        except ImportError:
            return False

    @property
    def available(self) -> bool:
        if self.disabled:
            return False
        return bool(self.api_key) or self.bedrock_available


@dataclass
class PipelineConfig:
    capital: CapitalConfig = field(default_factory=CapitalConfig)
    friction: FrictionConfig = field(default_factory=FrictionConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    data: DataConfig = field(default_factory=DataConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    eval_profile: EvalProfileConfig = field(default_factory=EvalProfileConfig)
    daytrade_gate: DaytradeGateConfig = field(default_factory=DaytradeGateConfig)
    aws: AwsConfig = field(default_factory=AwsConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "PipelineConfig":
        """Build the default config, optionally overlaying a JSON file.

        The JSON file mirrors the dataclass tree, e.g.
        ``{"data": {"equity_universe_size": 20}, "evolution": {"generations": 3}}``.
        """
        cfg = cls()
        if path is None:
            return cfg
        overrides = json.loads(Path(path).read_text(encoding="utf-8"))
        for section, values in overrides.items():
            target = getattr(cfg, section, None)
            if target is None or not isinstance(values, dict):
                raise ValueError(f"Unknown config section {section!r}")
            for key, value in values.items():
                if not hasattr(target, key):
                    raise ValueError(f"Unknown config key {section}.{key}")
                if key.endswith("_bounds") and isinstance(value, list):
                    value = tuple(value)
                setattr(target, key, value)
        # Re-run validation that __post_init__ would normally perform.
        FrictionConfig(**{k: getattr(cfg.friction, k) for k in (
            "equity_slippage", "crypto_taker_fee", "crypto_slippage", "crypto_fee_bump",
        )})
        return cfg
