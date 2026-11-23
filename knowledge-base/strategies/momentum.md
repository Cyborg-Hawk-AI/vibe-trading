# Momentum

**The most robust anomaly in finance.** Assets that outperformed over the past
3–12 months tend to keep outperforming over the next 1–12 months. Documented
by Jegadeesh & Titman (1993), confirmed in 200+ years of data across every
asset class (Geczy & Samonov's "212 years of price momentum").

## Two distinct flavors

### 1. Cross-sectional (relative) momentum
Rank a universe by trailing return; buy the top decile/quintile, (short the
bottom). It's a *relative* bet — winners vs losers — roughly market-neutral
if long-short.
- Classic parameters: rank on **12-month return skipping the last month**
  (12-1), hold 1 month, rebalance monthly.
- The skip-month matters: the most recent month exhibits *reversal*, not
  momentum (short-term reversal effect).

### 2. Time-series (absolute) momentum
Each asset is measured against **its own past**: if its trailing N-month
return is positive, be long; otherwise flat/short (Moskowitz, Ooi, Pedersen
2012). This is trend following in academic clothing.
- Classic: 12-month lookback, monthly evaluation.
- Cuts drawdowns dramatically because it exits everything in sustained bears.

## Why it works (best theories)
- **Underreaction:** information diffuses slowly; investors anchor to old prices.
- **Herding/feedback:** performance chasing amplifies moves (Soros's reflexivity).
- **Disposition effect:** holders sell winners too early (suppressing the move,
  extending it) and hold losers too long.

## Known failure modes
- **Momentum crashes:** violent reversals when a bear market bottoms — the
  short (loser) leg rips upward (2009: -73% for long-short momentum). Fixes:
  vol-scaling the strategy, long-only implementations, regime filters.
- **High turnover** — the edge is real but transaction costs eat naive
  implementations, especially in small caps.
- Crowding: decay is documented but the premium persists post-publication.

## Implementation checklist
- Lookback: 3–12 months (6 and 12 most robust); skip the last 1–4 weeks.
- Rebalance: weekly–monthly. Faster = more edge before costs, less after.
- Universe: liquid enough that costs < edge; momentum is strongest in
  mid-caps and *most dangerous* in illiquid microcaps.
- Vol-scale positions (target equal risk) — improves Sharpe materially.
- Combine with a market regime filter to sidestep momentum crashes.

## Key stats (long-run, academic, gross of costs)
- Cross-sectional US equity momentum: ~1.0% monthly long-short premium.
- Time-series momentum (diversified futures): Sharpe ~1.0 pre-costs at the
  portfolio level, near-zero correlation to equities.
