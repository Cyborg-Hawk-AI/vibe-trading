# Autotrader: Autonomous Trading Architecture, Simulation Engine & Mutation Loop

Design document for the `autotrader/` package — a fully local, self-improving
strategy discovery pipeline layered on top of the two repositories in this
workspace:

- **Vibe-Trading** (`agent/`) supplies the deterministic machinery: market
  engines with realistic execution rules, 20+ OHLCV loaders with fallback
  chains and a local cache, metrics, and statistical validation.
- **TradingAgents** (`TradingAgents/`) supplies the qualitative layer: a
  multi-agent LLM debate pipeline used as a *committee review* for finalist
  strategies only.

Neither upstream tree is modified — both remain pullable. `autotrader`
imports `backtest.*` by putting `agent/` on `sys.path`
(`autotrader/bootstrap.py`) and patches TradingAgents prompts at runtime.

## Operational parameters

| Parameter | Value |
|---|---|
| Starting capital | $100 equities sleeve + $100 crypto sleeve (simulated separately, like the real accounts) |
| Acceptance gate | net return ≥ 25% in ≥ 80% of held-out 2-week windows AND max drawdown ≤ 10% |
| Equity friction | slippage 0.05% (hard floor in `FrictionConfig`), zero commission |
| Crypto friction | taker 0.075% both sides, slippage 0.10%, optional additive gas-fee bump |
| History | ≥ 2 years of daily bars (`2024-07-01` → `2026-07-01`, configurable) |
| Universe | ~120 liquid US names/ETFs + top 30 crypto pairs + ~40 liquid low-priced ("penny") names, sliced by config |
| Compute | all simulation local; AWS = S3 artifact sync + Bedrock LLM fallback |
| Outer-loop LLM | Kimi (Moonshot, OpenAI-compatible) primary → AWS Bedrock Claude Opus fallback; keys in `~/.config/autotrader/llm.env`, never in the repo |

## The three pillars

### Pillar 1 — Autonomous simulation engine (`autotrader/simulator.py`)

```
snapshot (parquet, offline) ──> render genome -> signal_engine.py
                                      │
                                      ▼
                    vibe-trading market engine (in-process)
              GlobalEquityEngine("us") / CryptoEngine, real friction
                                      │
                                      ▼
                equity curve ──> rolling 2-week windows (step = 1 week)
                                      │
                     train windows (first 70%) │ OOS windows (last 30%)
                            fitness            │       HARD GATE
```

- **One full-period engine run per evaluation**, windows scored on the equity
  curve. This models continuously compounding capital ($100 → $500 → $1,000+)
  and is ~50× cheaper than a fresh backtest per window. Window pass =
  `ret ≥ 25% AND window MDD ≤ 10%`.
