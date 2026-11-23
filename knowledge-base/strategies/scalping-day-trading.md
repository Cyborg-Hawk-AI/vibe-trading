# Scalping & Day Trading

**Intraday strategies: all positions closed by the bell.** The hardest arena —
costs are the largest fraction of the edge, the counterparties include HFT
firms, and study after study shows 85–97% of retail day traders lose over
time (Barber & Odean; Brazil day-trader studies). Whatever works here is
narrow, mechanical, and cost-obsessed.

## Setups with actual documented evidence

### Opening Range Breakout (ORB)
- Define range = first 5–30 min. Trade the breakout with the gap direction.
- Zarattini & Aziz (2023): 5-min ORB traded **only on "stocks in play"**
  (high relative volume, news catalyst, gapped ≥ big ATR multiple) showed
  large risk-adjusted returns where naive all-stock ORB showed none.
- Lesson: the *stock selection* (relative volume, catalyst) is the edge; the
  ORB is just the entry timing.

### VWAP reversion / VWAP trend
- Institutions benchmark to VWAP, creating gravitational behavior:
  - Trend days: pullbacks to VWAP get bought (VWAP = intraday support).
  - Range days: extremes revert to VWAP (fade stretches).
- The skill is classifying the day type early (gap size, breadth, first-hour
  range vs ATR).

### Gap trading
- **Gap fill (fade):** small unexciting gaps in indices/large caps tend to
  fill intraday.
- **Gap-and-go:** large catalyst gaps (> 4% with news + volume) tend to
  continue, not fill. Direction of edge flips with gap size and catalyst.

### Momentum scalping (low-float)
- Small/low-float stocks with news can move 50–200% intraday on feedback
  loops. Traders like Ross Cameron trade the first pullback after a volume
  surge. Genuine but brutal: spreads are wide, halts are frequent,
  slippage is savage, and the same stocks collapse as fast as they rise.

## The cost wall (do this math before anything)
Per round trip: spread + commissions + slippage. A stock with a 5¢ spread
costs ~5–10 bps immediately; scalps targeting 20–30 bps give away a third of
gross edge to friction *when everything goes right*. Day trading only
survives with: tight-spread instruments, limit-order entries, and per-trade
expected edge ≥ 3× measured friction.

## Regulatory note (US equities)
Pattern Day Trader rule: < $25K accounts get 3 day trades per 5 sessions in
margin accounts. Small accounts route around it with cash accounts (settle
limits), futures, or crypto (no PDT).

## Implementation checklist
- Only trade "in play" symbols: relative volume > 2–3×, catalyst present.
- Classify day type before choosing mean-revert vs trend logic.
- Hard daily loss limit (e.g. -2R = done for the day) — the intraday
  revenge-trade spiral is the #1 account killer.
- Model friction pessimistically; assume worst-quartile fills.
