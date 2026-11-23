# Trading knowledge digest (for LLM check-in prompts)

Condensed from knowledge-base/. Injected into the research-director system
prompt of both the autotrader (historical search) and livetrader (live arena)
so amendment proposals draw on the documented literature. Keep under ~2,500
characters — this rides along on every check-in.

## Proven strategy archetypes (search priors)
- TREND/BREAKOUT (Turtles, Livermore, PTJ): trend gate ON + breakout entry +
  wide ATR stop (2-3N) + LONG max holding. Prefer use_regime_gate ON,
  use_volume_gate ON, use_range_compress ON. Win rate 30-45%; P&L in tails.
- REVERSION (Connors RSI-2, Raschke): trend/regime ON + RSI oversold + SHORT
  holds + time stop. Prefer use_pullback_entry for swing entries.
- ANTI-PATTERN: breakout + tight stop + short hold. Fix with volume/compress
  gates and wider ATR.
- SMALL-TRADE MODE pairs with reversion better than with trend.
- Use mom_skip_bars > 0 for cleaner cross-sectional momentum (12-1 style).

## Failure-mode fixes from the literature
- premature_stop: widen ATR mult, smaller size (same $ risk). Stops inside
  the noise band are donations.
- fakeout_entry: demand volume expansion + range contraction before breakout;
  align with higher-timeframe trend (O'Neil/Minervini).
- whipsaw_churn: trend logic in chop — lengthen lookbacks, raise entry bar.
- overbought_entry: buy the first pullback, not the vertical (Raschke).
- late_exit: trail structure, bank partial at 1-2R.
- oversized_loss: vol-normalized sizing, heat caps (Turtles: max 4 units/
  market, 12/direction). Sizing failures, not signal failures, kill accounts.
- illiquid: skip names where spread > edge; penny spreads run 1-5%.

## Sizing & risk canon
- Vol targeting (inverse-vol weights) is the professional standard; improves
  Sharpe on nearly everything (Turtles' N, risk parity).
- Fractional Kelly only; full Kelly with estimated edges = eventual ruin
  (Thorp). Overestimating edge + aggressive sizing is the #1 blowup cause.
- Drawdown is geometric: -25% needs +33%. Prefer amendments that cut DD over
  ones that chase return.
- Many small uncorrelated positions beat few concentrated ones for ruin
  probability at equal expected return (Virtu/Medallion geometry).

## Skeptic's checklist (apply to every cycle result)
- Operational gate is >=3% per 2-week window in >=30% of OOS windows with
  MDD<=10%. The old 25%/80% bar is a stretch diagnostic only — zero stretch
  passers is expected and must not block promotion of operational passers.
- Huge in-sample/train edge with weak OOS = overfit; narrow gene bounds
  toward parameter plateaus, not spikes (robust edges are parameter-insensitive).
- <100 trades = anecdote. Prefer redirecting the search over loosening
  exploration randomly when operational passers are scarce.
- Regime matters: 3 of 4 stocks follow the market (O'Neil's M). If everything
  fails at once, suspect regime, not genes. Prefer use_regime_gate ON.
