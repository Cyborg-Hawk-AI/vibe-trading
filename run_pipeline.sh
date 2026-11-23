#!/usr/bin/env bash
# Autotrader pipeline entry point.
#
# Usage:
#   ./run_pipeline.sh --daemon                 # fully autonomous loop (start it, stop it — nothing else)
#   ./run_pipeline.sh                          # one full run: prefetch + evolve all sleeves
#   ./run_pipeline.sh --sleeves equities       # one sleeve
#   ./run_pipeline.sh --smoke                  # fast offline smoke on a synthetic fixture
#   ./run_pipeline.sh --generations 20 --population 32 --workers 4
#
# LLM credentials live in ~/.config/autotrader/llm.env (Kimi K3 only, per
# user directive 2026-08-31). No key anywhere = fully deterministic run.
#
# Environment (all optional):
#   AUTOTRADER_LLM_DISABLE=1   never contact any LLM provider
#   AUTOTRADER_S3_BUCKET       enable S3 artifact sync
#   AUTOTRADER_LIVE_ENABLE=1   arm live order routing (also needs gate + paper pass)

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
    PYTHON=$(command -v python3.13 || command -v python3.11 || command -v python3)
    echo "[run_pipeline] .venv not found, using $PYTHON"
fi

# --- dependency check -------------------------------------------------------
if ! "$PYTHON" -c "import pandas, numpy, pydantic, yfinance, pyarrow" 2>/dev/null; then
    echo "[run_pipeline] Missing Python deps. Install with:"
    echo "  python3.11 -m venv .venv && .venv/bin/pip install -r autotrader/requirements.txt"
    exit 1
fi

export VIBE_TRADING_DATA_CACHE=1
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)"

# --- LLM credentials (Kimi K3 only) ---------------------------------------
LLM_ENV="$HOME/.config/autotrader/llm.env"
if [ -f "$LLM_ENV" ]; then
    set -a; . "$LLM_ENV"; set +a
fi

# --- status report ----------------------------------------------------------
if [ "${AUTOTRADER_LLM_DISABLE:-}" = "1" ]; then
    echo "[run_pipeline] AUTOTRADER_LLM_DISABLE=1: fully deterministic run"
elif [ -n "${AUTOTRADER_LLM_API_KEY:-}" ] || [ -f "$HOME/.aws/credentials" ]; then
    echo "[run_pipeline] LLM router ready (Kimi K3 only): check-ins, attribution, committee enabled"
else
    echo "[run_pipeline] No LLM credentials: running fully deterministic (this is fine)"
fi
if [ -n "${AUTOTRADER_S3_BUCKET:-}" ]; then
    echo "[run_pipeline] S3 sync enabled -> s3://${AUTOTRADER_S3_BUCKET} (tag Project=Vibe-Trading)"
else
    echo "[run_pipeline] AUTOTRADER_S3_BUCKET not set: artifacts stay local"
fi

# --- smoke mode --------------------------------------------------------------
if [ "${1:-}" = "--smoke" ]; then
    echo "[run_pipeline] Running offline smoke test suite..."
    exec "$PYTHON" -m pytest autotrader/tests -x -q
fi

# --- daemon mode: fully autonomous, Ctrl-C to stop ---------------------------
if [ "${1:-}" = "--daemon" ]; then
    shift
    echo "[run_pipeline] Starting autonomous daemon (Ctrl-C to stop; state in autotrader/state/)..."
    exec "$PYTHON" -m autotrader.daemon "$@"
fi

# --- daytrade mode: intraday (5m/15m) replay research daemon ------------------
if [ "${1:-}" = "--daytrade" ]; then
    shift
    echo "[run_pipeline] Starting daytrade replay daemon (state in autotrader/state/daytrade_*)..."
    exec "$PYTHON" -m autotrader.daytrade_daemon "$@"
fi

# --- single full pipeline run -------------------------------------------------
echo "[run_pipeline] Starting optimize loop (prefetching data snapshot first)..."
"$PYTHON" -m autotrader.optimize_loop --prefetch "$@"

# --- committee review of finalists (best-effort, any LLM provider) ------------
if [ "${AUTOTRADER_LLM_DISABLE:-}" != "1" ]; then
    "$PYTHON" -m autotrader.committee --sleeve equities --top 1 || true
    "$PYTHON" -m autotrader.committee --sleeve crypto --top 1 || true
fi

# --- S3 sync -------------------------------------------------------------------
"$PYTHON" -m autotrader.s3_sync || true

echo "[run_pipeline] Done. See autotrader/state/last_run_summary.json and autotrader/state/track_record.md"
