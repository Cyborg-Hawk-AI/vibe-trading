# Trading Psychology & Behavioral Failure Modes

**Markets are hard because they're priced by humans with the same bugs you
have.** Behavioral finance (Kahneman/Tversky) documented the bugs; the
legends' rules are mostly bug-patches; a fully automated system is the
ultimate patch — but the bugs reappear at the level of the system's *operator*
and *designer*.

## The core biases and their trading expressions

| Bias | Trading expression | Classic patch |
|---|---|---|
| **Loss aversion** (losses hurt ~2× as much) | Holding losers, cutting winners — the disposition effect | Mechanical stops; "losers average losers" (PTJ) |
| **Overconfidence** | Oversizing after wins; overtrading | Fixed risk %; Druckenmiller's sizing by recent form |
| **Recency bias** | Chasing what just worked; abandoning systems mid-drawdown | Judge systems on 100+ trades, not 10 |
| **Confirmation bias** | Seeing only evidence for your position | Pre-defined invalidation level = the stop |
| **Anchoring** | "It's cheap because it fell 50%" / waiting for breakeven | Trade the setup, not your entry price |
| **Sunk cost** | Averaging down to "fix" a mistake | Livermore/PTJ: never average losses |
| **Lottery preference** | Overpaying for long shots (bad OTM options, penny pumps) | Expectancy math over payoff fantasies |
| **Revenge trading** | Doubling risk after a loss to "get it back" | Daily loss breaker: flat and done |

## The two failure phases of every trader
1. **Failure of method** (fixable with study): no defined edge, random entries.
2. **Failure of discipline** (the killer): a method exists, but under stress
   the trader overrides it. Eckhardt on the Turtles: the washouts all had
   the same rules — they just couldn't take signal #11 after ten losses.

## What the legends converge on
- Livermore: his rules were fine; his bankruptcies came from breaking them.
- PTJ: "revenge trading is the beginning of the end."
- Raschke: consistency of execution *is* the edge; setups are secondary.
- Douglas (*Trading in the Zone*): think in probabilities — any single trade
  is noise; only the distribution over many trades is real. The practical
  target: execute 20 trades in a row with zero deviations ("consistency
  before profitability").
- Seykota (*Market Wizards*): "Everybody gets what they want out of the
  market" — unexamined motives (excitement, self-punishment) express
  themselves as losses.

## For an automated system, the psychology moves up a level
The bot can't revenge-trade, but the **operator** can:
- Turning the system off during its drawdown (selling the low of the equity
  curve) — the automated version of capitulation.
- Overriding signals ("the model doesn't know about X") — Simons's cardinal sin.
- Tinkering after every losing week → perpetually trading a v0.9 system
  (recency bias expressed as code churn).
- Cranking size after a hot streak the sample doesn't justify.

The patches are the same in spirit: pre-committed rules **about the system**
(when it may be revised, what statistical evidence justifies stopping it,
sizing changes only at scheduled reviews with sufficient sample).

## The designer's biases (see backtesting-pitfalls.md)
Overfitting is confirmation bias in code. Survivorship in universe selection
is availability bias in data. The knowledge base's entire process section is
behavioral finance applied to the person *building* the machine.
