# Position Sizing

**The most underrated decision in trading.** Van Tharp's famous demonstration:
give the same entry signals to different sizing algorithms and the results
range from ruin to fortune. Edge discovery and sizing are separate problems —
most blowups (Livermore, LTCM, Williams' 70% contest drawdown) were sizing
failures, not signal failures.

## The frameworks

### Fixed fractional (the default)
Risk a fixed % of current equity per trade: `size = (equity × risk%) / stop_distance`.
- Standard: 0.5–2% risk per trade. At 1%, it takes 69 consecutive losses to
  halve the account — survivability by construction.
- Naturally anti-martingale: size grows in wins, shrinks in drawdowns.

### Kelly criterion (the theoretical optimum)
`f* = (bp − q) / b` for win prob p, loss prob q, payoff ratio b. Or for
continuous returns: `f* ≈ μ / σ²`.
- Maximizes long-run geometric growth. But: full Kelly's swings are violent
  (a 50% drawdown is *expected* behavior), and it assumes you know p and b —
  which you only estimated from a backtest.
- **Practice: half-Kelly or quarter-Kelly.** Half-Kelly gives ~75% of the
  growth at ~half the variance (Thorp's recommendation). Overestimating edge
  + full Kelly = certain ruin, which is why nobody serious runs full Kelly.

### Volatility targeting (the professional standard)
Size positions inversely to their volatility so each contributes equal risk:
`size_i ∝ target_vol / realized_vol_i` (the Turtles' N-sizing; risk parity's
core; every CTA's baseline).
- Also applied at portfolio level: scale total exposure to hit a target
  portfolio vol (e.g. 10–15% annualized), de-levering automatically when
  markets go wild. Vol targeting alone materially improves Sharpe on most
  strategies because volatility clusters.

### Equal-risk many-small-positions (the compounding-machine shape)
Many positions × small risk each × frequency = the Virtu/Medallion geometry.
20 positions at 0.5% risk ≈ same heat as 4 at 2.5%, but far smoother equity,
and the law of large numbers converges to your true expectancy faster.

## Portfolio heat rules (Turtle-style)
- Max total open risk ("heat"): 5–10% of equity across all positions.
- Max per correlated group (all tech longs = one position): ~half of total.
- Max per direction. Correlation spikes to 1 in crashes — the caps are for
  that day, not for normal days.

## The compounding math worth memorizing
- Lose 10% → need +11% back. Lose 25% → +33%. Lose 50% → +100%. Drawdown is
  geometrically punishing, which is why sizing errs small.
- Expectancy per trade × trades per year × fraction risked = growth engine.
  Doubling trade count at the same edge beats doubling size at the same edge
  (same return, lower ruin probability).

## Checklist
- Decide max risk per trade (≤1% until the system is proven live).
- Vol-normalize sizes across symbols.
- Enforce portfolio heat + correlation-group caps.
- Derive Kelly from live stats, then run ≤ half of it.
- Recompute size from *current* equity (auto anti-martingale).
