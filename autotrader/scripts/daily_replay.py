"""Daily replay tracker: what would the current champions have made today?

Replays each sleeve's registry leader on the most recent session in the
5m/15m tape library, books it against the $1,000 paper account (allocation
from the latest quantum capital-allocation run, else equal thirds), and
appends the result to ``state/daily_replay.jsonl``. A running markdown
report tracks the equity curve against the daily-income target.

This is the honest scoreboard: hypothetical fills on real tape, fixed book
(no compounding fiction), per-day rows, cumulative P&L, and a clear read on
how far the current evidence is from the target.

    python -m autotrader.scripts.daily_replay
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from autotrader.bootstrap import state_dir
from autotrader.config import PipelineConfig

logger = logging.getLogger("autotrader.daily_replay")

BOOK_USD = 1000.0
DAILY_TARGET_USD = 300.0
SLEEVES = ("equities", "crypto", "equities_penny")
LOG_NAME = "daily_replay.jsonl"
REPORT_NAME = "daily_replay.md"


def _latest_allocation() -> dict[str, float]:
    """Sleeve -> USD from the newest quantum allocation result."""
    results_dir = state_dir() / "quantum" / "results"
    default = {s: BOOK_USD / len(SLEEVES) for s in SLEEVES}
    if not results_dir.exists():
        return default
    files = sorted(
        f for f in results_dir.glob("*_qubo_allocation.json")
        if not f.name.startswith("._")
    )
    if not files:
        return default
    try:
        res = json.loads(files[-1].read_text(encoding="utf-8"))
        alloc = (res.get("classical") or {}).get("allocation_usd") or {}
        out = dict(default)
        for key, usd in alloc.items():
            sleeve = key.split(":")[0]
            if sleeve in out:
                out[sleeve] = float(usd)
        return out
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def _replay_sleeve(sleeve: str, cfg: PipelineConfig) -> dict[str, Any] | None:
    """Replay the sleeve leader on the tape library; return last-session stats."""
    from autotrader.optimize_loop import _load_interval_snapshots, sleeve_codes
    from autotrader.registry import Registry
    from autotrader.simulator import (
        bars_per_trading_day, genome_interval, simulate_genome,
    )

    registry = Registry(state_dir() / "daytrade_registry.db")
    try:
        board = registry.leaderboard(sleeve, limit=1)
    finally:
        registry.close()
    if not board:
        return None
    champ = board[0]
    genome = champ.get("genome")
    if not genome:
        return None

    codes = sleeve_codes(sleeve, cfg)
    if not codes:
        return None
    data_maps = _load_interval_snapshots("daytrade", codes, cfg)
    if not data_maps:
        return None
    interval = genome_interval(genome, tuple(data_maps))
    data_map = data_maps[interval]
    if interval == "5m":
        tail = 45 * bars_per_trading_day(interval, sleeve)
        data_map = {c: df.tail(tail) for c, df in data_map.items()}
    result = simulate_genome(genome, data_map, cfg, sleeve, interval=interval)
    if not result.ok or not result.windows:
        return {"sleeve": sleeve, "gid": champ["gid"], "ok": False,
                "error": result.error or "no windows"}
    last = result.windows[-1]
    n_sessions = len(result.windows)
    green = sum(1 for w in result.windows if w.ret > 0)
    return {
        "sleeve": sleeve,
        "gid": champ["gid"],
        "ok": True,
        "interval": interval,
        "sessions": n_sessions,
        "green_sessions": green,
        "last_session_ret": float(last.ret),
        "last_session_green": bool(last.ret > 0),
        "mean_session_ret": float(sum(w.ret for w in result.windows) / n_sessions),
        "trade_count": result.trade_count,
    }


def run_replay() -> dict[str, Any]:
    cfg = PipelineConfig()
    cfg.data.intervals = ("15m", "5m")
    cfg.llm.disabled = True
    alloc = _latest_allocation()
    today = dt.datetime.now().strftime("%Y-%m-%d")

    rows: list[dict[str, Any]] = []
    day_pnl = 0.0
    for sleeve in SLEEVES:
        try:
            r = _replay_sleeve(sleeve, cfg)
        except Exception as exc:  # noqa: BLE001 — one sleeve must not stop the card
            logger.exception("replay failed for %s", sleeve)
            r = {"sleeve": sleeve, "ok": False, "error": str(exc)}
        if not r:
            continue
        if r.get("ok"):
            r["book_usd"] = round(alloc.get(sleeve, 0.0), 2)
            r["pnl_usd"] = round(r["last_session_ret"] * r["book_usd"], 2)
            day_pnl += r["pnl_usd"]
        rows.append(r)

    entry = {
        "date": today,
        "rows": rows,
        "day_pnl_usd": round(day_pnl, 2),
        "target_usd": DAILY_TARGET_USD,
        "target_hit": day_pnl >= DAILY_TARGET_USD,
    }
    log_path = state_dir() / LOG_NAME
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    _render(log_path)
    return entry


def _render(log_path: Path) -> None:
    entries: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    recent = entries[-10:]
    cumulative = sum(e.get("day_pnl_usd", 0.0) for e in entries)
    hits = sum(1 for e in entries if e.get("target_hit"))
    lines = [
        "# Daily replay — champion on latest tape",
        "",
        f"Book: ${BOOK_USD:,.0f} (fixed, no compounding fiction) | "
        f"Target: ${DAILY_TARGET_USD:,.0f}/day",
        "",
        f"**Cumulative hypothetical P&L: ${cumulative:+.2f}** over "
        f"{len(entries)} replay(s) | days at/above target: {hits}",
        "",
        "| date | sleeve | session ret | P&L | sessions green |",
        "|---|---|---|---|---|",
    ]
    for e in recent:
        for r in e.get("rows", []):
            if not r.get("ok"):
                lines.append(f"| {e['date']} | {r['sleeve']} | — | — | {r.get('error', '')[:30]} |")
                continue
            lines.append(
                f"| {e['date']} | {r['sleeve']} | {r['last_session_ret']:+.2%} "
                f"| ${r['pnl_usd']:+.2f} | {r['green_sessions']}/{r['sessions']} |"
            )
        lines.append(f"| **{e['date']} total** | | | **${e['day_pnl_usd']:+.2f}** | |")
    (state_dir() / REPORT_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    entry = run_replay()
    print(json.dumps({
        "date": entry["date"],
        "day_pnl_usd": entry["day_pnl_usd"],
        "target_hit": entry["target_hit"],
        "sleeves": {r["sleeve"]: r.get("pnl_usd", r.get("error")) for r in entry["rows"]},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
