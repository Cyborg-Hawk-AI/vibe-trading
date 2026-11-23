# Market Making & Liquidity Provision

**Earn the spread by quoting both sides; manage the inventory and adverse
selection you accumulate doing it.** The purest "casino side" of trading —
and the domain where speed, queue position, and infrastructure decide who
survives. Included for understanding, not because retail should attempt it
on equities.

## The economics
- Revenue: bid-ask spread × volume traded (plus exchange rebates).
- Cost 1 — **Inventory risk:** you accumulate a position as one side gets
  hit; the market may trend against it.
- Cost 2 — **Adverse selection:** informed traders hit your quotes exactly
  when they're stale. You systematically buy before drops and sell before
  rips unless you can update quotes fast enough.
- The business is profitable when spread + rebates > adverse selection +
  inventory variance. Hence the arms race in speed.

## The canonical model (Avellaneda-Stoikov 2008)
Optimal quotes around a **reservation price** that skews with inventory:
long inventory → shade both quotes down (eager to sell, reluctant to buy).
Every practical market maker implements some version of inventory-skewed
quoting with volatility-scaled spreads.

## Who the legends are here
- **Ken Griffin / Citadel Securities** — executes ~a quarter of US equity volume.
- **Jane Street** — ETF arbitrage and market making, options.
- **Virtu** — famously disclosed 1 losing day in 1,238 (IPO filing): the law
  of large numbers at maximum expression — thousands of tiny positive-EV
  trades a day.

## Where small players can actually do it
- **Crypto:** 24/7, fragmented venues, maker rebates, wider spreads on
  long-tail pairs. Amateur market making bots (Hummingbot etc.) can be
  positive-EV on illiquid pairs — until volatility spikes or an informed flow
  arrives. Inventory limits and kill switches are everything.
- **Illiquid options/altcoins:** wide spreads compensate; adverse selection
  lower. But sizing must respect that exits are as illiquid as entries.

## The transferable ideas (even for directional traders)
1. **Virtu's lesson = the small-trades thesis:** many tiny uncorrelated
   positive-EV trades produce absurd consistency. Frequency × edge × law of
   large numbers > any single prediction.
2. **Adverse selection thinking:** whenever your limit order fills, ask "why
   was someone eager to give me this fill?" Passive fills in fast markets are
   usually the wrong side.
3. **Inventory skew:** your existing book should change your willingness to
   add — the systematic version of "don't pyramid your exposure by accident."
4. **Spread = the tax every strategy pays.** Understanding MM economics is
   understanding your own transaction costs. See `risk/execution-microstructure.md`.
