# Jim Simons (1938–2024)

**Known for:** Founder of Renaissance Technologies. The Medallion Fund is the
best track record in the history of money management: roughly **66% gross /
39% net annualized from 1988–2018** with no losing year after 1989.

## Background
- World-class mathematician (Chern–Simons theory), NSA codebreaker, chair of
  Stony Brook math department before founding RenTec in 1982.
- Hired physicists, mathematicians, signal-processing and speech-recognition
  researchers (Peter Brown, Robert Mercer from IBM) — almost no Wall Street hires.

## What is actually known about the method
RenTec is famously secretive, but the credible public record (Zuckerman's
*The Man Who Solved the Market*, interviews, court filings) supports:

1. **Short-horizon statistical patterns.** Thousands of small, weak signals —
   each barely better than a coin flip — combined into a portfolio where the
   edge is statistically overwhelming. Holding periods from minutes to weeks.
2. **Enormous trade count.** The edge per trade is tiny; the law of large
   numbers does the work. (Directly relevant to a "dozens of small trades"
   sizing philosophy.)
3. **Everything is tested against data.** No trade because of a story. Signals
   must be statistically significant, repeatable, and survive out-of-sample.
4. **Mean reversion + trend components.** Early Medallion profited heavily from
   short-term reversion effects and commodity trend structure.
5. **Execution as alpha.** Modeling their own market impact and transaction
   costs was as important as signal discovery — bad execution destroys weak edges.
6. **Leverage on a diversified book.** The basket is hedged/market-neutral-ish,
   then levered (reportedly up to ~12–20×) because the *portfolio* is stable.
7. **One integrated model.** Not silos of strategies but one book that weighs
   all signals simultaneously.

## Cultural principles
- Never override the model. Humans intervene only in crisis liquidity events.
- Publish nothing; poach no Wall Street thinking; hire scientists.
- Rigorous peer review internally — a signal enters the system only after
  surviving adversarial challenge.

## Lessons for a systematic trader
| RenTec principle | Systematic translation |
|---|---|
| Many weak signals | Combine multiple entry gates instead of one "great" one |
| Huge trade count | Small position caps, many positions, compounding |
| Cost modeling | Friction floors in every simulation, never optimistic fills |
| Out-of-sample discipline | Hard OOS gates; kill anything that only works in-sample |
| Never override the model | Full autonomy in execution; humans set constraints only |
| Signal decay | Expect edges to decay; keep the search running continuously |

## Reality check
Medallion caps itself at ~$10B and returns capital — the strategies do not
scale. Its ~66% gross is the singular outlier of history and was built by
dozens of PhDs over decades with proprietary data and execution. It is the
existence proof that markets are beatable, not a template for its magnitude.
