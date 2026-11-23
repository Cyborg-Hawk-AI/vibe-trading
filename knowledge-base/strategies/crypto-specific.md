# Crypto-Specific Strategies

**Same behavioral edges as equities, amplified — plus structural income
sources that don't exist elsewhere.** Crypto is retail-dominated, 24/7,
narrative-driven, and less efficient: momentum and trend effects test
*stronger* here than in modern equities. It's also where blowups are fastest.

## Structural (non-directional) edges

### Perpetual funding rate harvesting
Perps track spot via a funding payment between longs and shorts (typically
every 8h). In bull euphoria funding runs heavily positive → the **cash-and-
carry**: long spot + short perp = collect funding, market-neutral.
- Historically 10–40% annualized in bull phases; near zero or negative in bears.
- Risks: exchange counterparty risk (FTX made "market-neutral" into -100%),
  funding flips, liquidation of the short leg on wicks.

### Futures basis trade
Dated futures trade above spot in contango; buy spot, short the future,
capture convergence at expiry. The classic "riskless" 5–30% annualized in
manias — same counterparty caveats.

### Cross-exchange arbitrage / Kimchi premium
Price differences across venues/regions. Mostly professionalized away except
during volatility spikes and in long-tail pairs.

## Directional edges

### Time-series momentum / trend
The best-documented crypto alpha: BTC/ETH trend strategies (e.g., price vs
20–50 day MA, breakout systems) historically captured the huge cycles while
sidestepping the -80% winters. Crypto trends are stronger and faster than
equity trends — and the drawdowns without a trend filter are unsurvivable.

### Cross-sectional momentum in alts
Rank alts by trailing 1–4 week returns; winners persist short-term.
Caveats: universe survivorship is brutal (most alts eventually -95%+),
liquidity evaporates in downturns, and the whole cross-section correlates
to ~1 in crashes.

### Narrative/event flows
Exchange listings (Binance/Coinbase listing pops), token unlocks (scheduled
supply hitting market = predictable pressure), airdrops, halvings, ETF flows.
Crypto's version of event-driven trading — dates are public and mechanical.

## Market-structure facts that change strategy design
- **24/7:** no overnight gaps, but also no closing bell to stop cascades;
  weekend liquidity is thin and wicks are violent.
- **Liquidation cascades:** leveraged longs force-sold into declines create
  the signature crypto flash-wick — stop placement must live outside wick
  range or use no stops + small size.
- **Fees dominate small accounts:** taker fees 5–10 bps/side plus slippage;
  high-frequency strategies on small accounts die by fees. Maker orders and
  lower frequency preserve edge.
- **Regime dominance:** everything is beta to BTC in a crash. A BTC-level
  regime filter (e.g., BTC above/below 200-day) should gate all alt exposure.

## Implementation checklist
- Trend-filter everything against BTC regime.
- Prefer top-liquidity pairs; model realistic taker fees + slippage.
- Never hold "market-neutral" positions on a venue you wouldn't trust with
  the full balance (counterparty = the real risk).
- Respect unlock/listing calendars as event risk and event opportunity.
