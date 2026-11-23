# Lessons learned (self-written, evidence-based)

_Regenerated 2026-09-03 23:03 by autotrader.lessons. Every line below is mined from measured results (registry backtests, live paper journal) — never from intuition. Sample sizes included; small-sample lessons are excluded by construction._

## What the evidence currently says

- WARNING — replay-vs-live gap: champion shows 60% green days in replay but the live desk runs 22% over 9 sessions. Suspect overfitting to the recorded library or friction underestimation; weight live evidence higher.
- Sleeve equities averages 16.5% higher green-day rate than equities_penny (n=36714 vs 25839).
- use_regime_gate: OFF averages 6.1% higher held-out green-day rate than ON (n=71280 vs 27446 backtests).
- use_rsi_gate: OFF averages 5.1% higher held-out green-day rate than ON (n=35231 vs 63495 backtests).
- use_range_compress: OFF averages 4.6% higher held-out green-day rate than ON (n=55305 vs 43421 backtests).
- Live exits via 'atr_stop': 4881 trades, net $-309.81, win rate 12%.
- Live exits via 'culled': 1066 trades, net $-56.76, win rate 12%.
- Live exits via 'time_stop': 101 trades, net $-44.54, win rate 44%.
- Live exits via 'dd_breaker': 27 trades, net $-10.10, win rate 4%.
- Live exits via 'session_eod_flat': 952 trades, net $+1.07, win rate 39%.
- Live exits via 'rule_exit': 4936 trades, net $+181.52, win rate 48%.
- Live desk green-day rate so far: 2/9 (22%).
- Hunter scalps, hold <15m: -2.636 $/trade expectancy over 17 trades (win rate 0%).
- Hunter scalps, gap 5-10%: -2.025 $/trade expectancy over 31 trades (win rate 0%).
- Hunter scalps, hold 15-40m: -1.215 $/trade expectancy over 15 trades (win rate 27%).
- Hunter scalps, source 'scan': -1.121 $/trade expectancy over 67 trades (win rate 22%).
- Hunter scalps, gap >10%: -0.687 $/trade expectancy over 76 trades (win rate 33%).
- Hunter scalps, source 'news+stocktwits': -0.570 $/trade expectancy over 21 trades (win rate 38%).
- Hunter scalps, source 'stocktwits': -0.468 $/trade expectancy over 45 trades (win rate 56%).
- Hunter scalps, hold >40m: -0.447 $/trade expectancy over 101 trades (win rate 44%).
- Oracle capture rate over the last 8 post-mortems: mean -10.1% (latest 0.0%). The gap to 100% is money the scanner/entry timing left on the table.
- Missed money on 2026-09-02: oracle-profitable names the hunters never touched: DELL ($9), IREN ($7), SOXL ($7). Check whether the scanner ranked them too low or the buzz sources saw them too late.
- Swing attribution over the last 90 large intraday moves: 8% news-driven, 9% buzz-driven, 83% no visible catalyst. No-catalyst moves are pure momentum — they reverse faster; tighter time stops apply.
- Buzz lead-lag: `news` leads price by a median 38020 minutes across recent attributed swings (n=10 source-days). Prefer that source when ranking the hot list.
