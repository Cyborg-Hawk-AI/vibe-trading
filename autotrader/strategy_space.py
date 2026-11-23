"""Strategy genome: a bounded parameter vector rendered into SignalEngine code.

A genome is a flat dict of named parameters (entry rule mix, indicator
thresholds, risk knobs). ``render_signal_engine`` turns it into a
self-contained ``signal_engine.py`` that satisfies the vibe-trading runner
contract (class ``SignalEngine``, no-arg ``__init__``,
``generate(data_map) -> Dict[str, pd.Series]``) and passes its AST sandbox
(imports + defs only, no I/O, no network).

Signal semantics (all causal / backward-looking):
  - Cross-sectional momentum ranking picks the top ``max_positions`` symbols.
  - Entry gates: RSI band, VWAP-offset trend filter, breakout confirmation —
    each active only if the genome enables it.
  - Exits: ATR trailing stop, RSI overbought exit, max holding period.
  - Sizing: inverse-volatility weights scaled to a target portfolio vol
    (fractional-Kelly-style cap), clipped per symbol.
  - Circuit breaker: strategy tracks its own causal proxy equity curve and
    flattens signals after a drawdown breach until a cooldown elapses.

The engine shifts signals one bar (next-bar-open execution), so nothing the
strategy computes at bar t can affect the fill at bar t.
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any

from autotrader.config import PipelineConfig

# Bar intervals the timeframe gene can select from. The optimizer only
# offers intervals that actually have a prefetched snapshot; index 0 (1D)
# is always available. Indices 3-4 are the day-trade horizons: the daytrade
# daemon narrows the timeframe gene to [3, 4] via apply_gene_bounds.
TIMEFRAMES: tuple[str, ...] = ("1D", "4H", "1H", "15m", "5m")

# Bars per YEAR per interval, split by market clock (equities RTH ~6.5h x 252d,
# crypto 24/7 x 365d). Used to annualize realized vol for position sizing —
# the template's default 252 is only correct for 1D equities.
BARS_PER_YEAR: dict[str, dict[str, float]] = {
    "1D": {"equity": 252.0, "crypto": 365.0},
    "4H": {"equity": 504.0, "crypto": 2_190.0},
    "1H": {"equity": 1_764.0, "crypto": 8_760.0},
    "15m": {"equity": 6_552.0, "crypto": 35_040.0},
    "5m": {"equity": 19_656.0, "crypto": 105_120.0},
}


def bars_per_year_for(interval: str, sleeve: str) -> float:
    kind = "crypto" if sleeve == "crypto" else "equity"
    return BARS_PER_YEAR.get(interval, {"equity": 252.0, "crypto": 365.0})[kind]

# name -> (low, high, kind)   kind: "f" float, "i" int, "b" bool-ish (0/1)
GENE_SPACE: dict[str, tuple[float, float, str]] = {
    # Cross-sectional selection
    "momentum_lookback":   (10, 120, "i"),
    "mom_skip_bars":       (0, 10, "i"),   # skip most-recent bars (12-1 style)
    "max_positions":       (2, 30, "i"),
    # Entry gates (enable flags + thresholds)
    "use_rsi_gate":        (0, 1, "b"),
    "rsi_period":          (7, 21, "i"),
    "rsi_entry_low":       (20, 45, "f"),
    "rsi_entry_high":      (50, 75, "f"),
    "use_trend_gate":      (0, 1, "b"),
    "vwap_offset_pct":     (-0.03, 0.03, "f"),
    "trend_ma_period":     (20, 100, "i"),
    "use_breakout_gate":   (0, 1, "b"),
    "breakout_lookback":   (10, 60, "i"),
    # Regime / quality filters (Phase-2 DNA expansion)
    "use_regime_gate":     (0, 1, "b"),
    "regime_ma_period":    (50, 200, "i"),
    "use_volume_gate":     (0, 1, "b"),
    "volume_mult":         (1.2, 3.0, "f"),
    "use_pullback_entry":  (0, 1, "b"),
    "use_range_compress":  (0, 1, "b"),
    "compress_lookback":   (10, 40, "i"),
    # Exits
    "atr_period":          (7, 28, "i"),
    "atr_stop_mult":       (1.5, 5.0, "f"),
    "rsi_exit":            (65, 90, "f"),
    "min_holding_bars":    (0, 10, "i"),
    "max_holding_bars":    (3, 60, "i"),
    # Sizing / risk
    "target_vol_annual":   (0.15, 0.80, "f"),
    "vol_lookback":        (10, 60, "i"),
    "max_weight":          (0.02, 0.50, "f"),
    # Small-trade mode: structurally favor DOZENS of small compounding
    # positions (forces max_positions >= 10 and max_weight <= the small-trade
    # cap in _repair) instead of a handful of concentrated ones.
    "small_trade_mode":    (0, 1, "b"),
    # Bar interval: index into TIMEFRAMES, clamped to snapshots that exist.
    "timeframe":           (0, len(TIMEFRAMES) - 1, "i"),
    # Circuit breaker
    "dd_breaker_pct":      (0.05, 0.10, "f"),
    "dd_cooldown_bars":    (3, 15, "i"),
}

# Per-symbol weight cap enforced when small_trade_mode is on.
SMALL_TRADE_MAX_WEIGHT = 0.05
SMALL_TRADE_MIN_POSITIONS = 10

# HARD_GENE_SPACE must mirror GENE_SPACE after every DNA expansion.
HARD_GENE_SPACE: dict[str, tuple[float, float, str]] = dict(GENE_SPACE)


def apply_gene_bounds(bounds: dict[str, list[float]]) -> None:
    """Narrow the searchable gene bounds (validated LLM amendment).

    Bounds are always clamped inside HARD_GENE_SPACE, so an amendment can
    focus the search but never widen it past the shipped safety limits.
    """
    for gene, rng in bounds.items():
        if gene not in HARD_GENE_SPACE:
            continue
        hard_low, hard_high, kind = HARD_GENE_SPACE[gene]
        low = max(float(rng[0]), hard_low)
        high = min(float(rng[1]), hard_high)
        if low < high:
            GENE_SPACE[gene] = (low, high, kind)


def reset_gene_bounds() -> None:
    GENE_SPACE.update(HARD_GENE_SPACE)

# Diagnostics -> which genes a failure mode should push, and in which
# direction (+1 widen/raise, -1 tighten/lower). Used by mutate_biased().
FAILURE_GENE_BIAS: dict[str, dict[str, int]] = {
    "premature_stop": {"atr_stop_mult": +1, "atr_period": +1},
    "fakeout_entry": {
        "use_breakout_gate": +1, "breakout_lookback": +1, "use_trend_gate": +1,
        "use_volume_gate": +1, "use_range_compress": +1,
    },
    "whipsaw_churn": {
        "momentum_lookback": +1, "max_holding_bars": +1, "use_rsi_gate": +1,
        "min_holding_bars": +1, "use_regime_gate": +1,
    },
    "overbought_entry": {
        "rsi_entry_high": -1, "use_rsi_gate": +1, "use_pullback_entry": +1,
    },
    "illiquid": {"max_positions": -1, "max_weight": -1, "small_trade_mode": -1},
    "late_exit": {"rsi_exit": -1, "max_holding_bars": -1},
    "oversized_loss": {"target_vol_annual": -1, "max_weight": -1, "dd_breaker_pct": -1,
                       "small_trade_mode": +1},
}


def _cast(name: str, value: float) -> Any:
    low, high, kind = GENE_SPACE[name]
    value = min(max(value, low), high)
    if kind == "i":
        return int(round(value))
    if kind == "b":
        return int(value >= 0.5)
    return round(float(value), 6)


def genome_id(genome: dict[str, Any]) -> str:
    payload = json.dumps(genome, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:12]


def random_genome(rng: random.Random) -> dict[str, Any]:
    g = {name: _cast(name, rng.uniform(low, high)) for name, (low, high, _k) in GENE_SPACE.items()}
    return _repair(g)


# Defaults for genes added after a genome was saved. Binary gates stay OFF so
# historical champions re-score identically; continuous knobs use a neutral value.
_LEGACY_GENE_DEFAULTS: dict[str, float] = {
    "mom_skip_bars": 0,
    "use_regime_gate": 0,
    "regime_ma_period": 50,
    "use_volume_gate": 0,
    "volume_mult": 1.5,
    "use_pullback_entry": 0,
    "use_range_compress": 0,
    "compress_lookback": 10,
}


def _repair(g: dict[str, Any]) -> dict[str, Any]:
    """Enforce cross-gene invariants (and backfill genes added in v2)."""
    for name, (low, high, _kind) in GENE_SPACE.items():
        if name not in g:
            fill = _LEGACY_GENE_DEFAULTS.get(name, (low + high) / 2.0)
            g[name] = _cast(name, fill)
    if g["rsi_entry_low"] >= g["rsi_entry_high"]:
        g["rsi_entry_low"], g["rsi_entry_high"] = (
            min(g["rsi_entry_low"], g["rsi_entry_high"] - 5),
            max(g["rsi_entry_high"], g["rsi_entry_low"] + 5),
        )
    g["rsi_exit"] = max(g["rsi_exit"], g["rsi_entry_high"] + 5)
    if g.get("min_holding_bars", 0) >= g["max_holding_bars"]:
        g["min_holding_bars"] = max(g["max_holding_bars"] - 1, 0)
    if int(g.get("small_trade_mode", 0)) >= 1:
        # Dozens-of-small-trades mode: many positions, tiny per-symbol risk.
        g["max_positions"] = max(g["max_positions"], SMALL_TRADE_MIN_POSITIONS)
        g["max_weight"] = min(g["max_weight"], SMALL_TRADE_MAX_WEIGHT)
    return {k: _cast(k, v) for k, v in g.items()}


def mutate(genome: dict[str, Any], rng: random.Random, sigma: float = 0.25) -> dict[str, Any]:
    """Gaussian mutation over a random subset of genes."""
    child = _repair(dict(genome))  # backfill legacy genomes before mutating
    n_mut = max(1, rng.randint(1, max(2, len(GENE_SPACE) // 4)))
    for name in rng.sample(sorted(GENE_SPACE), n_mut):
        low, high, kind = GENE_SPACE[name]
        span = high - low
        if kind == "b":
            child[name] = 1 - int(child[name]) if rng.random() < 0.5 else child[name]
        else:
            child[name] = _cast(name, float(child[name]) + rng.gauss(0.0, sigma * span))
    return _repair(child)


def mutate_biased(
    genome: dict[str, Any],
    failure_counts: dict[str, int],
    rng: random.Random,
    sigma: float = 0.25,
) -> dict[str, Any]:
    """Mutation nudged by the diagnostic failure taxonomy.

    The dominant failure modes push their associated genes in the direction
    that counteracts the failure (e.g. lots of premature stop-outs -> widen
    the ATR stop multiplier). A plain random mutation is layered on top so
    the search never collapses to pure hill-climbing on the taxonomy.
    """
    child = _repair(dict(genome))
    total = sum(failure_counts.values())
    if total > 0:
        for mode, count in sorted(failure_counts.items(), key=lambda kv: -kv[1])[:3]:
            weight = count / total
            for gene, direction in FAILURE_GENE_BIAS.get(mode, {}).items():
                low, high, kind = GENE_SPACE[gene]
                span = high - low
                if kind == "b":
                    child[gene] = 1 if direction > 0 else 0
                else:
                    nudge = direction * weight * sigma * span * rng.uniform(0.5, 1.5)
                    child[gene] = _cast(gene, float(child[gene]) + nudge)
    return _repair(mutate(child, rng, sigma=sigma * 0.5))


def mutate_gene_bias(
    genome: dict[str, Any],
    gene_bias: dict[str, int],
    rng: random.Random,
    sigma: float = 0.25,
) -> dict[str, Any]:
    """Success-reinforcement mutation from attribution's gene_bias map.

    Where the failure bias pushes AWAY from what lost, this pushes TOWARD
    what the attribution pass says the winners captured deliberately.
    """
    child = dict(genome)
    for gene, direction in gene_bias.items():
        if gene not in GENE_SPACE or direction not in (1, -1):
            continue
        low, high, kind = GENE_SPACE[gene]
        if kind == "b":
            child[gene] = 1 if direction > 0 else 0
        else:
            span = high - low
            nudge = direction * sigma * span * rng.uniform(0.3, 1.0)
            child[gene] = _cast(gene, float(child.get(gene, (low + high) / 2)) + nudge)
    return _repair(mutate(child, rng, sigma=sigma * 0.5))


def crossover(a: dict[str, Any], b: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    a, b = _repair(dict(a)), _repair(dict(b))
    child = {name: (a if rng.random() < 0.5 else b)[name] for name in GENE_SPACE}
    return _repair(child)


def clamp_to_risk_bounds(genome: dict[str, Any], cfg: PipelineConfig) -> dict[str, Any]:
    g = dict(genome)
    g["max_positions"] = min(max(g["max_positions"], cfg.risk.max_positions_bounds[0]),
                             cfg.risk.max_positions_bounds[1])
    g["target_vol_annual"] = min(max(g["target_vol_annual"], cfg.risk.target_vol_annual_bounds[0]),
                                 cfg.risk.target_vol_annual_bounds[1])
    g["atr_stop_mult"] = min(max(g["atr_stop_mult"], cfg.risk.atr_stop_mult_bounds[0]),
                             cfg.risk.atr_stop_mult_bounds[1])
    g["max_weight"] = min(g["max_weight"], cfg.risk.max_weight_per_symbol)
    if int(g.get("small_trade_mode", 0)) >= 1:
        g["max_weight"] = min(g["max_weight"], cfg.risk.max_weight_small_trade)
    g["dd_breaker_pct"] = min(g["dd_breaker_pct"], cfg.gate.max_drawdown)
    return _repair(g)


# ---------------------------------------------------------------------------
# Code template
# ---------------------------------------------------------------------------

_TEMPLATE = '''"""Auto-generated SignalEngine — genome {gid}.

