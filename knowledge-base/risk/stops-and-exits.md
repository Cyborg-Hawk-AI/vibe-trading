# Stops and Exits

**Entries get the attention; exits produce the P&L.** The same entries with
different exit logic swing a system from losing to winning. Exits encode the
strategy's actual thesis: trend systems exit on structure break, reversion
systems exit on reversion completion, and both die when they borrow the
other's exits.

## Stop-loss types

### Structure stops (best default for swing/breakout)
Below the level that invalidates the trade: the pullback low, the breakout
pivot, the range boundary. If price returns there, the setup failed — exit
is a *logical* statement, not a pain threshold.

### ATR (volatility) stops
`stop = entry − k × ATR(14)`, k = 1.5–3. Adapts to each symbol's noise:
tight in quiet names, wide in volatile ones. The Turtle 2N stop is the
canonical version. Pairs naturally with vol-based sizing: wider stop →
smaller size → identical dollar risk.

### Percentage stops
O'Neil's flat 7–8%. Crude but enforceable, and enforceability is most of
the value of any stop.

### Time stops (underrated)
Exit after N bars if the trade hasn't performed. Raschke: "the best trades
work almost right away." Time stops recycle capital out of dead positions
and cut the tail where slow bleeds live. Essential for mean reversion,
which has no natural price stop.

### The stop you must never use
The mental stop. If it isn't mechanical, it doesn't exist — under stress,
discretion always finds a reason to wait.

## Profit-taking approaches

### Trail everything (trend logic)
No targets; exit on opposite breakout / MA cross / trailing ATR (chandelier:
highest-high − 3×ATR). Maximizes tail capture; gives back open profit at the
end of every trend. Correct when the P&L depends on rare big winners.

### Fixed R-multiple targets (swing logic)
Take profit at 2–3R. Raises win rate, caps the right tail. Correct when the
edge is a repeated short move (swing, reversion), not an open-ended trend.

### Scale-out hybrids (most practical)
Bank 1/2 or 1/3 at 1–2R (pays for the trade, defuses the psychology), move
stop to breakeven, trail the rest. Slightly suboptimal mathematically,
substantially more survivable behaviorally — and it keeps a runner in every
potential monster.

## Placement subtleties
- Stops at obvious round numbers / exact swing lows get swept ("stop hunts"
  are mostly just liquidity clustering). Place beyond the obvious level.
- Wide stop + small size ≡ tight stop + big size in dollar risk, but the
  wide-stop version survives noise better on volatile instruments.
- Gaps and halts jump stops entirely: for gap-prone holdings (earnings,
  biotech, crypto weekends), size assuming the stop fills far beyond its level.

## Checklist
- Every position has a mechanical stop + a time stop before entry.
- Exit style must match strategy thesis (trend trails, reversion targets).
- Log MAE/MFE (max adverse/favorable excursion) per trade — the data tells
  you if stops are too tight (winners stopped early) or too loose.
