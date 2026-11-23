# Ed Thorp (1932–)

**Known for:** Inventing card counting (*Beat the Dealer*, 1962), then applying
the same math to markets. His Princeton Newport Partners ran ~**19.1% annualized
for 19 years (1969–1988) without a single losing quarter**. Arguably the first
true quant.

## Contributions
1. **Kelly criterion in practice.** Thorp popularized betting/position sizing
   proportional to edge over variance. Full Kelly maximizes long-run growth;
   half-Kelly gives ~75% of the growth for half the drawdown — his practical
   recommendation.
2. **Options pricing before Black-Scholes.** He derived and traded a version of
   the formula years before it was published, harvesting warrant mispricings
   (*Beat the Market*, 1967).
3. **Statistical arbitrage.** PNP ran early convertible arb, warrant hedging,
   and pairs-style trades — market-neutral books with small, consistent edges.
4. **Risk of ruin thinking.** From blackjack: the question is never just "is
   there an edge?" but "what fraction of capital can I bet so I *survive* to
   realize it?"

## Key principles
- Edge must be measurable, or you have no business betting.
- Overbetting a real edge still ruins you — position sizing is half the game.
- Diversify across independent bets; correlation is the silent killer.
- Fraud and hidden risk detection: he identified Madoff as a fraud in **1991**,
  17 years before the collapse, purely from return-stream analysis.

## Lessons for a systematic trader
| Thorp principle | Systematic translation |
|---|---|
| Kelly sizing | Size positions by edge/variance; cap at fractional Kelly |
| Risk of ruin | Portfolio drawdown breakers; never bet full Kelly |
| Independent bets | Many small uncorrelated positions > one big conviction |
| Measurable edge | If OOS stats don't show the edge, it doesn't exist |
| Survive first | The gate order is: survival constraints, then returns |

## Reading
- *Beat the Dealer* (1962), *Beat the Market* (1967)
- *A Man for All Markets* (2017) — autobiography, the best single book on
  thinking about edge, sizing, and survival.
