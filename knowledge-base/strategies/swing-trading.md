# Swing Trading

**Capture the 2–10 day "swing" between short-term extremes inside a larger
trend.** Sits between day trading (too much noise, too much cost) and
position trading (too slow for small accounts). The natural home for a small
account compounding many modest trades.

## The structural template
Markets move in impulse → pullback → impulse sequences. Swing trading buys
the end of pullbacks in uptrends and sells the end of rallies in downtrends.

1. **Higher-timeframe filter:** daily/weekly trend must be up (price above
   rising 50-day MA; higher highs and higher lows intact).
2. **Setup:** a 2–5 bar pullback, falling volume on the pullback, holding a
   logical support (prior swing, 20-EMA, gap fill).
3. **Trigger:** first bar reclaiming strength — break of prior bar's high,
   bullish reversal bar, or momentum turn.
4. **Stop:** below the pullback low (structure invalidation), typically
   0.5–1.5 ATR of risk.
5. **Targets:** prior swing high (bank partial), then trail — or fixed 2–3R.

## Classic named setups
- **Holy Grail (Raschke):** ADX > 30 uptrend, first touch of the 20-EMA, buy
  the resumption.
- **First pullback after breakout:** the highest-quality swing entry —
  breakout proves demand, pullback provides price.
- **3-bar / ABC pullbacks:** simple counted retracements in trend.
- **Gap-and-go continuation:** strong gap that holds its open price and
  extends over the following days (earnings gaps drift — see `event-driven.md`).

## Position management math
For a small account, expectancy compounds through frequency:
- 45–55% win rate at 2R average win vs 1R loss → expectancy ≈ +0.4R/trade.
- 3–5 concurrent positions, 2–4 trades/week each side → dozens of trades a
  month, each risking ~1% → smooth-ish equity curve driven by law of large
  numbers rather than any single call.

## What separates winners from losers here
- Trading **with** the higher timeframe (most losing swing traders fade trends).
- Taking the stop mechanically — the structure stop *is* the strategy.
- Not overstaying: swings are 2–10 days; when the swing target hits, the
  trade is done. Turning swings into "investments" is the classic account killer.
- Selectivity: A-grade setups only. The edge per trade is modest; slippage in
  discipline erases it.

## Implementation checklist
- Trend filter (dual timeframe) → pullback detector → strength trigger.
- Structure stop, partial at 1R/prior swing, trail remainder.
- Time stop: exit at 8–10 bars if the swing hasn't materialized.
- Cap concurrent correlated positions (all tech longs = one trade).