Generated by autotrader.strategy_space. All computations are causal:
rolling windows look backward only, and the backtest engine shifts the
returned signal one bar (next-bar-open execution).
"""

import json
from typing import Dict

import numpy as np
import pandas as pd

# JSON string, not a dict literal: the runner's AST sandbox only allows
# literal-only top-level assignments, and negative numbers parse as UnaryOp
# (not Constant), which it rejects. A string constant is always safe.
PARAMS_JSON = """{params}"""


class SignalEngine:
    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        p = json.loads(PARAMS_JSON)
        closes = {{}}
        feats = {{}}
        for code, df in data_map.items():
            need = max(
                p["momentum_lookback"] + p.get("mom_skip_bars", 0),
                p["trend_ma_period"], p["vol_lookback"],
                p.get("regime_ma_period", 50), p.get("compress_lookback", 10),
            ) + 5
            if df is None or len(df) < need:
                continue
            close = df["close"].astype(float)
            high = df["high"].astype(float) if "high" in df.columns else close
            low = df["low"].astype(float) if "low" in df.columns else close
            volume = df["volume"].astype(float) if "volume" in df.columns else pd.Series(1.0, index=close.index)
            closes[code] = close

            # Momentum with optional skip of most-recent bars (12-1 style)
            skip = int(p.get("mom_skip_bars", 0))
            mom = close.shift(skip).pct_change(p["momentum_lookback"]) if skip > 0 else close.pct_change(p["momentum_lookback"])

            # RSI (Wilder)
            delta = close.diff()
            gain = delta.clip(lower=0.0).ewm(alpha=1.0 / p["rsi_period"], adjust=False).mean()
            loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / p["rsi_period"], adjust=False).mean()
            rs = gain / loss.replace(0.0, np.nan)
            rsi = (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)

            # Trend gate: price vs MA with a VWAP-style offset band
            ma = close.rolling(p["trend_ma_period"]).mean()
            trend_ok = close > ma * (1.0 + p["vwap_offset_pct"])

            # Breakout gate: close above prior N-bar high (shifted to stay causal)
            prior_high = high.rolling(p["breakout_lookback"]).max().shift(1)
            breakout_ok = close > prior_high

            # Volume confirmation: bar volume >= N x rolling median
            vol_med = volume.rolling(20, min_periods=5).median()
            volume_ok = volume >= (vol_med * float(p.get("volume_mult", 1.5)))

            # Range compression (VCP-ish): recent range < prior range
            clb = int(p.get("compress_lookback", 20))
            recent_rng = (high.rolling(max(clb // 2, 3)).max() - low.rolling(max(clb // 2, 3)).min())
            prior_rng = (high.rolling(clb).max() - low.rolling(clb).min()).shift(max(clb // 2, 3))
            compress_ok = recent_rng < prior_rng * 0.7

            # Pullback entry: close within 2% of the trend MA (first pullback zone)
            pullback_ok = (close <= ma * 1.02) & (close >= ma * 0.97) & trend_ok

            # ATR for trailing stop
            tr = pd.concat(
                [(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
                axis=1,
            ).max(axis=1)
            atr = tr.ewm(alpha=1.0 / p["atr_period"], adjust=False).mean()

            # Realized vol for sizing, annualized for the active bar interval
            # (bars_per_year is injected at render time; 252 = legacy 1D).
            vol = close.pct_change().rolling(p["vol_lookback"]).std() * np.sqrt(
                float(p.get("bars_per_year", 252.0))
            )

            feats[code] = {{
                "mom": mom, "rsi": rsi, "trend_ok": trend_ok,
                "breakout_ok": breakout_ok, "volume_ok": volume_ok,
                "compress_ok": compress_ok, "pullback_ok": pullback_ok,
                "atr": atr, "vol": vol, "close": close, "low": low, "ma": ma,
            }}

        if not feats:
            return {{}}

        codes = sorted(feats)
        dates = sorted(set().union(*[feats[c]["close"].index for c in codes]))
        idx = pd.DatetimeIndex(dates)

        mom_df = pd.DataFrame({{c: feats[c]["mom"].reindex(idx) for c in codes}})
        rank_df = mom_df.rank(axis=1, ascending=False)

        signals = {{}}
        # Portfolio-level causal proxy equity for the drawdown circuit breaker:
        # equal-weight mean of per-symbol returns, cumulated, evaluated on
        # information available at each bar.
        port_close = pd.DataFrame(
            {{c: feats[c]["close"].reindex(idx) for c in codes}}
        ).mean(axis=1)
        port_ret = port_close.pct_change().fillna(0.0)
        proxy_equity = (1.0 + port_ret).cumprod()
        rolling_peak = proxy_equity.cummax()
        drawdown = proxy_equity / rolling_peak - 1.0
        breach = drawdown < -p["dd_breaker_pct"]
        # Extend each breach with a cooldown window
        blocked = breach.rolling(p["dd_cooldown_bars"], min_periods=1).max().astype(bool)

        # Regime gate: equal-weight "market" above its MA (O'Neil/PTJ style)
        regime_ma = port_close.rolling(int(p.get("regime_ma_period", 100))).mean()
        regime_ok = (port_close > regime_ma).fillna(False).astype(bool)

        # Session EOD flat (day-trade mode, injected at render time): force
        # weight 0 on the last bars of each session so the next-bar-open
        # engine fills the exit BEFORE the close — no overnight equity risk,
        # no fake profit from overnight gaps.
        n_idx = len(idx)
        eod_flat_v = np.zeros(n_idx, dtype=bool)
        if int(p.get("session_eod_flat", 0)) and n_idx > 0:
            last_in_day = (
                pd.Series(np.arange(n_idx), index=idx)
                .groupby(idx.normalize())
                .transform("max")
                .to_numpy()
            )
            eod_flat_v = (last_in_day - np.arange(n_idx)) < 2

        for code in codes:
            f = feats[code]
            close = f["close"].reindex(idx)
            rsi = f["rsi"].reindex(idx)
            trend_ok = f["trend_ok"].reindex(idx).fillna(False).astype(bool)
            breakout_ok = f["breakout_ok"].reindex(idx).fillna(False).astype(bool)
            volume_ok = f["volume_ok"].reindex(idx).fillna(False).astype(bool)
            compress_ok = f["compress_ok"].reindex(idx).fillna(False).astype(bool)
            pullback_ok = f["pullback_ok"].reindex(idx).fillna(False).astype(bool)
            atr = f["atr"].reindex(idx)
            vol = f["vol"].reindex(idx)
            rank = rank_df[code]

            selected = rank <= p["max_positions"]
            entry_ok = selected.fillna(False)
            if p["use_rsi_gate"]:
                entry_ok &= (rsi >= p["rsi_entry_low"]) & (rsi <= p["rsi_entry_high"])
            if p["use_trend_gate"]:
                entry_ok &= trend_ok
            if p["use_breakout_gate"]:
                entry_ok &= breakout_ok
            if int(p.get("use_regime_gate", 0)):
                entry_ok &= regime_ok
            if int(p.get("use_volume_gate", 0)):
                entry_ok &= volume_ok
            if int(p.get("use_range_compress", 0)):
                entry_ok &= compress_ok
            if int(p.get("use_pullback_entry", 0)):
                entry_ok &= pullback_ok

            # Stateful pass: entries, ATR trailing stop, RSI exit, max holding.
            n = len(idx)
            weight = np.zeros(n)
            in_pos = False
            stop = 0.0
            bars_held = 0
            close_v = close.to_numpy()
            atr_v = atr.to_numpy()
            rsi_v = rsi.to_numpy()
            entry_v = entry_ok.to_numpy()
            blocked_v = blocked.to_numpy()
            vol_v = vol.to_numpy()

            for i in range(n):
                px = close_v[i]
                if np.isnan(px):
                    weight[i] = weight[i - 1] if i > 0 else 0.0
                    continue
                if eod_flat_v[i]:
                    # Day-trade discipline: flat into the close, no new entries.
                    in_pos = False
                    weight[i] = 0.0
                    continue
                if in_pos:
                    bars_held += 1
                    a = atr_v[i]
                    if not np.isnan(a):
                        stop = max(stop, px - p["atr_stop_mult"] * a)
                    # Stops and the drawdown circuit breaker always fire;
                    # discretionary exits wait out the minimum holding period.
                    exit_now = px <= stop or blocked_v[i]
                    if bars_held >= p.get("min_holding_bars", 0):
                        exit_now = exit_now or (
                            rsi_v[i] >= p["rsi_exit"]
                            or bars_held >= p["max_holding_bars"]
                        )
                    if exit_now:
                        in_pos = False
                        weight[i] = 0.0
                    else:
                        weight[i] = weight[i - 1]
                else:
                    if entry_v[i] and not blocked_v[i]:
                        a = atr_v[i]
                        v = vol_v[i]
                        if not np.isnan(a) and not np.isnan(v) and v > 1e-8:
                            in_pos = True
                            bars_held = 0
                            stop = px - p["atr_stop_mult"] * a
                            # Inverse-vol sizing toward per-position vol budget
                            per_pos_target = p["target_vol_annual"] / max(p["max_positions"], 1)
                            w = per_pos_target / v
                            weight[i] = float(min(max(w, 0.0), p["max_weight"]))
                        else:
                            weight[i] = 0.0
                    else:
                        weight[i] = 0.0

            signals[code] = pd.Series(weight, index=idx).reindex(
                f["close"].index
            ).fillna(0.0)

        return signals
'''


def render_signal_engine(
    genome: dict[str, Any], extra_params: dict[str, Any] | None = None
) -> str:
    """Render the genome into runnable SignalEngine source.

    ``extra_params`` are execution-context knobs (bars_per_year,
    session_eod_flat) injected by the simulator for the active interval.
    They are NOT part of the genome and never affect genome_id.
    """
    params = {**genome, **(extra_params or {})}
    params_literal = json.dumps(params, sort_keys=True, indent=4)
    # JSON booleans/null never appear (all values are int/float), so the
    # literal is valid Python.
    return _TEMPLATE.format(gid=genome_id(genome), params=params_literal)
