"""Wipe learning state and restart research from cycle 0.

    python -m autotrader.reset_clean_slate

Stops are the caller's responsibility (stop overnight + livetrader first).
Preserves OHLCV snapshots under autotrader/state/data/daemon/ (after dropping
misaligned TON-USDT if present). Deletes registry, champions, paper verdicts,
arena state, checkins, and gate-passer seeds so search starts with fresh DNA.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from autotrader.bootstrap import state_dir
from autotrader.daemon import save_state
from autotrader.strategy_space import reset_gene_bounds


def _rm(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        # macOS AppleDouble leftovers can survive ignore_errors; force leftovers.
        if path.exists():
            for p in sorted(path.rglob("*"), reverse=True):
                try:
                    if p.is_file() or p.is_symlink():
                        p.unlink(missing_ok=True)
                    elif p.is_dir():
                        p.rmdir()
                except OSError:
                    pass
            try:
                path.rmdir()
            except OSError:
                shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    return True


def _fresh_daemon_state() -> dict:
    return {
        "cycle": 0,
        "amendments": {},
        "gene_bounds": {},
        "cycle_history": [],
        "champion": None,
        "consecutive_paper_passes": 0,
        "funding_rung": 0,
        "hard_goal_reached": False,
        "clean_slate_note": (
            "Learning state wiped for cycle-0 restart on fixed architecture "
            "(paper slice, 1D normalize, gene repair, penny size=0 fix)."
        ),
    }


def drop_bad_crypto_frames(data_root: Path) -> list[str]:
    """Remove known-bad TON frames that misalign daily calendars."""
    removed: list[str] = []
    for pattern in ("**/TON-USDT.parquet", "**/TON_USDT.parquet"):
        for path in data_root.glob(pattern):
            if path.name.startswith("._"):
                continue
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return removed


def reset_clean_slate(*, truncate_logs: bool = True) -> dict:
    at = state_dir()
    lt = Path(__file__).resolve().parents[1] / "livetrader" / "state"

    removed: list[str] = []
    for rel in (
        "registry.db",
        "registry_export.json",
        "gate_passers_seed.json",
        "track_record.md",
        "last_run_summary.json",
        "live_orders.jsonl",
        "daemon_state.json",
    ):
        p = at / rel
        if _rm(p):
            removed.append(str(p))

    for dirname in ("checkins", "paper"):
        p = at / dirname
        if _rm(p):
            removed.append(str(p))

    ton_removed = drop_bad_crypto_frames(at / "data")
    removed.extend(ton_removed)

    if lt.exists():
        for rel in (
            "arena_state.json",
            "status.json",
            "checkins.jsonl",
            "live_track_record.md",
        ):
            p = lt / rel
            if _rm(p):
                removed.append(str(p))

    if truncate_logs:
        for log in (
            at / "daemon.log",
            at / "overnight_monitor.log",
            lt / "daemon.log",
            lt / "livetrader_monitor.log",
        ):
            if log.exists():
                log.write_text("", encoding="utf-8")
                removed.append(f"truncated:{log}")

    reset_gene_bounds()
    state = _fresh_daemon_state()
    save_state(state)

    # Ensure empty dirs exist for the next cycle.
    (at / "checkins").mkdir(parents=True, exist_ok=True)
    (at / "paper").mkdir(parents=True, exist_ok=True)
    lt.mkdir(parents=True, exist_ok=True)

    return {
        "cycle": 0,
        "removed": removed,
        "ton_frames_dropped": ton_removed,
        "daemon_state": str(at / "daemon_state.json"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wipe autotrader/livetrader learning state")
    parser.add_argument(
        "--keep-logs",
        action="store_true",
        help="Do not truncate daemon logs",
    )
    args = parser.parse_args(argv)
    result = reset_clean_slate(truncate_logs=not args.keep_logs)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
