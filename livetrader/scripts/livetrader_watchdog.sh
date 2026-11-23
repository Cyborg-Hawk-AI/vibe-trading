#!/usr/bin/env bash
# Watchdog for the livetrader daemon: restart if dead or hung (stale log).
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE="$ROOT/livetrader/state"
LOG="$STATE/daemon.log"
WLOG="$STATE/watchdog_monitor.log"
PIDFILE="$STATE/daemon.pid"
PYTHON="$ROOT/.venv/bin/python"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$WLOG"; }

daemon_alive() {
  pgrep -f "livetrader.daemon" >/dev/null 2>&1
}

log_fresh() {
  # Healthy = daemon.log written within the last 20 minutes (heartbeat is 5m).
  [[ -f "$LOG" ]] || return 1
  local age=$(( $(date +%s) - $(stat -f %m "$LOG" 2>/dev/null || stat -c %Y "$LOG") ))
  (( age < 1200 ))
}

start_daemon() {
  if daemon_alive; then
    pgrep -f "livetrader.daemon" | head -1 > "$PIDFILE"
    log "START: daemon already running (pid=$(cat "$PIDFILE")); adopted"
    return 0
  fi
  log "START: launching livetrader daemon"
  cd "$ROOT"
  /usr/bin/caffeinate -dims "$PYTHON" -m livetrader.daemon >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  log "START: pid=$(cat "$PIDFILE")"
}

log "watchdog online (pid $$)"
sleep 60   # let a freshly started daemon warm up before the first check

while true; do
  if ! daemon_alive; then
    log "ALERT: daemon not running — restarting"
    start_daemon
  elif ! log_fresh; then
    log "ALERT: daemon hung (stale log) — restarting"
    pkill -f "livetrader.daemon" 2>/dev/null || true
    sleep 10
    pkill -9 -f "livetrader.daemon" 2>/dev/null || true
    sleep 5
    start_daemon
  fi
  # keep the machine awake even if the daemon's caffeinate died
  pgrep -x caffeinate >/dev/null 2>&1 || /usr/bin/caffeinate -dims -t 3600 &
  sleep 120
done
