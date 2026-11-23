# Execution & Market Microstructure

**The gap between backtest and live P&L is mostly execution.** RenTec treated
cost modeling as seriously as signal discovery; a weak edge with great
execution beats a strong edge with naive execution.

## The cost stack (every round trip pays all of these)
1. **Spread:** you cross it entering and exiting with market orders. Liquid
   large caps: 1–3 bps. Small caps: 10–50 bps. Penny stocks: often 1–5%.
   Crypto majors: 1–5 bps; alts: 10–100 bps.
2. **Commissions/fees:** equities near zero (but PFOF affects fill quality);
   crypto taker 5–10 bps/side (maker often rebated or ~0–2 bps).
3. **Slippage:** fill price vs decision price. Grows with order size relative
   to displayed liquidity, urgency, and volatility.
4. **Market impact:** your own order moves the price (matters above ~1% of
   ADV; the dominant cost at institutional scale).
5. **Timing cost:** limit orders that never fill = missed winners. The
   passive/aggressive tradeoff has no free option.

### The math that kills strategies
Strategy expecting 30 bps/trade gross, paying 12 bps round-trip friction:
60% of the edge survives. Same strategy on a penny stock with a 2% spread:
dead on arrival. **Every strategy has a minimum viable gross edge set by its
instrument's friction — check it before backtesting anything.**

## Order types in practice
- **Market:** guaranteed fill, unguaranteed price. Fine for liquid names at
  small size; poison in thin books.
- **Limit:** guaranteed price, unguaranteed fill — and adversely selected
  (fills happen more when price is moving through you; see `strategies/market-making.md`).
- **Marketable limit** (limit at/just through the touch): the sane default —
  caps the damage while behaving like a market order normally.
- **Stop orders:** become market orders on trigger — in a gap or cascade
  they fill at the far end. Stop-limit protects price but can fail to fill.

## Adverse selection in one paragraph
When your resting buy order fills, it's because someone was willing to sell
at your price *right then* — disproportionately when new information says
you're wrong. Passive fills are biased toward the bad ones. This is why
"I'll just use limit orders and save the spread" quietly underperforms its
backtest: the backtest assumed every limit fills; live, only the adverse ones do.

## Liquidity realities by venue
- **US equities:** deepest at open/close auctions; mid-day thin; spreads
  widen at open. First/last 30 min carry the day's volume.
- **Penny/OTC stocks:** quoted size is tiny, spreads huge, dark pools absent;
  a small "position" can take days to exit without cratering the price.
  This is the structural reason penny-stock backtests overstate reality.
- **Crypto:** depth concentrated on 2–3 venues per pair; weekend/off-hours
  books thin dramatically; wicks hunt clustered liquidation levels.

## Backtest execution assumptions (minimum honest set)
- Fill at next bar's open after signal (never the signal bar's close).
- Subtract spread/2 + slippage per side, scaled by the instrument class.
- Cap simulated participation (e.g., ≤ 1–5% of bar volume).
- Re-run every promising strategy with 2× assumed friction — if it dies,
  it was never alive.
