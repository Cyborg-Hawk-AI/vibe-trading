# Breakout Trading

**Enter when price escapes a defined range, betting the escape begins a
larger move.** The entry style of Livermore (pivotal points), the Turtles
(channel breakouts), O'Neil (bases), and Minervini (VCP pivots).

## Structural logic
A range = equilibrium where supply and demand are balanced. A genuine
breakout means one side is exhausted; the market must travel to find new
equilibrium. Breakouts also harvest mechanical flows: stops above the range
trigger, breakout systems pile in, shorts cover.

## Canonical variants

### Channel / Donchian breakout
- Buy the N-day high (20/55 classic). Pure, no chart-reading required.
- The workhorse of CTA trend systems — see `trend-following.md`.

### Volatility breakout (Larry Williams)
- Buy at `today's open + k × yesterday's range` (k ≈ 0.5–1.0).
- Intraday timeframe version: a thrust beyond normal volatility = real flow.

### Base breakout (O'Neil / Minervini)
- Weeks-long consolidation (cup-with-handle, flat base, VCP) near 52-week
  highs, volume drying up in the base.
- Buy the pivot break on volume ≥ 40–50% above average.
- Tightest risk: stop just below the pivot; a valid breakout shouldn't return.

### Opening range breakout (ORB)
- Range = first 5–30 minutes of the session; trade the break of it.
- Recent academic work (Zarattini & Aziz 2023) found 5-min ORB on high
  relative-volume stocks-in-play still tested strongly. See `scalping-day-trading.md`.

## The central problem: false breakouts
Most breakouts fail — the range holds and price snaps back (this is exactly
what Raschke's Turtle Soup monetizes). Quality filters that raise the odds:
1. **Volume expansion** on the break (institutional participation).
2. **Range contraction before the break** (VCP logic: coiled spring, supply
   exhausted). Tight quiet bases break better than wide loud ones.
3. **Trend alignment:** breakouts in the direction of the higher-timeframe
   trend and the general market succeed far more often.
4. **Proximity to 52-week highs** — breakouts into "no supply overhead"
   (no trapped holders waiting to sell at breakeven) travel further.
5. **Catalyst present** (earnings, news) — see `event-driven.md`.

## P&L shape
Win rate 35–50% even with good filters; payoff comes from the winners that
trend for weeks. Requires the trend-following exit discipline (trail, don't
target) and emotional tolerance for many small stop-outs.

## Implementation checklist
- Define the range mechanically (N-day high, X-week base with < Y% depth).
- Demand volume and trend confirmation; skip breakouts against the market.
- Initial stop below the breakout level (failed break = immediate exit).
- Trail the remainder; the entire edge lives in the tail winners.
