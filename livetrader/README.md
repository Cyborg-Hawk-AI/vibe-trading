# livetrader — the real-time paper-trading twin

Where `autotrader/` evolves strategies against frozen historical snapshots,
`livetrader/` runs an **arena of genome-driven traders against the live
market** — current quotes, current news, geopolitics, weather, oil, rates —
simulating every fill with fees, spread, and slippage. **No real order is
ever sent.** The mission: trade all day, every day, until the arena proves a
strategy at the benchmarks (≥25% per rolling 2 weeks, drawdown ≤10%) on live
data — the final gate before real money.

## How it works

```
 feeds ──► MarketData (shared tape) ──► Arena of N paper traders ──► PaperBooks
   │                                        │ evaluate every ~5s
   │  Binance REST (crypto, 24/7)           │ evolve every ~30min (cull + breed)
   │  yfinance 1m bars (equities, RTH)      │
   │  Yahoo movers screen (discovery)       ▼
   │  macro tape (oil/VIX/DXY/10y/gold)   LLM check-ins (Kimi-K3 → Bedrock)
   │                                      memo + bounded arena amendments
 news ──► NewsStore (RSS + NWS alerts, keyword-scored)
          └► periodic LLM macro assessment → exposure scaling + avoid-list
```

- **Same genome as the autotrader** (`autotrader.strategy_space`): momentum
  ranking, RSI/trend/breakout gates, ATR stops, vol-targeted sizing,
  drawdown breaker, small-trade mode. Timeframe gene maps to 5m/15m/60m
  intraday bars. Champions from the overnight historical search are seeded
  into the arena at startup.
- **Live evolution:** every ~30 minutes the bottom quartile of paper books is
  culled and replaced with mutations/crossovers of the live leaders plus
  fresh randoms. Selection pressure comes from *reality*, not history.
- **News is a risk governor, not a trade signal:** keyword sentiment runs on
  every headline (free); Kimi/Bedrock produces a bounded macro assessment
  every ~30 minutes (risk bias, avoid/favor lists). Biases scale exposure
  0.4×–1.2× and veto strongly-negative names — a hallucination can tilt
  sizing but can never place a trade.
- **Overnight:** crypto (24/7) keeps the arena learning while US equities
  sleep; equity traders wake at 09:30 ET with warmed history.
- **Throughput:** ~48 traders × ~100 symbols × one pass every ~5s ≈ several
  million signal evaluations per day (reported in the heartbeat and status).

## Run it

```bash
# start detached + watchdog (survives closing the terminal; keeps Mac awake)
python3 livetrader/scripts/start_livetrader.py

# health check / stop
python3 livetrader/scripts/start_livetrader.py --status
python3 livetrader/scripts/start_livetrader.py --stop

# foreground (debugging)
.venv/bin/python -m livetrader.daemon
```

## Where results land (all under `livetrader/state/`)
- `live_track_record.md` — the proof report: champion, leaderboard, benchmark pace
- `status.json` — machine-readable health + arena aggregates
- `arena_state.json` — every trader's genome and live stats (restart-safe)
- `checkins.jsonl` — every LLM memo and accepted amendment
- `daemon.log` / `watchdog_monitor.log`

## Honest constraints
- Free data: crypto quotes are true real-time; equity 1m bars lag up to ~1
  minute — fills charge spread + slippage so the simulation stays pessimistic.
- "Millions per day" = signal evaluations. Fill counts are bounded by real
  bar cadence; a strategy that only wins by trading thousands of times per
  minute would be fiction on this data, so the arena can't learn one.
- A live champion here is evidence, not proof: it must hold its stats for
  weeks before the real-money interlocks (in `autotrader/live_connector.py`)
  are worth arming.
