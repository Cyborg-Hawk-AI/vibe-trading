# Event-Driven Trading

**Trade the predictable behavior around discrete information events.**
Catalysts compress a lot of repricing into a short window — exactly where
short-horizon strategies can find edge that slower money leaves behind.

## The documented event effects

### Post-Earnings Announcement Drift (PEAD)
The flagship anomaly (Ball & Brown 1968; Bernard & Thomas 1989): stocks with
big positive earnings surprises **keep drifting up for weeks** after the
announcement (and negative surprises drift down). Underreaction, not
overreaction. Playbook:
- Rank surprise (SUE score or reaction-day gap + volume).
- Enter the drift *after* the initial gap (e.g., first pullback), hold 2–8 weeks.
- Strongest in smaller, under-covered names; weaker but present in large caps.

### Earnings gap continuation
A gap > 5–8% on earnings with volume 3×+ average that **holds its opening
range** tends to continue in the gap direction for days-weeks (institutional
accumulation takes days, not minutes). Failed gaps (fill same day) signal the
opposite.

### Index rebalancing
Additions to/deletions from S&P/Russell indices force benchmarked funds to
trade — historically produced predictable pre/post-effect moves. Heavily
arbitraged now; Russell reconstitution week still moves small caps.

### M&A / merger arbitrage
Buy the target below deal price, capture the closing spread. Positive carry,
tail risk on deal breaks. A professional's game (legal edge on antitrust
assessment) — for retail, the useful signal is *rumor behavior*: unusual
volume + drift often precedes announcements (never trade on non-public info).

### Other tradeable catalysts
- FDA decisions (biotech binary events — options-defined risk only).
- Guidance changes, analyst day, product launches.
- Crypto: exchange listings, token unlocks (supply events → predictable
  pressure), protocol upgrades, ETF flows.

## The general lessons
1. **The reaction beats the news.** A stock rallying on bad news is telling
   you positioning was worse than fundamentals (and vice versa). Trade the
   reaction, not your interpretation.
2. **Volume validates.** Events without volume expansion don't drift.
3. **Point-in-time discipline is everything in backtests.** Earnings
   timestamps, revision dates, and news times must be exact or the backtest
   silently trades on the future. (See `process/backtesting-pitfalls.md`.)

## Failure modes
- Binary events with undefined risk (holding through earnings/ FDA without
  hedges) — a single gap can exceed any stop.
- Crowded event trades (index adds) now often *reverse* post-event.
- Halts and gaps mean stops don't protect — size assuming the stop fails.

## Implementation checklist
- Separate "hold through event" (needs defined risk) from "trade the
  aftermath" (PEAD/gap-continuation — safer, still edge-rich).
- Require volume confirmation; enter on the first structured pullback.
- Exit by time (drift decays in weeks) or trailing structure.