- **Point-in-time integrity.**
  - The engine executes at *next-bar open*: signals are shifted one bar in
    `backtest/engines/base.py::_align` before any fill.
  - Rendered strategies use only backward-looking rolling computations; the
    breakout gate shifts its prior-high window by one bar; the drawdown
    circuit breaker is computed from the strategy's own causal proxy equity.
  - The runner's AST sandbox (`_validate_signal_engine_source` +
    runtime-reachable scrubber) is applied to every rendered genome — the
    simulator loads strategies through the runner's own validators, so
    generated code can't do I/O or network even in a sweep.
  - Data is a **frozen parquet snapshot** of settled daily bars
    (`autotrader/data.py`), fetched once through the loader registry with
    `VIBE_TRADING_DATA_CACHE=1`. Simulation sweeps never touch the network.
  - *Known leak surfaces (documented, accepted):* the curated universe has
    survivorship bias (today's liquid names, historical window); daily bars
    hide intrabar stop dynamics (ATR stops evaluated on closes).
- **Execution realism** is injected per sleeve from `FrictionConfig`:
  equities via `slippage_us` (floor-checked at 0.05%, US commission is
  structurally $0 in `GlobalEquityEngine`); crypto via
  `taker_rate=maker_rate=0.075%` (conservative: the engine charges the maker
  rate on closes, so we equalize them) + `slippage=0.10%`. Crypto funding
  fees are charged daily by `CryptoEngine.on_bar`. Fractional shares (US) and
  fractional crypto sizes are native to the engines.
- **On-demand research invocation** (`autotrader/research_hooks.py`): a
  deterministic anomaly detector flags volume spikes (>5× 20-bar median) and
  volatility spikes (>4σ) per tick. Events are logged during simulation and
  enriched *outside the hot loop* with PIT-filtered yfinance headlines
  (publication timestamp ≤ event date) and an optional LLM digest. No LLM
  call ever happens inside the tick loop.

### Pillar 2 — Mutation & nuance-tracking loop (`autotrader/optimize_loop.py`)

- **Strategy genome** (`autotrader/strategy_space.py`): 23 bounded parameters
  — cross-sectional momentum selection, RSI/trend/breakout entry gates, ATR
  trailing stop, RSI + min/max-holding exits, inverse-vol sizing toward a
  target portfolio vol (fractional-Kelly-style cap), per-symbol weight cap,
  and a drawdown circuit breaker with cooldown. A genome renders to a
  self-contained `SignalEngine` via a code template. v2 widens the search:
  - `timeframe` gene selects the bar interval (`1D`/`4H`/`1H`) from whatever
    snapshots exist (`DataConfig.intervals`), so candidates genuinely trade
    different timeframes; the 2-week gate window is converted to bars per
    interval so every timeframe is held to the same calendar-time target.
  - `min_holding_bars`/`max_holding_bars` randomize the holding horizon from
    short scalps to multi-week swings (stops and the circuit breaker always
    fire; only discretionary exits wait out the minimum).
  - `small_trade_mode` structurally favors **dozens of small compounding
    trades**: forces ≥10 concurrent positions with a ≤5% per-symbol cap
    (`RiskConfig.max_weight_small_trade`), `max_positions` bounds widened to
    (2, 30).
  - The `equities_penny` sleeve runs the same engine/friction over a curated
    list of ~40 high-volume low-priced US names (`universe.py::PENNY_LIQUID`).
    True sub-$1 OTC names are deliberately excluded — their spreads can't be
    simulated honestly with free daily data.
- **Diagnostic engine** (`autotrader/diagnostics.py`): losing round trips
  from `trades.csv` are deterministically tagged: `premature_stop` (price
  recovered above entry within 5 bars of the stop-out), `fakeout_entry`
  (spike-bar entry dead within 3 bars), `whipsaw_churn` (≥3 consecutive short
  losers per symbol), `overbought_entry`, `illiquid` (bottom-decile dollar
  volume), `late_exit` (>50% giveback of peak gain), `oversized_loss`. An
  optional cheap-LLM pass writes a per-generation memo.
- **Mutation**: evolutionary loop per sleeve — elites survive; children come
  from tournament selection + uniform crossover + Gaussian mutation.
  `FAILURE_GENE_BIAS` maps each failure mode to the genes it should push and
  in which direction (e.g. dominant `premature_stop` → widen
  `atr_stop_mult`), so 60% of children are *diagnostic-biased* mutations. An
  optional LLM proposes structural variants (validated through the exact same
  simulator; falls back to random genomes offline).
- **Prompt mutation**: the same failure histogram compiles to guidance text
  (`autotrader/prompts/overlays.json`) injected into every TradingAgents
  agent prompt during committee review (see Pillar 3).
- **Winner attribution** (`autotrader/attribution.py`): after each
  generation the top winning symbols of the best performer are deep-dived —
  deterministic context (trade stats, causal price/volume stats, anomaly
  events, PIT-gated news via `company_intel.get_company_brief`) goes to the
  LLM, which attributes each win to a concrete driver (momentum burst, news
  catalyst, sector move, liquidity event…) and judges whether the strategy
  *captured* the driver or got lucky. Captured drivers produce a
  `gene_bias` that `mutate_gene_bias` reinforces in the next generation —
  the search learns from what worked, not only from what failed.
- **After-run check-in** (`autotrader/checkin.py`): after every generation
  (and every daemon cycle) the full results — window stats, failure
  histogram, leaderboard, attribution — are sent to the LLM router
  (Kimi primary, Bedrock fallback). The reply is a persisted memo plus a
  JSON amendment proposal. `validate_amendments` clamps proposals to a hard
  schema: the LLM may tune mutation sigma, population size, universe sizes,
  and gene bounds (only *inside* the shipped `HARD_GENE_SPACE`); it can
  never touch friction floors, risk caps, capital, or the acceptance gate.
  Every check-in is stored under `autotrader/state/checkins/` for audit.
- **Continuous validation & the hard gate**: fitness is scored on TRAIN
  windows only; the gate is checked on the held-out OOS windows every
  generation. Passers become finalists. **Honest-reporting policy:** every
  generation's OOS stats, train/OOS divergence, and failure histogram are
  persisted to the registry regardless of gate outcome, and finalists get a
  full `backtest.validation` pass (walk-forward, Monte Carlo p-value,
  bootstrap CI) via `simulator.rerun_with_validation`. A best-so-far
  leaderboard is always maintained even when nothing passes.

### Pillar 3 — Committee review & artifacts

- **Committee** (`autotrader/committee.py`): finalists (gate passers, else
  top leaderboard entries) are reviewed by
  `TradingAgentsGraph.propagate(ticker, date)` on dates sampled from their
  OOS windows, with `selected_analysts=["market", "news", "fundamentals"]`.
  The `social` analyst is **excluded** — StockTwits/Reddit/Polymarket fetch
  live state and would leak the future into a historical review.
- **Prompt overlay mechanism**: TradingAgents hardcodes prompts inline, but
  every agent appends `get_language_instruction()` to its system message.
  `apply_prompt_overlay` monkeypatches that function in `agent_utils` and in
  each agent module that imported it by name; prompts are assembled inside
  node functions at invocation time, so the overlay takes effect immediately
  and idempotently. Verdicts are stored per (strategy, ticker, date) in the
  registry.
- **Registry** (`autotrader/registry.py`): SQLite at
  `autotrader/state/registry.db` — genome JSON + lineage (parent, origin:
  seed/mutation/biased_mutation/crossover/llm_proposal), per-window results,
  gate status, generation history with failure histograms and LLM memos,
  committee verdicts. `export_json` produces a portable summary.
- **S3 sync** (`autotrader/s3_sync.py`): AWS CLI only. Creates the bucket
  (name from `AUTOTRADER_S3_BUCKET`) if missing, tags the bucket and every
  synced object `Project=Vibe-Trading` (post-sync `put-object-tagging` pass,
  since `s3 sync` has no tagging flag). Syncs `autotrader/state/` and
  promoted `runs/autotrader/final_*` artifacts.

## Pillar 4 — Autonomous daemon & staged live-readiness

- **LLM router** (`autotrader/llm.py`): every outer-loop LLM touchpoint goes
  through `chat()`, which tries Kimi first (OpenAI-compatible,
  `api.moonshot.ai`) and falls back to AWS Bedrock Claude Opus (`converse`
  API, `~/.aws` credentials) on any failure. Credentials live in
  `~/.config/autotrader/llm.env` (chmod 600, gitignored);
  `AUTOTRADER_LLM_DISABLE=1` (or `--offline`) forces a fully deterministic
  run. Note: this AWS account has Opus 4.6 invoke access (4.8/4.7 not
  granted, verified 2026-07); the model id is a config value.
- **Daemon** (`autotrader/daemon.py`, `./run_pipeline.sh --daemon`): the
  start-it/stop-it entry point. Infinite loop: load central state
  (`autotrader/state/daemon_state.json`) → apply accumulated validated LLM
  amendments → evolve all sleeves → check in with the LLM → merge new
  amendments → paper-validate the champion → regenerate
  `autotrader/state/track_record.md` → save state atomically. SIGINT/SIGTERM
  stop it gracefully; a killed daemon resumes from the saved state. Reaching
  the hard goal does not stop the loop — the daemon keeps re-validating the
  champion and searching for better ones.
- **Paper gate** (`autotrader/paper_trader.py`): a hard-gate finalist is
  re-run on the most recent ~45 days — data the search never optimized on —
  with the same engine and friction. It must clear `PaperConfig`
  (≥5% net, ≤10% drawdown, ≥3 trades) to become live-eligible; verdicts
  persist to `autotrader/state/paper/` and expire after 14 days.
- **Live connector** (`autotrader/live_connector.py`): Alpaca (equities,
  paper endpoint by default) + CCXT (crypto). Four independent interlocks,
  all required per order: hard gate passed, fresh paper pass,
  `AUTOTRADER_LIVE_ENABLE=1` in the environment (a config file can never arm
  it), and per-order notional + daily-loss limits. Every attempt — accepted
  or refused — is appended to `autotrader/state/live_orders.jsonl`.
- **Track-record proof report** (`autotrader/state/track_record.md`,
  regenerated every cycle): hard-goal status, champion's OOS window stats,
  paper-gate results, live interlock status, and the funding-ladder
  readiness line ($200 → $500 → $1k → $2k → $4k → $8k; each rung requires 3
  more consecutive cycles with a fresh paper pass). The report never claims
  returns that were not measured.

## Resource management strategy

- **Local-first compute**: the inner loop is pure pandas/numpy in-process
  (no subprocess per evaluation; artifacts trimmed during sweeps via a
  light-artifacts patch that skips per-symbol OHLCV dumps).
  `--workers N` enables a `ProcessPoolExecutor` that loads the data snapshot
  once per worker and fans evaluations across cores.
- **Network discipline**: one prefetch pass populates the parquet snapshot
  (+ the loaders' own `VIBE_TRADING_DATA_CACHE`); everything after is
  offline. LLM usage is bounded: one memo + up to N proposals per
  generation, committee review only for finalists.
- **Cloud discipline**: S3 sync only, tagged, and entirely optional.

## Honest caveats

- **The 25%/2-week gate is retained as specified but is extraordinarily
  aggressive** (≈ 25,000%+ annualized if compounded). The expected outcome is
  that most or all generations fail the gate; anything that *does* pass on
  historical data carries a very high overfitting risk. That is why fitness
  never sees the OOS windows, the report always shows train/OOS divergence
  and Monte Carlo significance, and the leaderboard keeps honest
  best-so-far results (typical good outcomes are single-digit percent per
  window). Treat gate passers as candidates for *paper* validation, not as
  guaranteed returns.
- Universe realism: "thousands of tickers" is bounded by free-data
  throughput; the default is ~120 US names + 30 crypto pairs + ~40 liquid
  low-priced names, expandable in `universe.py` / `DataConfig`.
- **On the income goal, stated plainly:** the mission is a few thousand
  dollars a month (ideally $10k) from a $200 start on a $2k/month funding
  ladder. The system maximizes the honest search for compounding
  small-trade strategies and produces auditable evidence
  (`track_record.md`), but no simulation can guarantee that outcome — the
  math requires sustained returns far beyond what markets reliably offer.
  The funding ladder is therefore gated on *measured* out-of-sample and
  paper results, never on projections. Fund the next rung only when the
  report shows the evidence.
- Live execution exists but is quadruple-interlocked and defaults to
  paper/off; nothing routes real money until the hard gate, the paper gate,
  and an explicit environment arm switch all agree.

## File map

| Path | Role |
|---|---|
| `autotrader/config.py` | Capital, friction, gate, risk, evolution, data, paper, live, AWS, LLM config |
| `autotrader/bootstrap.py` | sys.path + env bootstrap (`agent/`, run roots, data cache) |
| `autotrader/universe.py` | Curated US-liquid + crypto-top + penny-liquid universes |
| `autotrader/data.py` | One-pass prefetch → parquet snapshot (per interval); offline loading |
| `autotrader/strategy_space.py` | Genome space (incl. timeframe/horizon/small-trade genes), mutation/crossover, SignalEngine renderer |
| `autotrader/simulator.py` | Core simulation engine + window gate scoring + anomaly scan |
| `autotrader/diagnostics.py` | Failure taxonomy from trades.csv (+ LLM memo) |
| `autotrader/optimize_loop.py` | Evolutionary outer loop, CLI entry point |
| `autotrader/llm.py` | Provider router: Kimi primary → Bedrock Opus fallback |
| `autotrader/checkin.py` | After-run LLM check-in + bounded amendment validation |
| `autotrader/attribution.py` | Why-winners-won deep dive → success gene bias |
| `autotrader/company_intel.py` | PIT-gated per-company research briefs |
| `autotrader/daemon.py` | Autonomous start/stop loop, central state, track-record report |
| `autotrader/paper_trader.py` | Recent-data shadow validation gate |
| `autotrader/live_connector.py` | Interlocked Alpaca/CCXT order routing (off by default) |
| `autotrader/research_hooks.py` | PIT-safe news enrichment of anomaly batches |
| `autotrader/committee.py` | TradingAgents finalist review + prompt overlay patching |
| `autotrader/registry.py` | SQLite lineage/results/verdicts store |
| `autotrader/s3_sync.py` | AWS CLI bucket + tagged artifact sync |
| `autotrader/prompts/overlays.json` | Failure-mode → committee guidance text |
| `autotrader/tests/` | Offline test suite (synthetic fixtures, mocked LLM) |
| `run_pipeline.sh` | Entry script: env checks → daemon / prefetch → evolve → committee → S3 |

## Running it

```bash
# one-time setup
python3.11 -m venv .venv
.venv/bin/pip install -r autotrader/requirements.txt

# fast offline verification
./run_pipeline.sh --smoke

# THE autonomous mode: start it, stop it (Ctrl-C) — nothing else.
# Evolves all sleeves, checks in with Kimi/Bedrock every cycle, applies
# validated amendments, paper-validates champions, rewrites the track record.
./run_pipeline.sh --daemon --workers 4

# one full manual run (prefetches real data on first use)
./run_pipeline.sh --sleeves equities,crypto,equities_penny --generations 10 --workers 4

# artifacts
cat autotrader/state/track_record.md          # the funding-decision evidence
cat autotrader/state/last_run_summary.json
ls autotrader/state/checkins/                 # every LLM check-in, audited
sqlite3 autotrader/state/registry.db 'SELECT gid, oos_pass_rate, gate_passed FROM results ORDER BY oos_pass_rate DESC LIMIT 10;'
```
