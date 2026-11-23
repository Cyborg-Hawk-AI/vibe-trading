# Trend Following (Managed Futures / CTA style)

**Ride persistent directional moves; lose small and often, win big and
rarely.** The oldest systematic strategy family (Donchian 1950s → Turtles →
modern CTAs like AHL, Winton, Aspect managing tens of billions).

## Canonical mechanics
1. **Signal:** price vs. its own history —
   - Channel breakout: N-day high/low (Donchian/Turtle, N = 20–100)
   - MA crossover: fast MA crosses slow (e.g. 50/200)
   - MA slope or price-above-MA (PTJ's 200-day rule)
2. **Sizing:** inverse volatility (ATR or stdev) so every market contributes
   equal risk. THE defining feature of professional implementations.
3. **Exit:** opposite channel breakout, trailing ATR stop, or MA cross-back.
   Never profit targets — the whole P&L comes from outlier trends.
4. **Diversification:** 30–100+ markets across equities, bonds, FX,
   commodities, crypto. Any single market's trend edge is weak; the ensemble
   is strong.

## The P&L shape (know this cold)
- Win rate: **30–45%**. Most trades are small losses ("paying premium").
- Payoff ratio: average win 2–4× average loss; a few trades per year deliver
  the entire annual return.
- Long flat/choppy periods (years) punctuated by crisis windfalls — trend
  made fortunes in 1987, 2000–02, 2008 (+20–50% while equities halved), 2022.
- This "crisis alpha" (long convexity behavior) is why institutions hold it.

## Evidence
- *A Century of Evidence on Trend-Following* (AQR/Hurst-Ooi-Pedersen):
  Sharpe ~0.75–1.0 gross across 137 years, 67 markets, never a losing decade.
- Time-series momentum profits documented in every asset class tested.

## Failure modes
- **Chop/whipsaw:** range-bound markets generate repeated false breakouts.
  Mitigation: longer lookbacks, trend-strength filters (ADX), fewer signals.
- **Sharp reversals:** trend is always positioned with the crowd at the turn
  (e.g. V-bottoms). Vol-targeting and fast de-risking limit damage.
- **Single-market trend trading is nearly worthless** — the strategy is a
  portfolio effect. Running it on 3 symbols is noise.

## Parameter robustness (why quants love it)
Trend profits are remarkably insensitive to exact parameters: 20 vs 50 vs
100-day breakouts all work over decades. If your backtest only works at
N=23, it's overfit, full stop.

## Implementation checklist
- Wide, diversified universe (the more uncorrelated markets, the better).
- ATR-based sizing, ~0.5–1% account risk per position.
- Let winners run for weeks–months; do not cap holding period tightly.
- Expect 40%-ish win rates; judge the system on expectancy, not hit rate.
