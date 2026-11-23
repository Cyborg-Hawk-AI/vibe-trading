# Applied: Mapping the Knowledge Base to the Autotrader

The bridge document. Every idea in this library, tied to the concrete genes,
gates, and diagnostics in `autotrader/`.

## 1. The current genome already encodes the classics

| Gene (`strategy_space.py`) | Knowledge-base lineage |
|---|---|
| `momentum_lookback` | Cross-sectional momentum (Jegadeesh-Titman; O'Neil's RS rank) — `strategies/momentum.md` |
| `use_trend_gate` + `trend_ma_period` | PTJ's 200-day rule; Minervini trend template; time-series momentum — `legends/paul-tudor-jones.md` |
| `use_breakout_gate` + `breakout_lookback` | Donchian/Turtle channel breakout; Livermore pivotal points — `strategies/breakout.md`, `legends/richard-dennis-turtles.md` |
| `use_rsi_gate` + `rsi_entry_low/high` | Connors-style short-term reversion (buy-the-dip band) or momentum-confirmation band — `strategies/mean-reversion.md` |
| `atr_stop_mult` | Turtle 2N stop; ATR/volatility stops — `risk/stops-and-exits.md` |
| `min/max_holding_bars` | Time stops (Raschke: "best trades work right away") and swing-horizon capping — `strategies/swing-trading.md` |
| `target_vol_annual` + `vol_lookback` | Volatility targeting — the CTA/risk-parity standard — `risk/position-sizing.md` |
| `max_weight` + `max_positions` | Portfolio heat rules; many-small-positions geometry — `risk/position-sizing.md` |
| `small_trade_mode` | The Virtu/Medallion thesis: frequency × small edge × law of large numbers — `legends/jim-simons.md`, `strategies/market-making.md` |
| `dd_breaker_pct` + `dd_cooldown_bars` | Layered drawdown control; daily breakers — `risk/drawdown-control.md` |
| `timeframe` | Horizon selection: each strategy family has a natural horizon (see README table) |

**Implication:** the evolutionary search is exploring combinations of
*proven structure*, which is the right prior. The knowledge base's job is to
suggest which combinations the legends found to work together (below).

## 2. Combinations the literature says work (search priors)

1. **Trend gate ON + breakout entry + wide ATR stop + long max holding** =
   the Turtle/CTA archetype. Expect low win rate, tail-driven P&L. Judge it
   on expectancy across many windows, never on pass-rate of one window.
2. **Trend gate ON + RSI oversold entry + short holding + time stop** =
   Connors reversion archetype. High win rate, negative skew — needs the
   dd-breaker genes active because its tail loss arrives correlated.
3. **The anti-pattern:** breakout entry + tight stop + short max holding
   (trend entry with reversion exits) — cuts every winner before the trend
   pays. If diagnostics show `late_exit` low but `premature_stop` high on
   breakout genomes, this mismatch is why.
4. **Small-trade mode + reversion** pairs better than small-trade + trend:
   reversion's edge-per-trade is small but frequent (law of large numbers
   friendly); trend needs concentration in the few monsters.
5. **Regime-gated sleeves** (Dalio/O'Neil "M"): equities momentum in
   index-uptrend regimes, reversion in chop, cash/short crypto below the
   BTC 200-day. A regime-classifier gene would be the highest-leverage
   addition to the genome.

## 3. Diagnostic failure modes ↔ the literature

| `diagnostics.py` mode | Named in the literature | Canonical fix |
|---|---|---|
| `premature_stop` | Stops inside the noise band | Wider ATR mult + smaller size (same $ risk) — `risk/stops-and-exits.md` |
| `fakeout_entry` | False breakout (Raschke's Turtle Soup exists *because* of these) | Volume confirmation, range-contraction precondition, trend alignment — `strategies/breakout.md` |
| `whipsaw_churn` | Trend system in chop | Longer lookbacks, trend-strength (ADX-like) filter, fewer signals — `strategies/trend-following.md` |
| `overbought_entry` | Chasing extended moves | Buy first pullback, not the vertical (Raschke Holy Grail) — `strategies/swing-trading.md` |
| `illiquid` | The penny-stock cost wall | Friction floor per instrument class; skip names where spread > edge — `risk/execution-microstructure.md` |
| `late_exit` | Round-tripping winners (disposition-effect inverse) | Trailing structure exits; partial at 1–2R — `risk/stops-and-exits.md` |
| `oversized_loss` | Sizing failure, the blowup class | Vol-normalized sizing, heat caps, gap-aware sizing — `risk/position-sizing.md` |

## 4. Honest calibration against the project's gate
The hard gate is ≥25% per rolling 2 weeks. Reference points from this library:
- Medallion, the best ever: ~66% gross **per year**.
- Druckenmiller/Soros: ~30%/yr. Thorp: ~19%/yr with no losing quarter.
- Contest records (Williams' +11,376%/yr, Minervini +155%) were achieved with
  near-ruin sizing or single-year peaks — not sustained processes.

A strategy passing the 25%/2-week gate on OOS data is therefore either (a) a
small-capital microstructure edge that won't scale but doesn't need to at
$100–$2K, or (b) overfit. The validation ladder in
`process/backtesting-pitfalls.md` (especially 2× cost stress and paper
shadowing, which the pipeline already implements) is what distinguishes them.
The realistic expectation the library supports: durable 1–5% per 2-week
window at small size, with the funding ladder compounding that — still a
life-changing outcome if sustained, without requiring the gate's outlier.

## 5. Candidate genome extensions from this library (ranked by evidence)
1. **Market-regime gate** (index/BTC above 200-day; O'Neil's "M", PTJ's rule).
   The single best-documented conditioner of every equity strategy.
2. **Volume-confirmation gene for breakouts** (O'Neil: +40–50% volume on the
   pivot) — directly attacks `fakeout_entry`.
3. **Range-contraction precondition** (Minervini VCP / Raschke contraction →
   expansion) before breakout entries.
4. **Skip-month momentum** (rank on 12-1 style lookback minus most recent
   bars) — the academically correct momentum formulation.
5. **First-pullback entry mode** (enter on pullback-to-MA after signal, not
   on the signal bar) — attacks `overbought_entry`.
6. **Equity-curve exposure scaling** (Druckenmiller/PTJ: shrink after
   losses) at the daemon level, on live/paper equity.
7. **PEAD/event sleeve** (earnings-surprise drift) — needs point-in-time
   earnings timestamps; highest data-integrity bar, real documented edge.
