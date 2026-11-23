# Trading Knowledge Base

A curated reference library: the legends of trading, the strategy families that
actually have evidence behind them, risk management, psychology, and the
pitfalls that kill backtests. Built to serve as a long-term knowledge base for
the autotrader project — see `applied/autotrader-mapping.md` for how each idea
maps to this repo's strategy genome.

## Directory

### Legends (`legends/`)
| File | Who | Known for |
|---|---|---|
| `jesse-livermore.md` | Jesse Livermore | Trend speculation, pyramiding, 1929 short |
| `jim-simons.md` | Jim Simons | Renaissance Medallion, quant pattern trading |
| `ed-thorp.md` | Ed Thorp | Kelly criterion, first quant hedge fund |
| `george-soros.md` | George Soros | Reflexivity, breaking the Bank of England |
| `stanley-druckenmiller.md` | Stanley Druckenmiller | Concentration, never a down year |
| `paul-tudor-jones.md` | Paul Tudor Jones | Macro, 1987 crash, 5:1 risk/reward rule |
| `richard-dennis-turtles.md` | Richard Dennis & the Turtles | Trend following can be taught |
| `william-oneil.md` | William O'Neil | CAN SLIM, growth + momentum |
| `mark-minervini.md` | Mark Minervini | SEPA, volatility contraction pattern |
| `peter-lynch.md` | Peter Lynch | Growth at reasonable price, "buy what you know" |
| `warren-buffett.md` | Warren Buffett | Value, moats, compounding |
| `ray-dalio.md` | Ray Dalio | Risk parity, All Weather |
| `larry-williams.md` | Larry Williams | Short-term futures, %R, record contest run |
| `linda-raschke.md` | Linda Raschke | Short-term swing patterns, market structure |

### Strategy families (`strategies/`)
| File | Family | Time horizon |
|---|---|---|
| `momentum.md` | Cross-sectional & time-series momentum | Weeks–months |
| `trend-following.md` | Managed-futures style trend | Weeks–months |
| `mean-reversion.md` | Short-term reversion, RSI-2 style | Days |
| `breakout.md` | Range/channel breakouts, VCP | Days–weeks |
| `swing-trading.md` | Multi-day swing structure | 2–10 days |
| `scalping-day-trading.md` | Intraday: VWAP, ORB, gaps | Minutes–hours |
| `stat-arb-pairs.md` | Pairs / statistical arbitrage | Days |
| `factor-investing.md` | Value, quality, low-vol, size factors | Months+ |
| `event-driven.md` | Earnings, catalysts, news | Hours–days |
| `market-making.md` | Liquidity provision, spread capture | Seconds–minutes |
| `options-strategies.md` | Premium selling, hedges, convexity | Days–months |
| `crypto-specific.md` | Perp funding, basis, on-chain | Hours–weeks |

### Risk & execution (`risk/`)
- `position-sizing.md` — Kelly, fixed-fractional, vol targeting
- `drawdown-control.md` — circuit breakers, heat rules, recovery math
- `stops-and-exits.md` — ATR stops, trailing, time stops, scaling out
- `execution-microstructure.md` — slippage, spreads, liquidity, order types

### Mindset & process (`process/`)
- `psychology.md` — the failure modes of discretionary and system traders
- `backtesting-pitfalls.md` — overfitting, lookahead, survivorship, costs
- `system-development.md` — how professionals build and validate systems

### Applied (`applied/`)
- `autotrader-mapping.md` — how every idea here maps to this repo's genome,
  gates, and diagnostic failure modes

## How to use this with the autotrader
1. The strategy-family files list concrete, parameterizable rules — candidates
   for new genes or new sleeves.
2. The failure modes in `process/psychology.md` and `risk/` map directly onto
   the diagnostic taxonomy (`premature_stop`, `late_exit`, `oversized_loss`…).
3. `applied/autotrader-mapping.md` is the bridge document — start there.

## Honest note
No strategy in this library promises the project's 25%-per-2-weeks gate.
The legends compounded 20–66% **per year** (Medallion's ~66% gross being the
extreme outlier of all time). What this library offers is the shape of what
has actually worked, so the search explores proven structure instead of noise.
