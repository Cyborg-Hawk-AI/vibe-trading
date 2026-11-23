# Drawdown Control

**Survival is the strategy.** Every legend says a version of it: Buffett's
"never lose money," PTJ's "play great defense," Soros's "survive first."
The asymmetry of losses (need +100% to recover -50%) means drawdown control
is not risk aversion — it *is* long-run return maximization.

## The recovery math (the whole argument in one table)
| Drawdown | Gain needed to recover |
|---|---|
| 5% | 5.3% |
| 10% | 11.1% |
| 20% | 25% |
| 30% | 42.9% |
| 50% | 100% |
| 70% | 233% |

## Control mechanisms, in layers

### Layer 1 — per-trade
Stops sized so a single loss ≤ 0.5–1.5% of equity. (See `stops-and-exits.md`.)

### Layer 2 — daily/weekly circuit breakers
- Daily loss limit: e.g. -2 to -3R or -2% equity → flat, done for the day.
  Kills the revenge-trade spiral, the #1 discretionary account killer, and
  bounds the damage from a broken bot equally well.
- Weekly limit: e.g. -5% → half size or flat until review.

### Layer 3 — equity-curve-based exposure scaling
Scale exposure with recent performance (Druckenmiller's "press when hot,
shrink when cold"; PTJ trades smallest when trading worst):
- Under a 5% drawdown: full size.
- 5–10%: half size.
- >10%: minimum size or paper-only until the curve stabilizes.
This converts an equity-curve dive into an automatic soft landing and is
trivially mechanizable.

### Layer 4 — hard stop on the system itself
A max drawdown at which the strategy is *retired for review*, not resized
(e.g. 2× worst backtest drawdown). Rationale: live drawdown exceeding all
backtest history means the model is broken or the regime changed — more
size won't fix a dead edge.

## Volatility clustering: the free early-warning
Volatility clusters (GARCH is finance's most replicated fact). Most equity
disasters happen *after* realized vol has already risen. Vol-targeting
(cutting exposure when vol expands) mechanically front-runs the worst days —
one of the few free lunches in risk.

## What the disasters teach
- **LTCM (1998):** Nobel-grade models, 25:1 leverage, correlated "diverse"
  bets → -92% in months. Lesson: leverage converts being early into being dead.
- **Melvin Capital (2021):** short squeeze concentration → -53% in a month.
  Lesson: positions with unbounded loss need hard caps, not confidence.
- **Quant quake (Aug 2007):** everyone's "uncorrelated" books were the same
  book. Lesson: your correlation matrix is wrong exactly when you need it.

## Checklist
- Per-trade risk cap, daily breaker, equity-curve scaling, system kill-line —
  all four layers, all automated.
- Measure drawdown from peak equity continuously; log every breaker trip.
- Treat "live drawdown > backtest worst × 1.5–2" as a model-invalidation
  event, not a rough patch.
