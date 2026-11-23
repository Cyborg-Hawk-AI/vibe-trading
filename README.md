<p align="center">
  <img src="assets/icon.png" width="120" alt="Vibe-Trading Logo"/>
</p>

<h1 align="center">Vibe-Trading — Autonomous AI Trading Research Platform</h1>

<p align="center">
  <b>A self-improving trading system: evolve strategies with a genetic algorithm, gate them on out-of-sample evidence, size them with a calibrated edge model, and push the search to AWS spot bursts and quantum kernels.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/LLM-Kimi%20K3-6E44FF?style=flat" alt="Kimi K3">
  <img src="https://img.shields.io/badge/AWS-Braket%20%2B%20EC2%20Spot-FF9900?style=flat&logo=amazonaws&logoColor=white" alt="AWS">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow?style=flat" alt="License"></a>
</p>

> **Fork note:** This repository extends the upstream [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) agent framework with a complete **autonomous research-and-trading stack** (`autotrader/`, `livetrader/`, `autotrader/quantum/`). The original upstream README is preserved in [`README_UPSTREAM.md`](README_UPSTREAM.md). Everything below documents the autonomous stack.

---

## ⚠️ Honest expectations (read this first)

This system is built on evidence, not hype. What it is and is not:

- **It is** a rigorous research loop that evolves intraday strategies, validates them out-of-sample, and only "promotes" ones that pass a strict statistical gate. It learns and self-improves every night.
- **It is not** a money printer. After evolving **61,000+ strategies**, the best *robust, out-of-sample* edge found so far is roughly **+0.15%/day** on the equities sleeve. That compounds, but it will not turn $1,000 into $8,000 in a month. Claims online of "thousands per day from a few hundred dollars" are marketing and fraud — the academic literature ([Barber & Odean](https://faculty.haas.berkeley.edu/odean/papers/day%20traders/daytrader.pdf)) shows **<1% of day traders** are predictably profitable, and the best quant funds on Earth return ~10–25%/**year**.
- The system is deliberately designed to **refuse to promote strategies that only look good in backtest**. Zero gate-passers is a *feature*: it means the filter works.

Use it to find and compound a real, measurable edge — not to chase lottery tickets.

---

## 🏗️ Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │            Research Director                 │
                    │  (nightly evidence pack → Kimi K3 directives)│
                    └───────────────┬──────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
┌───────▼────────┐        ┌─────────▼─────────┐       ┌─────────▼────────┐
│  Daytrade       │        │  Livetrader        │       │  AWS Burst        │
│  Daemon (GA)    │        │  Daemon (paper)    │       │  (EC2 spot GA)    │
│  evolve sleeves │        │  real-time sim     │       │  256×32 sweeps    │
└───────┬────────┘        └─────────┬─────────┘       └─────────┬────────┘
        │                           │                           │
        └───────────────┬───────────┴───────────────┬───────────┘
                        │                           │
              ┌─────────▼─────────┐       ┌─────────▼─────────┐
              │  Edge Model        │       │  Quantum (Braket)  │
              │  logistic/MLP/GBM/ │       │  ZZ kernel, QUBO   │
              │  qkernel + Platt   │       │  feature selection │
              └─────────┬─────────┘       └───────────────────┘
                        │
              ┌─────────▼─────────┐
              │  Strict OOS Gate   │  ← min green rate, min mean return,
              │  + Registry (SQLite)│     max drawdown, min # OOS sessions
              └───────────────────┘
```

### Key components

| Path | What it does |
|---|---|
| `autotrader/` | The research brain: GA optimizer, edge model, hypothesis tester, post-mortems, research director, registry |
| `autotrader/optimize_loop.py` | Genetic-algorithm strategy evolution with multiprocess genome evaluation |
| `autotrader/edge_model.py` | Calibrated win-probability model (logistic / MLP / GBM / **quantum kernel**), Platt-scaled, Brier-gated |
| `autotrader/simulator.py` | Session replay engine + the **out-of-sample promotion gate** |
| `autotrader/research_director.py` | Nightly evidence synthesis → structured directives for the LLM |
| `autotrader/quantum/` | AWS Braket: QUBO feature selection/allocation, quantum fidelity-kernel benchmarks |
| `autotrader/scripts/aws_burst.py` | Orchestrates EC2 spot fleets for massive GA sweeps (budget-guarded) |
| `autotrader/scripts/daily_replay.py` | Replays current champions on the latest tape → daily hypothetical P&L tracker |
| `livetrader/` | Real-time paper-trading arena (equities + crypto feeds, hunter scalping path) |
| `agent/` | Upstream Vibe-Trading agent framework (backtest engines, connectors, API) |

---

## 🚀 Quick start

### 1. Install

```bash
git clone <your-repo-url>
cd Vibe-Trading
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r autotrader/requirements.txt
pip install amazon-braket-sdk  # optional, for quantum jobs
```

### 2. Configure the LLM (Kimi K3)

The system uses **Kimi K3 only** (Moonshot's OpenAI-compatible API). Set your key:

```bash
mkdir -p ~/.config/autotrader
cat > ~/.config/autotrader/llm.env <<'EOF'
MOONSHOT_API_KEY=sk-your-moonshot-key
AUTOTRADER_LLM_MODEL=kimi-k3
EOF
```

AWS features (spot bursts, Braket) need `~/.aws/credentials` configured with EC2/S3/Braket permissions.

### 3. Run the autonomous loop

```bash
# Start the overnight GA + research daemon (evolves strategies, gates, learns)
./run_pipeline.sh

# Or run components individually:
python -m autotrader.daytrade_daemon      # GA evolution + nightly research
python -m autotrader.scripts.daily_replay # replay champions on latest tape
python -m autotrader.scripts.weekly_scorecard  # generate the learning-loop report
```

### 4. Launch a large AWS spot burst (optional, costs a few $)

```bash
python -m autotrader.scripts.aws_burst run \
  --instance-type c7i.24xlarge --population 256 --generations 32 --workers 48 --wait
```

Results auto-merge into `autotrader/state/daytrade_registry.db` when the instance finishes and self-terminates.

---

## 🔬 How to refine it (the part that actually matters)

The system is a *loop*, and each stage is a lever you can tune. Refinement = adjusting these levers based on evidence.

### 1. The promotion gate — `DaytradeGateConfig` in `autotrader/config.py`

This is the filter between "backtest winner" and "trusted strategy." Current defaults:

```python
min_green_rate: 0.55        # ≥55% of OOS sessions must be net positive
min_mean_daily_ret: 0.003   # ≥+0.3% mean OOS daily return
session_max_drawdown: 0.05  # ≤5% intraday drawdown
min_oos_sessions: 8         # ≥8 held-out sessions required
```

- **Tighten** (raise `min_mean_daily_ret`, lower `session_max_drawdown`) → fewer, higher-conviction promotions.
- **Loosen** while exploring → more candidates pass, but expect more false positives.
- The 61K-strategy burst found **zero** passers — the bar is honest. Don't loosen it just to see green.

### 2. The genetic algorithm — `apply_daytrade_profile` in `autotrader/daytrade_daemon.py`

```python
population_size: 96    # genomes per generation (AWS burst uses 256)
generations: 20        # evolutionary rounds (AWS burst uses 32)
workers: 7             # parallel genome evaluators
```

- Larger population × generations = broader search, more compute. The AWS burst exists for exactly this.
- GA populations are **seeded from the registry leaderboard**, so each night builds on prior winners rather than starting from scratch.

### 3. The edge model — `autotrader/edge_model.py`

- `FEATURE_KEYS` defines what the model sees (gap, RVOL, buzz, VWAP extension, archetype one-hots, buzz lead-lag). **Add features here** to give the model new information.
- The model-class search (`train_edge_model`) pits logistic / MLP / GBM / **quantum kernel** against each other on held-out Brier score and keeps the winner. A model must beat the base-rate predictor to be marked `usable`.
- Trade brackets (target/stop %) are defined in `livetrader/engine/trader.py` (`_hunter_brackets`). Match these to the regime you want to trade — small scalps vs. larger swings.

### 4. Quantum refinement — `autotrader/quantum/`

- `qubo.py` — QUBO builders for **feature selection** and **capital allocation** (classical simulated annealing + QAOA on SV1).
- `kernel.py` / `feature_map.py` — the quantum fidelity kernel. The numpy-native `feature_map.py` is ~800× faster than the Braket local simulator and bit-exact, making the quantum kernel usable in the nightly model search.
- `runner.py` — drains the Braket task queue with cost gates (`drain_queue()` runs weekly).

### 5. Hunter discipline gates — `autotrader/gate_ab.py`

Offline A/B tests of entry filters (buzz threshold, VWAP extension, time-of-day, pile-on caps, EV floor) against the labeled setup library. Tune entries with evidence instead of intuition:

```bash
python -m autotrader.gate_ab   # grid-search gate configs, stage proposals
```

### 6. Hypothesis testing — `autotrader/hypotheses.py`

The LLM proposes hypotheses (≤10/cycle); each is tested against labeled data with a **Bonferroni-corrected** significance bar. Survivals feed back into the strategy. This is how the system turns observations into durable rules without overfitting to noise.

---

## 📊 Monitoring progress

| Artifact | What it shows |
|---|---|
| `autotrader/state/daytrade_registry.db` | Full GA results ledger (every genome, OOS metrics, gate verdicts) |
| `autotrader/state/daily_replay.md` | Champions replayed on latest tape — cumulative hypothetical P&L vs. target |
| `autotrader/state/edge_model.json` | Current edge model: winner class, Brier, calibration bins, usability |
| Weekly scorecard | End-to-end learning-loop summary (`python -m autotrader.scripts.weekly_scorecard`) |

Query the registry directly:

```bash
sqlite3 -header -column autotrader/state/daytrade_registry.db \
  "SELECT sleeve, COUNT(*) n, SUM(gate_passed) passed,
   ROUND(MAX(oos_mean_window_ret)*100,3) best_oos_pct
   FROM results GROUP BY sleeve;"
```

---

## 🧭 Realistic roadmap

1. **Prove a small positive edge** on the replay tracker over 2–4 weeks of forward data.
2. **Compound** it — reinvest, and add capital steadily rather than sizing up on hope.
3. **Layer in market-neutral yield** (e.g. crypto funding-rate arbitrage) as a base return once capital supports it.
4. **Scale frequency** — as of **June 4, 2026 the FINRA $25K pattern-day-trader rule is eliminated** ([SEC Release 34-105226](https://www.sec.gov/files/rules/sro/finra/2026/34-105226.pdf)), so accounts with >$2,000 in a margin account can day trade without frequency restrictions (broker phase-in through Oct 2027). This removed the main regulatory ceiling on trade count.

---

## 📄 License & disclaimer

MIT (see `LICENSE`). This is a research platform for education and experimentation. **Nothing here is financial advice.** Trading involves substantial risk of loss; past backtested performance does not guarantee future results. Never trade money you cannot afford to lose.
