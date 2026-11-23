# Backtesting Pitfalls

**Most published backtests are false.** Bailey, Borwein, López de Prado made
the formal case ("Pseudo-Mathematics and Financial Charlatanism"): search
enough configurations and a spectacular in-sample Sharpe is *guaranteed* by
chance. Every pitfall below has destroyed real money.

## The deadly sins

### 1. Overfitting / selection bias under multiple testing
Test 1,000 parameter combos, report the best — its expected out-of-sample
performance is near zero. The deflated Sharpe ratio exists precisely to
discount for number of trials.
**Defenses:** hold-out OOS periods never touched during search; walk-forward
testing; parameter-neighborhood robustness (if N=20 works but 15 and 25
don't, it's noise); count and record every trial; prefer strategies with
*economic rationale* over pure pattern mining.

### 2. Lookahead bias
Using information before it existed: signals computed on the close you're
buying, indicators centered on future bars, fundamentals as-revised instead
of as-reported, news timestamped by publication date but containing later
edits.
**Defenses:** next-bar-open execution; point-in-time datasets; timestamp
audits; the paranoid rule — any suspiciously good backtest has lookahead
until proven otherwise.

### 3. Survivorship bias
Backtesting today's ticker list on the past silently drops the delisted, the
bankrupt, the acquired. US equity universes lose several % of names a year;
strategies on survivors overstate returns by ~1–4%/yr, worst in small caps
and *catastrophic* in penny stocks and altcoins (where most of the universe
died — the exact names the survivor list forgets).
**Defenses:** point-in-time universe membership; include delisting returns;
be triply suspicious of small-cap/penny/alt backtests.

### 4. Unrealistic costs and fills
Zero slippage, mid-price fills, infinite liquidity at the close, shorting
hard-to-borrow names for free. See `risk/execution-microstructure.md`.
**Defenses:** friction floors by instrument class; participation caps;
re-test at 2× costs — a real edge survives.

### 5. Data-snooping the regime
A strategy built on 2020–2021 data learned one regime. It will meet 2022.
**Defenses:** test across multiple regimes (bull/bear/chop, high/low vol);
demand the strategy either works across regimes or *knows* its regime and
gates itself.

### 6. Backtest-to-live gaps nobody models
Halts, delistings mid-position, dividend/split errors, exchange outages,
API failures, partial fills, PDT constraints. Reality is lumpier than any
simulator.

## Validation ladder (what evidence actually accumulates)
1. In-sample fit — worth almost nothing on its own.
2. Out-of-sample hold-out — first real evidence.
3. Walk-forward (rolling re-fit + forward test) — better; mimics deployment.
4. Parameter robustness + 2× cost stress — separates structure from noise.
5. **Paper/shadow trading** — catches execution and data-pipeline reality.
6. Small live capital — the only test of fills, borrow, and slippage.
7. Scale gradually as live stats confirm backtest stats (they'll be worse;
   plan for live ≈ 50–70% of backtest performance).

## Sanity heuristics
- Live Sharpe ≈ half of backtest Sharpe is the folk-wisdom haircut.
- If annualized return > 100% in-sample: assume a bug or bias, hunt it.
- Fewer than ~100 trades = anecdote, not statistics.
- The more beautiful the equity curve, the more likely the crime.
