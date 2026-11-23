#!/usr/bin/env python3
"""Launch the DAYTRADE replay daemon + watchdog fully detached from the caller.

The overnight compute is repurposed for the intraday (5m/15m) replay engine
(autotrader.daytrade_daemon) — the legacy daily-bar daemon is retired.

Double-forks (setsid) so the processes survive the launching shell/session.
Usage:
    python3 autotrader/scripts/start_overnight.py            # start both
    python3 autotrader/scripts/start_overnight.py --stop     # stop both
    python3 autotrader/scripts/start_overnight.py --status   # health check
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
STATE = ROOT / "autotrader" / "state"
DAEMON_LOG = STATE / "daemon.log"
DAEMON_PID = STATE / "daemon.pid"
WATCHDOG_PID = STATE / "watchdog.pid"
WATCHDOG_LOG = STATE / "overnight_monitor.log"

DAEMON_CMD = [
    "/usr/bin/caffeinate", "-dims",
    str(ROOT / "run_pipeline.sh"), "--daytrade", "--workers", "7", "--cooldown", "30",
]
WATCHDOG_CMD = ["/bin/bash", str(ROOT / "autotrader" / "scripts" / "overnight_watchdog.sh")]


def _spawn_detached(cmd: list[str], log_path: Path, pid_path: Path) -> int:
    """Double-fork so the child survives the launching session."""
    r, w = os.pipe()
    pid = os.fork()
    if pid > 0:  # original process: read grandchild pid back
        os.close(w)
        gc_pid = int(os.read(r, 32).decode() or "0")
        os.close(r)
        os.waitpid(pid, 0)
        return gc_pid
    # child
    os.close(r)
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)
    # grandchild: fully detached
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
    os._exit(1)  # unreachable


def _alive(pid_path: Path) -> int | None:
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


def _daemon_process_running() -> bool:
    return subprocess.run(
        ["pgrep", "-f", "autotrader.daytrade_daemon"], capture_output=True, check=False
    ).returncode == 0


def start() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    if _alive(DAEMON_PID) or _daemon_process_running():
        print("daemon already running")
    else:
        pid = _spawn_detached(DAEMON_CMD, DAEMON_LOG, DAEMON_PID)
        print(f"daemon started, pid {pid} (caffeinate wrapper)")
        # Wait for the python daemon process to actually appear before the
        # watchdog starts, so the watchdog never races into a duplicate spawn.
        for _ in range(30):
            if _daemon_process_running():
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
            # Kill the whole detached session (caffeinate wrapper + python)
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
            print(f"{name}: sent SIGTERM to pid {pid}")
        else:
            print(f"{name}: not running")
        pid_path.unlink(missing_ok=True)
    # Best-effort sweep for stragglers (legacy daily daemon included)
    subprocess.run(["pkill", "-f", "autotrader.daytrade_daemon"], check=False)
    subprocess.run(["pkill", "-f", "autotrader.daemon"], check=False)


def status() -> None:
    for name, pid_path in (("daemon", DAEMON_PID), ("watchdog", WATCHDOG_PID)):
        pid = _alive(pid_path)
        print(f"{name}: {'RUNNING pid ' + str(pid) if pid else 'NOT RUNNING'}")
    ds = STATE / "daytrade_state.json"
    if ds.exists():
        s = json.loads(ds.read_text())
        print(f"cycle={s.get('cycle')} ladder_rung={s.get('ladder_rung')} "
              f"champion={(s.get('champion') or {}).get('gid')} "
              f"simulated_trades={s.get('simulated_trades_total')}")
    if DAEMON_LOG.exists():
        age = time.time() - DAEMON_LOG.stat().st_mtime
        print(f"daemon.log last write {age:.0f}s ago")
        print("--- last 3 log lines ---")
        lines = DAEMON_LOG.read_text(errors="replace").splitlines()
        for line in lines[-3:]:
            print(line)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--stop":
        stop()
    elif arg == "--status":
        status()
    else:
        start()
