# Mean Reversion (Short-Term)

**Buy panic, sell relief.** Over horizons of 1–10 days, equities (especially
indices and large caps) exhibit strong reversal: sharp short-term losers
bounce. The statistical mirror image of momentum, living at a shorter horizon.

## The core empirical facts
- Short-term reversal (1-week/1-month losers outperforming) is one of the
  oldest documented anomalies (Jegadeesh 1990, Lehmann 1990).
- Since ~1990, S&P 500 behavior flipped from daily follow-through to daily
  mean reversion — buying N-day pullbacks in a long-term uptrend has been one
  of the most persistent index edges (the entire Connors/RSI-2 literature).

## Canonical setups

### RSI(2) (Connors)
- Condition: price above the 200-day MA (long-term uptrend intact).
- Entry: 2-period RSI < 10 (< 5 more selective) — a severe short-term oversold.
- Exit: close above 5-day MA, or RSI(2) > 65–70. Typically 3–7 day holds.
- Historical stats on SPX/liquid ETFs: 70–80% win rate, small average win,
  occasional larger loss — the *inverse* of trend's P&L shape.

### Bollinger fade
- Entry: close below lower band (2σ, 20-day) while above the 200-day MA.
- Exit: touch of the middle band (20-day MA).

### N-day low / pullback systems
- "Buy a 5–10 day low in an uptrend, sell the first strength" — dozens of
  variants; all monetize the same reversion effect.

## Why it works
- Liquidity provision: reversion traders get paid for absorbing panicked,
  price-insensitive selling (and euphoria buying).
- Overreaction to news + margin/stop cascades push price past fair value.

## Failure modes (these kill accounts)
1. **Catching the falling knife in a regime break.** Reversion buys every dip
   — including the first dip of a crash. The 200-day MA condition and a
   portfolio-level breaker are what keep 2008-style events survivable.
2. **Negative skew:** many small wins, rare large losses. Sizing must assume
   the largest loss is 3–5× the backtest's worst.
3. **No natural stop:** "more oversold" is a better entry, so stops fight
   the logic. Discipline substitutes: time stops (exit after N bars
   regardless) and portfolio heat caps.
4. Works poorly on individual small caps (bankruptcy risk = dips that never
   revert); best on indices, ETFs, and large liquid names.

## Implementation checklist
- Always pair with a long-term trend condition (above 200-day = fade dips;
  below = do not catch knives).
- Time stop at 5–10 bars; exit on first strength, don't overstay.
- Expect win rates 60–75% with payoff ratio < 1 — judge by expectancy.
- Position caps small: the tail risk arrives correlated across all positions.
