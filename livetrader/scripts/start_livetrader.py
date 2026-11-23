#!/usr/bin/env python3
"""Launch the livetrader daemon + watchdog fully detached (all-night safe).

    python3 livetrader/scripts/start_livetrader.py            # start
    python3 livetrader/scripts/start_livetrader.py --stop     # stop
    python3 livetrader/scripts/start_livetrader.py --status   # health check

Same double-fork detachment as autotrader/scripts/start_overnight.py, so the
processes survive the launching shell. caffeinate keeps the Mac awake.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "livetrader" / "state"
DAEMON_LOG = STATE / "daemon.log"
DAEMON_PID = STATE / "daemon.pid"
WATCHDOG_PID = STATE / "watchdog.pid"
WATCHDOG_LOG = STATE / "watchdog_monitor.log"
PYTHON = str(ROOT / ".venv" / "bin" / "python")

DAEMON_CMD = ["/usr/bin/caffeinate", "-dims", PYTHON, "-m", "livetrader.daemon"]
WATCHDOG_CMD = ["/bin/bash", str(ROOT / "livetrader" / "scripts" / "livetrader_watchdog.sh")]


def _spawn_detached(cmd: list[str], log_path: Path, pid_path: Path) -> int:
    r, w = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(w)
        gc_pid = int(os.read(r, 32).decode() or "0")
        os.close(r)
        os.waitpid(pid, 0)
        return gc_pid
    os.close(r)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    log = open(log_path, "ab")
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.write(w, str(os.getpid()).encode())
    os.close(w)
    pid_path.write_text(str(os.getpid()))
    os.chdir(ROOT)
    os.execv(cmd[0], cmd)
    os._exit(1)


def _alive(pid_path: Path) -> int | None:
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


def _daemon_running() -> bool:
    return subprocess.run(
        ["pgrep", "-f", "livetrader.daemon"], capture_output=True, check=False
    ).returncode == 0


def start() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    if _alive(DAEMON_PID) or _daemon_running():
        print("livetrader daemon already running")
    else:
        pid = _spawn_detached(DAEMON_CMD, DAEMON_LOG, DAEMON_PID)
        print(f"livetrader daemon started, pid {pid} (caffeinate wrapper)")
        for _ in range(30):
            if _daemon_running():
                break
            time.sleep(1)
        else:
            print("WARNING: daemon process not visible after 30s — check daemon.log")
    if _alive(WATCHDOG_PID):
        print(f"watchdog already running (pid {_alive(WATCHDOG_PID)})")
    else:
        pid = _spawn_detached(WATCHDOG_CMD, WATCHDOG_LOG, WATCHDOG_PID)
        print(f"watchdog started, pid {pid}")


def stop() -> None:
    for name, pid_path in (("watchdog", WATCHDOG_PID), ("daemon", DAEMON_PID)):
        pid = _alive(pid_path)
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
            print(f"{name}: sent SIGTERM to pid {pid}")
        else:
            print(f"{name}: not running")
        pid_path.unlink(missing_ok=True)
    subprocess.run(["pkill", "-f", "livetrader.daemon"], check=False)


def status() -> None:
    for name, pid_path in (("daemon", DAEMON_PID), ("watchdog", WATCHDOG_PID)):
        pid = _alive(pid_path)
        print(f"{name}: {'RUNNING pid ' + str(pid) if pid else 'NOT RUNNING'}")
    st = STATE / "status.json"
    if st.exists():
        s = json.loads(st.read_text())
        arena = s.get("arena", {})
        print(f"ts={s.get('ts')} market_open={s.get('market_open')} "
              f"regime={s.get('regime')} sentiment={s.get('news_sentiment')}")
        print(f"gen={arena.get('generation')} traders={arena.get('traders')} "
              f"agg_ret={arena.get('aggregate_return')} "
              f"trades={arena.get('closed_trades')} evals={arena.get('signal_evaluations')}")
    if DAEMON_LOG.exists():
        age = time.time() - DAEMON_LOG.stat().st_mtime
        print(f"daemon.log last write {age:.0f}s ago")
        print("--- last 3 log lines ---")
        for line in DAEMON_LOG.read_text(errors="replace").splitlines()[-3:]:
            print(line)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--stop":
        stop()
    elif arg == "--status":
        status()
    else:
        start()
