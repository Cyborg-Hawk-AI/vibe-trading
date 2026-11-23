"""Daily P&L reports + the monthly income scoreboard.

Built entirely from the closed-trade journal (``closed_trades.jsonl``), so
every dollar shown traces back to an individual simulated fill with fees,
spread, and slippage charged.

Outputs (all under ``livetrader/state/``):
  - ``daily_pnl/YYYY-MM-DD.md``   one human-readable report per session
  - ``daily_pnl.jsonl``           one aggregate row per day (idempotent)
  - ``monthly_scoreboard.md``     MTD $ P&L vs the monthly income goal

The monthly income goal is an aspirational scoreboard target, NOT a
promotion gate — promotion still requires consistent positive days and a
capped drawdown.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter
from zoneinfo import ZoneInfo

from livetrader.config import LiveTraderConfig, state_dir
from livetrader.engine import journal

logger = logging.getLogger("livetrader.reports")

NY = ZoneInfo("America/New_York")


def today_ny() -> str:
    return dt.datetime.now(tz=NY).strftime("%Y-%m-%d")


def _daily_jsonl_path():
    return state_dir() / "daily_pnl.jsonl"


def _load_daily_rows() -> dict[str, dict]:
    """date -> aggregate row, from daily_pnl.jsonl."""
    path = _daily_jsonl_path()
    rows: dict[str, dict] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            rows[row["date"]] = row
        except (json.JSONDecodeError, KeyError):
            continue
    return rows


def _save_daily_rows(rows: dict[str, dict]) -> None:
    path = _daily_jsonl_path()
    tmp = path.with_suffix(".tmp")
    lines = [json.dumps(rows[d], default=str) for d in sorted(rows)]
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(path)


HUNTER_SLEEVE = "sip_hunter"
HUNTER_TRADES_PER_DAY_TARGET = 200      # "hundreds of round trips/day" bar
SCALE_MIN_TRADES = 100                  # readiness: trades in the rolling month
SCALE_MIN_GREEN_DAYS = 10               # readiness: green hunter days in month


def _postmortem_capture(date_ny: str) -> float | None:
    """Yesterday's oracle capture rate from the post-mortem report, if any."""
    try:
        path = (
            state_dir().parents[1] / "autotrader" / "state" / "postmortem"
            / f"{date_ny}.json"
        )
        report = json.loads(path.read_text(encoding="utf-8"))
        return report.get("oracle", {}).get("capture_rate")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def aggregate_hunter_day(trades: list[dict], date_ny: str) -> dict:
    """Hunter-desk aggregates for one session (expectancy is the headline)."""
    day = [t for t in trades
           if t.get("date_ny") == date_ny and t.get("sleeve") == HUNTER_SLEEVE]
    pnls = [float(t.get("realized_pnl", 0.0)) for t in day]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pnl = sum(pnls)
    longs = [t for t in day if str(t.get("side", "long")) != "short"]
    shorts = [t for t in day if str(t.get("side", "long")) == "short"]
    long_pnl = sum(float(t.get("realized_pnl", 0.0)) for t in longs)
    short_pnl = sum(float(t.get("realized_pnl", 0.0)) for t in shorts)

    # Entries-by-source expectancy (parse src= from entry_reason).
    by_src: dict[str, list[float]] = {}
    for t in day:
        reason = str(t.get("entry_reason", ""))
        src = "scan"
        if "src=" in reason:
            src = reason.split("src=", 1)[1].split()[0].split("cat")[0].strip() or "scan"
        by_src.setdefault(src, []).append(float(t.get("realized_pnl", 0.0)))
    source_expectancy = {
        k: round(sum(v) / len(v), 4) for k, v in by_src.items() if v
    }

    # Edge-model calibration from p= in entry_reason + outcome.
    brier = None
    calib_note = "no edge-model predictions yet"
    pairs: list[tuple[float, int]] = []
    for t in day:
        reason = str(t.get("entry_reason", ""))
        if " p=" not in reason:
            continue
        try:
            p = float(reason.split(" p=", 1)[1].split()[0])
        except (ValueError, IndexError):
            continue
        pairs.append((p, 1 if float(t.get("realized_pnl", 0.0)) > 0 else 0))
    if len(pairs) >= 5:
        brier = round(
            sum((p - y) ** 2 for p, y in pairs) / len(pairs), 4
        )
        calib_note = f"live Brier {brier} over {len(pairs)} predicted trades"

    # Gate-reject counts from setup snapshots.
    gate_rejects: dict[str, int] = {}
    try:
        from livetrader.engine.snapshots import gate_reject_counts

        gate_rejects = gate_reject_counts(date_ny)
    except Exception:  # noqa: BLE001
        pass

    return {
        "trades": len(day),
        "pnl": round(pnl, 4),
        "fees": round(sum(float(t.get("fee", 0.0)) for t in day), 4),
        "wins": len(wins),
        "losses": len(losses),
        "expectancy": round(pnl / len(day), 4) if day else 0.0,
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "long_trades": len(longs),
        "short_trades": len(shorts),
        "long_pnl": round(long_pnl, 4),
        "short_pnl": round(short_pnl, 4),
        "source_expectancy": source_expectancy,
        "brier": brier,
        "calibration_note": calib_note,
        "gate_rejects": gate_rejects,
    }


