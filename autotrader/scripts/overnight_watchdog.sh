#!/usr/bin/env bash
# Daytrade-engine health monitor: every 30m verify daemon alive, log progressing, restart if dead.
# (The daytrade search never "finishes" — escalating targets keep it running forever.)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
STATE="$ROOT/autotrader/state"
LOG="$STATE/daemon.log"
MON="$STATE/overnight_monitor.log"
PIDFILE="$STATE/daemon.pid"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
INTERVAL="${WATCHDOG_INTERVAL_SEC:-1800}"  # 30 minutes

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$MON"; }

start_daemon() {
  # Never start a duplicate: another daemon may already be running under a
  # pid we did not record (e.g. started by start_overnight.py).
  if pgrep -f "autotrader.daytrade_daemon" >/dev/null 2>&1; then
    pgrep -f "autotrader.daytrade_daemon" | head -1 > "$PIDFILE"
    log "START: daemon already running (pid=$(cat "$PIDFILE")); adopted"
    return 0
  fi
  log "START: launching daemon (workers=${WORKERS:-4})"
  caffeinate -dims nohup ./run_pipeline.sh --daytrade --workers "${WORKERS:-4}" --cooldown 30 \
    >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  log "START: pid=$(cat "$PIDFILE")"
}

daemon_alive() {
  pgrep -f "autotrader.daytrade_daemon" >/dev/null 2>&1
}

log_progress_ok() {
  # Healthy = the log was written recently. Old tracebacks in the tail are
  # NOT a restart reason while the process is alive and progressing.
  [[ -f "$LOG" ]] || return 1
  local age=$(( $(date +%s) - $(stat -f %m "$LOG" 2>/dev/null || stat -c %Y "$LOG") ))
  if (( age > 7200 )); then
    log "WARN: daemon.log stale ${age}s — process may be hung"
    return 1
  fi
  return 0
}

log "watchdog online (interval=${INTERVAL}s)"
# Grace period: give a freshly launched daemon time to boot before checking.
sleep 60

while true; do
  if ! daemon_alive; then
    log "ALERT: daemon not running — restarting"
    start_daemon
    sleep 60
  elif ! log_progress_ok; then
    log "ALERT: daemon hung (stale log) — restarting"
    pkill -f "autotrader.daytrade_daemon" 2>/dev/null || true
    sleep 10
    pkill -9 -f "autotrader.daytrade_daemon" 2>/dev/null || true
    sleep 5
    start_daemon
  else
    cycle=$(python3 -c "import json; print(json.load(open('$STATE/daytrade_state.json')).get('cycle','?'))" 2>/dev/null || echo "?")
    rung=$(python3 -c "import json; print(json.load(open('$STATE/daytrade_state.json')).get('ladder_rung',0))" 2>/dev/null || echo "?")
    last=$(tail -1 "$LOG" 2>/dev/null | head -c 200)
    log "OK: pid=$(cat "$PIDFILE") cycle=$cycle ladder_rung=$rung | $last"
  fi
  sleep "$INTERVAL"
done
