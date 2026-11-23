# Options Strategies

**Trade volatility, time, and convexity — not just direction.** Options let
you shape the payoff distribution: cap losses, sell insurance, or buy lottery
tickets with defined risk.

## The one concept that rules them all: the Variance Risk Premium
Implied volatility is, on average, priced **above** subsequently realized
volatility (~1–3 vol points on index options). Sellers of options harvest
this premium — and pay for it in crash tails. Every popular income strategy
(covered calls, cash-secured puts, iron condors, strangles) is a variation
of harvesting VRP with different risk shapes.

## The core strategy families

### Premium selling (short volatility)
- **Covered call / cash-secured put:** long stock ± income; the benchmark
  CBOE BXM/PUT indices show equity-like returns with lower vol over decades.
- **Iron condor / strangles:** range bets; steady small wins, rare big losses.
- P&L shape: high win rate, negative skew. The sizing sin is ruinous:
  short-vol accounts die in single events (Feb 2018 "Volmageddon" killed
  XIV in one day; option-selling funds like LJM lost 80%+ that week).
- Rules that keep sellers alive: defined-risk structures only (spreads, not
  naked), position sizing for the max loss not the expected loss, exits at
  ~50% of max profit, never sell vol after long calm stretches.

### Convexity buying (long volatility)
- Long calls/puts/straddles: negative expectancy on average (you pay VRP)
  **except** when you have a timing/selection edge or need crash insurance.
- Taleb/Spitznagel (Universa): systematic tail hedging — bleed small premium
  constantly, collect enormously in crashes (reported ~4,000%+ in Mar 2020).
  The portfolio logic: a 1–3% allocation to convexity lets the other 97%
  stay aggressively long.

### Directional substitution
- Calls/puts as stock substitutes: defined risk (premium = max loss) with
  leverage. For binary events (earnings, FDA), the *only* structurally sound
  way to hold through, since stops can't protect across gaps.
- The cost: theta decay and IV crush — buying options into events pays peak
  IV that collapses post-event. Spreads (debit verticals) mitigate.

### Relative value / vol arb
- Term-structure trades (calendar spreads), skew trades, dispersion (index
  vol vs constituent vol). Professional domain; needs vol-surface modeling.

## Greeks in one table
| Greek | Measures | The practical question |
|---|---|---|
| Delta | Direction exposure | How much stock am I really long? |
| Gamma | Delta's acceleration | How fast does my exposure change if spot moves? |
| Theta | Time decay | What do I earn/pay per day standing still? |
| Vega | Vol exposure | What happens when IV reprices? |

## Implementation checklist
- Know which side of VRP you're on and why.
- Short vol: defined-risk only, size to max loss, respect the skew.
- Long vol: only with an edge (event selection) or as portfolio insurance.
- Model IV crush for any event trade; check IV percentile before buying.
