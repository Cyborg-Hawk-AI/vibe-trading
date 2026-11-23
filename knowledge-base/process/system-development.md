# System Development Process

**How professionals go from idea to deployed strategy.** The synthesis of
Tomasini/Jaekle, Robert Carver (*Systematic Trading*), Ernest Chan, and the
documented practices of quant desks.

## The pipeline

### 1. Hypothesis first
Start from an economic reason edge could exist: whose behavior creates the
mispricing, and why can't/won't faster money arbitrage it away? (Underreaction,
forced flows, risk premia, liquidity provision, structural constraints.)
Pattern-mining without a "why" produces overfit artifacts — see
`backtesting-pitfalls.md`. Carver: "ideas first, not data first."

### 2. Simplest possible implementation
Express the hypothesis in the fewest rules that could capture it. Every
added parameter is another dimension to overfit. The trend-following
lesson: 100-year edges are parameter-insensitive; if your rule needs
5 finely-tuned constants, it's describing the past, not the future.

### 3. Backtest honestly
Point-in-time data, realistic frictions, next-bar fills, multi-regime span.
Record how many variants were tried (the multiple-testing count discounts
the result).

### 4. Robustness battery
- Parameter perturbation: performance surface should be a plateau, not a spike.
- Cost stress: 2× friction.
- Universe perturbation: does it work on the neighboring 50 symbols too?
- Time perturbation: start the backtest a year later/earlier; walk-forward.
- Regime cuts: bull/bear/chop; high/low vol.

### 5. Judge by the right numbers
| Metric | What it tells you | Trap |
|---|---|---|
| Expectancy (R/trade) | The raw edge | Meaningless under ~100 trades |
| Sharpe | Risk-adjusted consistency | Hides skew (short-vol looks great until it doesn't) |
| Max drawdown & duration | Survivability + operator patience required | Backtest max DD is a *floor*, not ceiling |
| Profit factor (gross win/gross loss) | Robustness margin | 1.1 dies to costs; >2.5 in-sample smells overfit |
| Win rate × payoff ratio | The P&L shape | Neither matters alone |
| Trade count | Statistical significance | The most ignored number in retail backtests |

### 6. Paper → small live → scale
Shadow trade to validate the *pipeline* (data, signals, orders). Then small
real capital to validate *fills*. Scale only as live statistics confirm
backtest statistics. Expect live ≈ 50–70% of backtest.

### 7. Monitor for decay, retire without mercy
Alpha decays (crowding, regime change, structural shifts). Define in advance
the statistical tripwire that retires a strategy (e.g., live drawdown > 1.5–2×
backtest worst, or expectancy CI crossing zero on a rolling window). Keep the
research loop running: a portfolio of strategies with a birth/death process,
not a single immortal system.

## Portfolio-level assembly (Dalio's free lunch)
Combining lowly-correlated systems does more for the equity curve than
improving any single one. Trend + reversion is the classic pairing — their
P&L shapes (rare big wins vs frequent small wins) offset each other's
droughts. Allocate by inverse-vol / equal risk contribution across systems.

## Cadence discipline for the operator
- Changes to a live system happen at scheduled reviews with pre-registered
  criteria — never mid-drawdown on emotion (see `psychology.md`).
- Version everything; every parameter change is an experiment with a
  hypothesis, an expected effect, and a review date.
