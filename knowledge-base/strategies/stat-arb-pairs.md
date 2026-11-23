# Statistical Arbitrage & Pairs Trading

**Trade the spread between related instruments, not direction.** Born on
Morgan Stanley's trading desk in the 1980s (Bamberger, Tartaglia's group,
later Shaw and RenTec lineage). Market-neutral: profit comes from
*relationships* converging, immune (in theory) to market direction.

## Classic pairs trading
1. **Find pairs** that move together: same sector, same economics
   (KO/PEP, XOM/CVX, GLD/miners).
2. **Measure the relationship:**
   - Distance method: normalized price series distance (Gatev et al. 2006).
   - Cointegration: Engle-Granger/Johansen test that a linear combination is
     stationary — the statistically correct approach (correlation ≠ cointegration).
3. **Trade the z-score of the spread:**
   - Enter when |z| > 2 (short the rich leg, long the cheap leg).
   - Exit at z ≈ 0 (convergence), stop if |z| > 3–4 (relationship break).
4. **Half-life sanity check:** fit an Ornstein-Uhlenbeck process to the
   spread; only trade spreads whose mean-reversion half-life fits your
   horizon (days, not quarters).

## Evidence & decay
- Gatev, Goetzmann & Rouwenhorst (2006): ~11% annualized market-neutral on
  simple distance pairs, 1962–2002. Post-2002 the simple version decayed
  heavily — crowding. Modern desks survive on speed, better universes
  (ETF vs constituents, cross-listings), and multi-factor residual models.

## The generalized version (true stat arb)
Regress each stock's returns on factors (market, sector, style); trade the
**residual** — bet that idiosyncratic moves revert. Hundreds of small
positions, factor-neutral book, 1–10 day horizon. This is the industrial
version RenTec/D.E. Shaw-style shops run at scale, and it's execution- and
infrastructure-intensive.

## Failure modes (famous ones)
1. **Relationship breaks:** cointegration is a historical statement, not a
   law. GM/Ford diverge permanently; a merger, scandal, or structural change
   kills the spread. Stop-losses on spread widening are mandatory.
2. **Crowding unwinds:** August 2007 "quant quake" — everyone held the same
   spreads; one large liquidation forced all books to unwind simultaneously,
   -20%+ weeks in "market-neutral" funds.
3. **Short leg risk:** borrow costs, buy-ins, and short squeezes (VW 2008
   became briefly the world's most valuable company on a squeeze).
4. **Leverage:** spreads are small; the business requires leverage; leverage
   converts a broken spread into ruin (LTCM is the canonical grave).

## Implementation checklist
- Test cointegration, not correlation; re-test on a rolling basis.
- Only trade half-lives ≤ your holding horizon.
- Hard stop at z > 3–4 or after N days of non-convergence.
- Diversify across many pairs; cap per-pair risk at ≤ 1–2%.
- Model borrow cost and shortability before believing any backtest.