def aggregate_day(trades: list[dict], date_ny: str) -> dict:
    day = [t for t in trades if t.get("date_ny") == date_ny]
    pnl = sum(float(t.get("realized_pnl", 0.0)) for t in day)
    fees = sum(float(t.get("fee", 0.0)) for t in day)
    wins = sum(1 for t in day if float(t.get("realized_pnl", 0.0)) > 0)
    losses = sum(1 for t in day if float(t.get("realized_pnl", 0.0)) <= 0)
    by_reason = Counter(t.get("exit_reason", "?") for t in day)
    by_symbol: dict[str, float] = {}
    for t in day:
        by_symbol[t["symbol"]] = by_symbol.get(t["symbol"], 0.0) + float(
            t.get("realized_pnl", 0.0)
        )
    return {
        "date": date_ny,
        "realized_pnl": round(pnl, 4),
        "fees": round(fees, 4),
        "trades": len(day),
        "wins": wins,
        "losses": losses,
        "exit_reasons": dict(by_reason),
        "by_symbol": {k: round(v, 4) for k, v in sorted(
            by_symbol.items(), key=lambda kv: kv[1], reverse=True)},
        "hunter": aggregate_hunter_day(trades, date_ny),
    }


def write_daily_report(cfg: LiveTraderConfig, date_ny: str | None = None) -> dict:
    """Aggregate one NY session from the journal; write md + jsonl row."""
    date_ny = date_ny or today_ny()
    trades = journal.load_trades()
    agg = aggregate_day(trades, date_ny)

    # Update the jsonl (idempotent: reruns replace the same date's row).
    rows = _load_daily_rows()
    rows[date_ny] = agg
    _save_daily_rows(rows)

    out_dir = state_dir() / "daily_pnl"
    out_dir.mkdir(parents=True, exist_ok=True)
    day_trades = [t for t in trades if t.get("date_ny") == date_ny]
    lines = [
        f"# Daily P&L — {date_ny} (paper, live prices)",
        "",
        f"_All fills simulated against real quotes with fees/spread/slippage. "
        f"No real orders._",
        "",
        f"- Realized P&L: **${agg['realized_pnl']:+,.2f}** "
        f"({agg['trades']} closed trades, {agg['wins']}W/{agg['losses']}L)",
        f"- Fees paid: ${agg['fees']:,.2f}",
    ]
    if agg["exit_reasons"]:
        reasons = ", ".join(f"{k}: {v}" for k, v in sorted(
            agg["exit_reasons"].items(), key=lambda kv: kv[1], reverse=True))
        lines.append(f"- Exits by reason: {reasons}")
    # Hunter desk: the buzz-driven momentum-scalp cohort, scored on
    # net-$-per-trade expectancy (the number that decides scale-up).
    h = agg.get("hunter", {})
    if h.get("trades"):
        lines += [
            "",
            "## Hunter desk (momentum scalps)",
            f"- Trades: {h['trades']} (target: hundreds/day, "
            f"{HUNTER_TRADES_PER_DAY_TARGET}+) | {h['wins']}W/{h['losses']}L",
            f"- Net P&L: **${h['pnl']:+,.2f}** | expectancy "
            f"**${h['expectancy']:+,.4f}/trade**",
            f"- Avg winner ${h['avg_win']:+,.2f} vs avg loser ${h['avg_loss']:+,.2f} "
            f"| fee drag ${h['fees']:,.2f}",
            f"- Long/short: {h.get('long_trades', 0)}L "
            f"(${h.get('long_pnl', 0):+.2f}) / "
            f"{h.get('short_trades', 0)}S (${h.get('short_pnl', 0):+.2f})",
        ]
        if h.get("source_expectancy"):
            src = ", ".join(
                f"{k}: ${v:+.3f}" for k, v in sorted(h["source_expectancy"].items())
            )
            lines.append(f"- Expectancy by source: {src}")
        if h.get("calibration_note"):
            lines.append(f"- Edge model: {h['calibration_note']}")
        if h.get("gate_rejects"):
            rejects = ", ".join(
                f"{k}: {v}" for k, v in sorted(
                    h["gate_rejects"].items(), key=lambda kv: -kv[1]
                )[:8]
            )
            lines.append(f"- Gate rejects (skipped setups): {rejects}")
        capture = _postmortem_capture(date_ny)
        if capture is not None:
            lines.append(f"- Oracle capture rate (post-mortem): {capture:.1%}")
    top = list(agg["by_symbol"].items())
    if top:
        lines += ["", "## Winners / losers"]
        for sym, pnl in top[:5]:
            lines.append(f"- {sym}: ${pnl:+,.2f}")
        if len(top) > 5:
            for sym, pnl in top[-3:]:
                if (sym, pnl) not in top[:5]:
                    lines.append(f"- {sym}: ${pnl:+,.2f}")
    # Trades with news attribution — the "why it moved" record.
    with_news = [t for t in day_trades if t.get("headlines")]
    if with_news:
        lines += ["", "## Trades with news attribution"]
        for t in with_news[:10]:
            lines.append(
                f"- {t['symbol']} {t.get('exit_reason', '?')} "
                f"pnl ${float(t.get('realized_pnl', 0.0)):+,.2f}"
            )
            for h in t["headlines"][:2]:
                lines.append(f"  - \"{h['title']}\" ({h['source']}, {h['sentiment']:+.2f})")
    (out_dir / f"{date_ny}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return agg


def write_monthly_scoreboard(cfg: LiveTraderConfig) -> dict:
    """Rebuild monthly_scoreboard.md from the daily rows."""
    rows = _load_daily_rows()
    month = dt.datetime.now(tz=NY).strftime("%Y-%m")
    mtd = [rows[d] for d in sorted(rows) if d.startswith(month)]
    mtd_pnl = sum(r["realized_pnl"] for r in mtd)
    mtd_trades = sum(r["trades"] for r in mtd)
    win_days = sum(1 for r in mtd if r["realized_pnl"] > 0)
    lose_days = sum(1 for r in mtd if r["realized_pnl"] < 0)
    worst_day = min((r["realized_pnl"] for r in mtd), default=0.0)
    best_day = max((r["realized_pnl"] for r in mtd), default=0.0)
    goal = cfg.arena.monthly_income_goal_usd
    hunter_traders = int(getattr(cfg.arena, "hunter_traders", 0))
    hunter_cash = float(getattr(cfg.arena, "hunter_cash", 0.0))
    total_start = (
        cfg.sim.starting_cash * cfg.arena.n_traders + hunter_cash * hunter_traders
    )
    equiv_1k = (mtd_pnl / total_start * 1000.0) if total_start else 0.0

    # Hunter desk month-to-date (built from the per-day hunter aggregates).
    h_rows = [r.get("hunter", {}) for r in mtd if r.get("hunter")]
    h_pnl = sum(r.get("pnl", 0.0) for r in h_rows)
    h_trades = sum(r.get("trades", 0) for r in h_rows)
    h_trade_days = sum(1 for r in h_rows if r.get("trades", 0) > 0)
    h_green = sum(1 for r in h_rows if r.get("trades", 0) > 0 and r.get("pnl", 0.0) > 0)
    h_expectancy = h_pnl / h_trades if h_trades else 0.0
    ready_to_scale = (
        h_trades >= SCALE_MIN_TRADES
        and h_expectancy > 0
        and h_green >= SCALE_MIN_GREEN_DAYS
    )

    lines = [
        "# Monthly income scoreboard (paper)",
        "",
        f"_Generated {dt.datetime.now(tz=NY):%Y-%m-%d %H:%M} ET. "
        "Aspirational scoreboard — NOT a promotion gate. Promotion still "
        "requires consistent positive days + drawdown <= "
        f"{cfg.arena.max_drawdown:.0%}._",
        "",
        f"## {month}",
        "",
        f"- MTD realized P&L: **${mtd_pnl:+,.2f}** vs goal ${goal:,.0f}/month "
        f"({(mtd_pnl / goal):+.1%} of goal)" if goal else
        f"- MTD realized P&L: **${mtd_pnl:+,.2f}**",
        f"- Trading days: {len(mtd)} ({win_days} green / {lose_days} red)",
        f"- Closed trades: {mtd_trades}",
        f"- Best day ${best_day:+,.2f} | worst day ${worst_day:+,.2f}",
        f"- Arena capital: ${total_start:,.0f} across "
        f"{cfg.arena.n_traders + hunter_traders} books",
        f"- If one book started at $1,000: MTD ≈ **${equiv_1k:+,.2f}**",
        "",
        "## Hunter desk (momentum scalps, ${:.0f} books)".format(hunter_cash or 100.0),
        "",
        f"- MTD hunter P&L: **${h_pnl:+,.2f}** over {h_trades} trades "
        f"({h_trade_days} trade-days)",
        f"- Expectancy: **${h_expectancy:+,.4f}/trade** "
        f"(scale bar: positive over >= {SCALE_MIN_TRADES} trades)",
        f"- Green hunter days: {h_green} vs >= {SCALE_MIN_GREEN_DAYS} target",
        f"- Avg trades/trade-day: "
        f"{(h_trades / h_trade_days):.0f}" if h_trade_days else
        "- Avg trades/trade-day: 0",
        (
            "- **READY TO SCALE**: expectancy proven — bump "
            f"`arena.hunter_cash` {hunter_cash:.0f} -> {hunter_cash * 10:.0f} "
            "(paper) to 10x the $ per trade."
            if ready_to_scale else
            "- Not ready to scale yet (needs positive expectancy over "
            f">= {SCALE_MIN_TRADES} trades AND >= {SCALE_MIN_GREEN_DAYS} green days)."
        ),
        "",
        "## Daily log",
        "",
        "| date | pnl $ | trades | W/L | fees $ |",
        "|---|---|---|---|---|",
    ]
    for r in mtd:
        lines.append(
            f"| {r['date']} | {r['realized_pnl']:+,.2f} | {r['trades']} "
            f"| {r['wins']}/{r['losses']} | {r['fees']:,.2f} |"
        )
    (state_dir() / "monthly_scoreboard.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8",
    )
    return {
        "month": month, "mtd_pnl": round(mtd_pnl, 2), "days": len(mtd),
        "win_days": win_days, "lose_days": lose_days,
        "equiv_1k": round(equiv_1k, 2),
        "hunter_pnl": round(h_pnl, 2), "hunter_trades": h_trades,
        "hunter_green_days": h_green,
        "hunter_expectancy": round(h_expectancy, 4),
        "ready_to_scale": ready_to_scale,
    }
