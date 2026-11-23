"""Yesterday post-mortem: re-trade the day we just lived through.

Once per trading day (triggered by the daytrade daemon after the ET close)
this module takes the previous session's recorded tape and squeezes every
lesson out of it:

  1. GA replay on ONE day — a genetic pass (population x generations of
     full-friction backtests) restricted to yesterday's session, so tens of
     thousands of simulated trades explore entries/exits on the exact data
     the live hunters just traded. Winning genomes are exported to
     ``postmortem_winners.json`` and seed tomorrow's hunter newborns.
  2. Oracle max-profit analysis — for each symbol, the best achievable set
     of non-overlapping bracket trades (hunter-style: +6.5%/-3.5%/8-bar time
     stop, full friction). ``oracle $ vs hunters' actual $ = capture rate``:
     the daily answer to "how could we have made the most money yesterday?"
  3. Swing attribution — the day's largest 30-minute moves matched against
     the persisted news log and buzz log; every swing gets a probable cause
     or an explicit "no visible catalyst" flag.

Outputs:
  - ``livetrader/state/postmortem/YYYY-MM-DD.md``  (human report)
  - ``autotrader/state/postmortem/YYYY-MM-DD.json`` (structured, mined by
    the lessons ledger and the scoreboard)
  - ``autotrader/state/postmortem_winners.json``    (genome seeds)

The oracle is a hindsight ceiling used ONLY to measure capture rate and
find missed patterns — it is never a return projection.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

from autotrader.bootstrap import ensure_agent_path, state_dir
from autotrader.config import PipelineConfig
from autotrader.data import load_snapshot, snapshot_for_interval
from autotrader.strategy_space import (
    GENE_SPACE,
    _repair,
    crossover,
    genome_id,
    mutate,
    random_genome,
)
from autotrader.validation_ladder import export_gate_passers_for_livetrader

ensure_agent_path()

logger = logging.getLogger("autotrader.postmortem")

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
LIVE_STATE = ROOT / "livetrader" / "state"

SNAPSHOT_5M = snapshot_for_interval("daytrade", "5m")
WINNERS_NAME = "postmortem_winners.json"

# GA pass sizing (one-day replay is cheap per eval; this is ~480 backtests).
POPULATION = 48
GENERATIONS = 10
WARMUP_SESSIONS = 3          # context sessions before the target day
MAX_SYMBOLS = 60             # cap the one-day universe for cycle time

# Oracle bracket (mid-range hunter bracket) + friction per side.
ORACLE_TARGET = 0.065
ORACLE_STOP = 0.035
ORACLE_TIME_BARS = 8         # 8 x 5m = 40 min
FEE_BPS = 10.0
SLIP_BPS = 5.0
SPREAD_TIERS = ((1.0, 30.0), (5.0, 15.0), (20.0, 8.0), (1e12, 3.0))

SWING_WINDOW_BARS = 6        # 30 minutes of 5m bars
SWING_MIN_Z = 2.0
SWING_MATCH_S = 45 * 60.0


def _pm_json_dir() -> Path:
    d = state_dir() / "postmortem"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pm_md_dir() -> Path:
    d = LIVE_STATE / "postmortem"
    d.mkdir(parents=True, exist_ok=True)
    return d


def latest_completed_session(now: dt.datetime | None = None) -> str:
    """Most recent ET equity session that has fully closed."""
    now = now or dt.datetime.now(tz=NY)
    day = now.date()
    # Today counts only after the close settles (16:15 ET).
    if now.weekday() >= 5 or now.time() < dt.time(16, 15):
        day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def _spread_bps(price: float) -> float:
    for ceiling, bps in SPREAD_TIERS:
        if price <= ceiling:
            return bps
    return SPREAD_TIERS[-1][1]


def _round_trip_cost_pct(price: float) -> float:
    """Full friction both ways: half-spread + slippage + fee, per side."""
    per_side = (_spread_bps(price) / 2.0 + SLIP_BPS + FEE_BPS) / 1e4
    return 2.0 * per_side


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _load_day_frames(date: str) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """(ga_map, day_map) of 5m equity frames for the target session.

    ``ga_map`` carries the target day plus ``WARMUP_SESSIONS`` prior sessions
    (indicator warmup); ``day_map`` is the target session only (oracle/swings).
    """
    target = pd.Timestamp(date)
    full = load_snapshot(SNAPSHOT_5M)
    ga_map: dict[str, pd.DataFrame] = {}
    day_map: dict[str, pd.DataFrame] = {}
    for code, df in full.items():
        if not code.endswith(".US") or df is None or df.empty:
            continue
        days = sorted(set(pd.DatetimeIndex(df.index).normalize()))
        if target not in days:
            continue
        pos = days.index(target)
        keep = set(days[max(pos - WARMUP_SESSIONS, 0): pos + 1])
        norm = pd.DatetimeIndex(df.index).normalize()
        ga = df[[d in keep for d in norm]]
        day = df[norm == target]
        if len(day) < 12:   # need at least an hour of the session
            continue
        ga_map[code] = ga
        day_map[code] = day
    # Cap universe size, preferring the most active symbols on the day.
    if len(day_map) > MAX_SYMBOLS:
        ranked = sorted(
            day_map,
            key=lambda c: float((day_map[c]["close"] * day_map[c]["volume"]).sum()),
            reverse=True,
        )[:MAX_SYMBOLS]
        ga_map = {c: ga_map[c] for c in ranked}
        day_map = {c: day_map[c] for c in ranked}
    return ga_map, day_map


# ---------------------------------------------------------------------------
# 1. GA replay on the single day
# ---------------------------------------------------------------------------

def _day_fitness(result: Any, date: str) -> float:
    """Fitness = the target day's net session return (must actually trade)."""
    if not result.ok:
        return -9.0
    day_wins = [w for w in result.windows if w.start == date]
    if not day_wins:
        return -9.0
    ret = float(day_wins[0].ret)
    if result.trade_count <= 0:
        ret -= 0.001   # never reward doing nothing
    return ret


def _ga_replay(
    date: str, ga_map: dict[str, pd.DataFrame], cfg: PipelineConfig,
) -> dict[str, Any]:
    from autotrader.simulator import simulate_genome

    rng = random.Random(hash(date) & 0xFFFF)
    prev_tf = GENE_SPACE.get("timeframe")
    GENE_SPACE["timeframe"] = (4, 4, "i")   # lock the pass to 5m genomes
    total_trades = 0
    evals = 0
    scored: dict[str, tuple[float, dict, Any]] = {}
    try:
        population = [_repair(random_genome(rng)) for _ in range(POPULATION)]
        # Seed with current daytrade gate-passers so the day is also scored
        # against the incumbent DNA.
        try:
            from autotrader.validation_ladder import load_gate_passer_seeds

            for i, row in enumerate(load_gate_passer_seeds(
                limit=POPULATION // 4,
                path=state_dir() / "daytrade_gate_passers_seed.json",
            )):
                g = dict(row.get("genome") or {})
                if g:
                    g["timeframe"] = 4
                    population[i] = _repair(g)
        except Exception:  # noqa: BLE001
            pass

        for gen in range(GENERATIONS):
            fits: list[tuple[float, dict]] = []
            for genome in population:
                gid = genome_id(genome)
                if gid in scored:
                    fits.append((scored[gid][0], genome))
                    continue
                result = simulate_genome(genome, ga_map, cfg, "equities", interval="5m")
                evals += 1
                total_trades += int(result.trade_count or 0)
                fit = _day_fitness(result, date)
                scored[gid] = (fit, genome, result)
                fits.append((fit, genome))
            fits.sort(reverse=True, key=lambda x: x[0])
            elites = [g for _, g in fits[: max(4, POPULATION // 4)]]
            nxt = [dict(g) for g in elites[:4]]   # elitism
            while len(nxt) < POPULATION:
                roll = rng.random()
                if roll < 0.15:
                    child = random_genome(rng)
                elif len(elites) >= 2 and roll < 0.55:
                    child = crossover(rng.choice(elites), rng.choice(elites), rng)
                else:
                    child = mutate(rng.choice(elites), rng)
                nxt.append(_repair(child))
            population = nxt
            logger.info(
                "postmortem %s GA gen %d/%d: best day ret %.2f%% (%d evals, ~%s trades)",
                date, gen + 1, GENERATIONS, fits[0][0] * 100, evals, f"{total_trades:,}",
            )
    finally:
        if prev_tf is not None:
            GENE_SPACE["timeframe"] = prev_tf

    ranked = sorted(scored.values(), reverse=True, key=lambda x: x[0])
    winners = [
        {
            "gid": genome_id(genome),
            "sleeve": "sip_hunter",
            "genome": genome,
            "result": {"oos_pass_rate": 1.0 if fit > 0 else 0.0,
                       "oos_mean_window_ret": round(fit, 5)},
        }
        for fit, genome, _res in ranked[:8] if fit > 0
    ]
    best = ranked[0] if ranked else None
    return {
        "evals": evals,
        "simulated_trades": total_trades,
        "best_day_ret": round(best[0], 5) if best else 0.0,
        "best_gid": genome_id(best[1]) if best else None,
        "best_genome": best[1] if best else None,
        "winners": winners,
    }


# ---------------------------------------------------------------------------
# 2. Oracle max-profit analysis
# ---------------------------------------------------------------------------

def _oracle_symbol(day: pd.DataFrame) -> list[dict[str, Any]]:
    """Best non-overlapping hunter-bracket trades on one symbol's day tape."""
    close = day["close"].to_numpy(dtype=float)
    high = day["high"].to_numpy(dtype=float)
    low = day["low"].to_numpy(dtype=float)
    idx = day.index
    n = len(close)
    trades: list[dict[str, Any]] = []
    for i in range(n - 1):
        entry = close[i]
        if entry <= 0:
            continue
        target = entry * (1.0 + ORACLE_TARGET)
        stop = entry * (1.0 - ORACLE_STOP)
        exit_px = None
        exit_j = None
        for j in range(i + 1, min(i + 1 + ORACLE_TIME_BARS, n)):
            if low[j] <= stop:       # stop first within a bar: conservative
                exit_px, exit_j = stop, j
                break
            if high[j] >= target:
                exit_px, exit_j = target, j
                break
        if exit_px is None:
            exit_j = min(i + ORACLE_TIME_BARS, n - 1)
            exit_px = close[exit_j]
        net_pct = exit_px / entry - 1.0 - _round_trip_cost_pct(entry)
        if net_pct > 0:
            trades.append({
                "entry_bar": i, "exit_bar": int(exit_j),
                "entry_ts": str(idx[i]), "net_pct": round(net_pct, 5),
            })
    # Greedy non-overlapping selection by profit.
    trades.sort(key=lambda t: t["net_pct"], reverse=True)
    chosen: list[dict[str, Any]] = []
    used: set[int] = set()
    for t in trades:
        span = set(range(t["entry_bar"], t["exit_bar"] + 1))
        if span & used:
            continue
        used |= span
        chosen.append(t)
    chosen.sort(key=lambda t: t["entry_bar"])
    return chosen


def _tod_bucket_et(ts_str: str) -> str:
    """Session time-of-day bucket for an entry timestamp string (ET)."""
    try:
        ts = pd.Timestamp(ts_str)
        t = ts.tz_convert(NY).time() if ts.tzinfo is not None else ts.time()
    except Exception:  # noqa: BLE001
        return "unknown"
    if t < dt.time(10, 0):
        return "open"
    if t < dt.time(11, 30):
        return "morning"
    if t < dt.time(13, 30):
        return "midday"
    if t < dt.time(15, 0):
        return "afternoon"
    return "close"


def _price_class(px: float) -> str:
    if px < 5.0:
        return "penny"
    if px < 20.0:
        return "low"
    if px < 100.0:
        return "mid"
    return "large"


def _oracle_report(
    date: str, day_map: dict[str, pd.DataFrame], book_usd: float = 100.0,
) -> dict[str, Any]:
    """Per-symbol oracle $ vs the hunters' actual $ from the live journal.

    Also decomposes the oracle money by time-of-day, mover archetype, and
    price class, and diffs each bucket against what hunters actually
    captured — the ranked "money left on the table" target list.
    """
    notional = 0.9 * book_usd
    per_symbol: list[dict[str, Any]] = []
    trades_detail: list[dict[str, Any]] = []
    for code, day in day_map.items():
        trades = _oracle_symbol(day)
        if not trades:
            continue
        oracle_usd = sum(t["net_pct"] for t in trades) * notional
        per_symbol.append({
            "symbol": code.replace(".US", ""),
            "oracle_usd": round(oracle_usd, 2),
            "oracle_trades": len(trades),
            "first_entry": trades[0]["entry_ts"],
        })
        arch = (
            _classify_archetype(day)
            if code.endswith(".US") and len(day) >= 20 else "crypto"
        )
        px0 = float(day["close"].iloc[0]) if len(day) else 0.0
        for t in trades:
            trades_detail.append({
                "symbol": code.replace(".US", ""),
                "net_usd": t["net_pct"] * notional,
                "tod": _tod_bucket_et(t["entry_ts"]),
                "archetype": arch,
                "price_class": _price_class(px0),
            })
    per_symbol.sort(key=lambda r: r["oracle_usd"], reverse=True)

    # Hunters' actual $ per symbol, from the live journal.
    actual: dict[str, float] = {}
    hunter_trades = 0
    hunter_tod: dict[str, int] = {}
    journal = LIVE_STATE / "closed_trades.jsonl"
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if t.get("sleeve") != "sip_hunter" or t.get("date_ny") != date:
                continue
            hunter_trades += 1
            sym = str(t.get("symbol", ""))
            actual[sym] = actual.get(sym, 0.0) + float(t.get("realized_pnl", 0.0))
            entry_ts = float(t.get("ts", 0.0)) - float(t.get("held_s", 0.0) or 0.0)
            if entry_ts > 0:
                bucket = _tod_bucket_et(
                    dt.datetime.fromtimestamp(entry_ts, tz=NY).isoformat()
                )
                hunter_tod[bucket] = hunter_tod.get(bucket, 0) + 1
    for row in per_symbol:
        row["actual_usd"] = round(actual.get(row["symbol"], 0.0), 2)
    traded_syms = {r["symbol"] for r in per_symbol}
    for sym, pnl in actual.items():
        if sym not in traded_syms:
            per_symbol.append({
                "symbol": sym, "oracle_usd": 0.0, "oracle_trades": 0,
                "first_entry": "", "actual_usd": round(pnl, 2),
            })

    oracle_total = sum(r["oracle_usd"] for r in per_symbol)
    actual_total = sum(actual.values())

    # --- Oracle-gap decomposition -------------------------------------------
    def _bucket(key: str) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for t in trades_detail:
            b = out.setdefault(t[key], {"oracle_usd": 0.0, "oracle_trades": 0})
            b["oracle_usd"] += t["net_usd"]
            b["oracle_trades"] += 1
        return {
            k: {"oracle_usd": round(v["oracle_usd"], 2),
                "oracle_trades": int(v["oracle_trades"])}
            for k, v in sorted(out.items(), key=lambda kv: -kv[1]["oracle_usd"])
        }

    by_tod = _bucket("tod")
    for b in by_tod.values():
        b["hunter_entries"] = 0
    for bucket, n in hunter_tod.items():
        by_tod.setdefault(
            bucket, {"oracle_usd": 0.0, "oracle_trades": 0}
        )["hunter_entries"] = n

    by_arch = _bucket("archetype")
    by_price = _bucket("price_class")
    # Attribute hunter actual $ to archetype/price buckets via symbol.
    sym_arch = {t["symbol"]: t["archetype"] for t in trades_detail}
    sym_price = {t["symbol"]: t["price_class"] for t in trades_detail}
    for sym, pnl in actual.items():
        arch = sym_arch.get(sym)
        if arch:
            by_arch.setdefault(arch, {"oracle_usd": 0.0, "oracle_trades": 0})
            by_arch[arch]["actual_usd"] = round(
                by_arch[arch].get("actual_usd", 0.0) + pnl, 2
            )
        pc = sym_price.get(sym)
        if pc:
            by_price.setdefault(pc, {"oracle_usd": 0.0, "oracle_trades": 0})
            by_price[pc]["actual_usd"] = round(
                by_price[pc].get("actual_usd", 0.0) + pnl, 2
            )

    # Ranked (archetype, tod) combos by uncaptured oracle money.
    combos: dict[tuple[str, str], float] = {}
    for t in trades_detail:
        key = (t["archetype"], t["tod"])
        combos[key] = combos.get(key, 0.0) + t["net_usd"]
    money_left = [
        {"archetype": a, "tod": b, "oracle_usd": round(v, 2)}
        for (a, b), v in sorted(combos.items(), key=lambda kv: -kv[1])[:10]
        if v >= 5.0
    ]

    return {
        "per_symbol": per_symbol[:25],
        "oracle_total_usd": round(oracle_total, 2),
        "actual_total_usd": round(actual_total, 2),
        "hunter_trades": hunter_trades,
        "capture_rate": round(actual_total / oracle_total, 4) if oracle_total > 0 else None,
        "gap": {
            "by_tod": by_tod,
            "by_archetype": by_arch,
            "by_price_class": by_price,
            "money_left_ranked": money_left,
        },
    }


# ---------------------------------------------------------------------------
# 3. Swing attribution
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _swings(
    date: str, day_map: dict[str, pd.DataFrame], top_n_symbols: int = 12,
) -> list[dict[str, Any]]:
    """Largest 30-min moves per symbol, matched to headlines and buzz spikes."""
    headlines = _load_jsonl(LIVE_STATE / "news_log.jsonl")
    buzz_rows = _load_jsonl(LIVE_STATE / "buzz_log.jsonl")

    def _epoch(ts: pd.Timestamp) -> float:
        return ts.tz_localize(NY).timestamp()

    swings: list[dict[str, Any]] = []
    # Rank symbols by dollar volume; always include SPY/QQQ market context.
    ranked = sorted(
        day_map,
        key=lambda c: float((day_map[c]["close"] * day_map[c]["volume"]).sum()),
        reverse=True,
    )[:top_n_symbols]
    for ctx in ("SPY.US", "QQQ.US"):
        if ctx in day_map and ctx not in ranked:
            ranked.append(ctx)

    for code in ranked:
        day = day_map[code]
        close = day["close"].astype(float)
        if len(close) < SWING_WINDOW_BARS + 4:
            continue
        roll_ret = close.pct_change(SWING_WINDOW_BARS).dropna()
        sd = float(roll_ret.std())
        if sd <= 0:
            continue
        z = roll_ret / sd
        picks = z.abs().nlargest(3)
        seen_bars: set[int] = set()
        for ts, zval in zip(picks.index, picks.values):
            bar_i = int(day.index.get_loc(ts))
            if any(abs(bar_i - b) < SWING_WINDOW_BARS for b in seen_bars):
                continue   # de-dupe overlapping windows of the same move
            seen_bars.add(bar_i)
            if abs(zval) < SWING_MIN_Z:
                continue
            move = float(roll_ret.loc[ts])
            epoch = _epoch(ts)
            bare = code.replace(".US", "")
            cause = ""
            # nearest headline mentioning the symbol within the window
            best_dt = SWING_MATCH_S
            for h in headlines:
                h_ts = float(h.get("ts", 0.0))
                gap = abs(h_ts - epoch)
                if gap > best_dt:
                    continue
                title = str(h.get("title", ""))
                syms = h.get("symbols", []) or []
                if bare in syms or f" {bare} " in f" {title.upper()} ":
                    cause = f"news: \"{title[:90]}\" ({h.get('source', '?')})"
                    best_dt = gap
            if not cause:
                for b in buzz_rows:
                    if abs(float(b.get("ts", 0.0)) - epoch) > SWING_MATCH_S:
                        continue
                    for item in b.get("top", []):
                        if item.get("symbol") == bare:
                            srcs = "+".join(item.get("sources", [])) or "social"
                            cause = f"buzz spike ({srcs}, score {item.get('score')})"
                            break
                    if cause:
                        break
            swings.append({
                "symbol": bare,
                "when": str(ts),
                "epoch": epoch,
                "move_30m": round(move, 4),
                "z": round(float(zval), 1),
                "cause": cause or "no visible catalyst",
            })
    swings.sort(key=lambda s: abs(s["move_30m"]), reverse=True)
    return swings[:20]


# ---------------------------------------------------------------------------
# 4. Archetype classifier + buzz lead-lag
# ---------------------------------------------------------------------------

def _classify_archetype(day: pd.DataFrame) -> str:
    """Tag a mover-day from its 5m shape."""
    if day is None or len(day) < 20:
        return "chop"
    close = day["close"].astype(float)
    open_ = day["open"].astype(float) if "open" in day.columns else close
    idx = day.index
    first = float(open_.iloc[0])
    last = float(close.iloc[-1])
    try:
        times = [
            (ts.timetz().replace(tzinfo=None) if hasattr(ts, "timetz") else ts.time())
            for ts in idx
        ]
    except Exception:  # noqa: BLE001
        times = [None] * len(idx)

    def _at(hh: int, mm: int = 0) -> int:
        target = dt.time(hh, mm)
        for i, t in enumerate(times):
            if isinstance(t, dt.time) and t >= target:
                return i
        return len(close) - 1

    i1030, i1300, i1500 = _at(10, 30), _at(13, 0), _at(15, 0)
    ret_open = float(close.iloc[min(i1030, len(close) - 1)] / first - 1.0)
    ret_mid = float(close.iloc[min(i1300, len(close) - 1)] / first - 1.0)
    ret_pm = float(close.iloc[min(i1500, len(close) - 1)] / first - 1.0)
    ret_eod = last / first - 1.0
    if ret_open >= 0.08 and ret_eod >= ret_open * 0.6:
        return "gap-and-go"
    if ret_open >= 0.08 and ret_eod < ret_open * 0.3:
        return "gap-and-fade"
    if ret_mid >= 0.10 and ret_open < 0.05:
        return "midday-pump"
    if ret_pm >= 0.05 and ret_eod < ret_pm * 0.5:
        return "afternoon-fade"
    return "chop"


def _archetypes(date: str, day_map: dict[str, pd.DataFrame]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for code, day in day_map.items():
        if not code.endswith(".US"):
            continue
        close = day["close"].astype(float)
        if len(close) < 20:
            continue
        day_ret = float(close.iloc[-1] / close.iloc[0] - 1.0)
        if abs(day_ret) < 0.05:
            continue
        arch = _classify_archetype(day)
        counts[arch] = counts.get(arch, 0) + 1
        # Did a midday pump fade by the close?
        faded = False
        if arch == "midday-pump" and len(close) > 40:
            mid_i = len(close) // 2
            mid_ret = float(close.iloc[mid_i] / close.iloc[0] - 1.0)
            faded = day_ret < mid_ret * 0.5
        rows.append({
            "symbol": code.replace(".US", ""),
            "archetype": arch,
            "day_ret": round(day_ret, 4),
            "faded": faded,
        })
    rows.sort(key=lambda r: abs(r["day_ret"]), reverse=True)
    pumps = [r for r in rows if r["archetype"] == "midday-pump"]
    fade_rate = (
        sum(1 for r in pumps if r["faded"]) / len(pumps) if pumps else None
    )
    return {
        "counts": counts,
        "movers": rows[:20],
        "midday_pump_fade_rate": round(fade_rate, 3) if fade_rate is not None else None,
        "midday_pump_n": len(pumps),
    }


def _buzz_lead_lag(swings: list[dict]) -> dict[str, Any]:
    """Minutes between earliest buzz/news mention and move start, per source."""
    headlines = _load_jsonl(LIVE_STATE / "news_log.jsonl")
    buzz_rows = _load_jsonl(LIVE_STATE / "buzz_log.jsonl")
    events: list[dict[str, Any]] = []
    by_source: dict[str, list[float]] = {}
    for s in swings:
        move_ts = float(s.get("epoch") or 0.0)
        if not move_ts:
            continue
        bare = s.get("symbol", "")
        earliest = None
        src = "unknown"
        for h in headlines:
            h_ts = float(h.get("ts", 0.0))
            if h_ts <= 0 or h_ts > move_ts:
                continue
            title = str(h.get("title", ""))
            syms = h.get("symbols", []) or []
            if bare in syms or f" {bare} " in f" {title.upper()} ":
                if earliest is None or h_ts < earliest:
                    earliest = h_ts
                    src = f"news:{h.get('source', '?')}"
        for b in buzz_rows:
            b_ts = float(b.get("ts", 0.0))
            if b_ts <= 0 or b_ts > move_ts:
                continue
            for item in b.get("top", []):
                if item.get("symbol") == bare:
                    if earliest is None or b_ts < earliest:
                        earliest = b_ts
                        srcs = item.get("sources") or ["social"]
                        src = f"buzz:{'+'.join(srcs)}"
        if earliest is None:
            continue
        lead_min = (move_ts - earliest) / 60.0
        events.append({
            "symbol": bare, "source": src,
            "lead_min": round(lead_min, 1), "move": s.get("move_30m"),
        })
        by_source.setdefault(src.split(":")[0], []).append(lead_min)
    summary = [
        {
            "source": k, "n": len(v),
            "median_lead_min": round(float(np.median(v)), 1),
        }
        for k, v in by_source.items()
    ]
    return {"events": events[:30], "by_source": summary}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _write_md(date: str, report: dict[str, Any]) -> Path:
    ga = report["ga"]
    oracle = report["oracle"]
    lines = [
        f"# Post-mortem — {date} (yesterday, re-traded)",
        "",
        f"_Generated {dt.datetime.now(tz=NY):%Y-%m-%d %H:%M} ET. The GA re-traded "
        "this exact session with full friction; the oracle is a hindsight ceiling "
        "used only to measure capture rate — never a projection._",
        "",
        "## GA replay (what COULD have been traded)",
        f"- {ga['evals']} genome backtests, ~{ga['simulated_trades']:,} simulated trades on this day",
        f"- Best genome day return: **{ga['best_day_ret']:+.2%}** (`{ga['best_gid']}`)",
        f"- {len(ga['winners'])} winning genomes exported as hunter seeds",
        "",
        "## Oracle vs hunters (max money vs money made)",
        f"- Oracle ceiling (per $100 book, hunter brackets): **${oracle['oracle_total_usd']:,.2f}**",
        f"- Hunters actual: **${oracle['actual_total_usd']:+,.2f}** "
        f"over {oracle['hunter_trades']} trades",
    ]
    if oracle.get("capture_rate") is not None:
        lines.append(f"- Capture rate: **{oracle['capture_rate']:.1%}**")
    lines += ["", "| symbol | oracle $ | actual $ | oracle trades | first entry |",
              "|---|---|---|---|---|"]
    for r in oracle["per_symbol"][:12]:
        lines.append(
            f"| {r['symbol']} | {r['oracle_usd']:,.2f} | {r.get('actual_usd', 0.0):+,.2f} "
            f"| {r['oracle_trades']} | {r['first_entry'][-8:] if r['first_entry'] else '-'} |"
        )
    missed = [r for r in oracle["per_symbol"][:8]
              if r["oracle_usd"] > 5.0 and abs(r.get("actual_usd", 0.0)) < 0.01]
    if missed:
        lines += ["", "## Missed money (oracle top names the hunters never touched)"]
        for r in missed:
            lines.append(f"- {r['symbol']}: oracle ${r['oracle_usd']:,.2f} left on the table")
    gap = oracle.get("gap") or {}
    if gap.get("money_left_ranked"):
        lines += ["", "## Oracle gap — where the money was (attack list)"]
        for row in gap["money_left_ranked"][:6]:
            lines.append(
                f"- {row['archetype']} / {row['tod']}: "
                f"${row['oracle_usd']:,.2f} of oracle money"
            )
        if gap.get("by_tod"):
            lines += ["", "| time bucket | oracle $ | oracle trades | hunter entries |",
                      "|---|---|---|---|"]
            for bucket, b in gap["by_tod"].items():
                lines.append(
                    f"| {bucket} | {b['oracle_usd']:,.2f} | {b['oracle_trades']} "
                    f"| {b.get('hunter_entries', 0)} |"
                )
        if gap.get("by_archetype"):
            lines += ["", "| archetype | oracle $ | hunter actual $ |",
                      "|---|---|---|"]
            for arch, b in gap["by_archetype"].items():
                lines.append(
                    f"| {arch} | {b['oracle_usd']:,.2f} "
                    f"| {b.get('actual_usd', 0.0):+,.2f} |"
                )
    lines += ["", "## Swing attribution (why it moved)"]
    for s in report["swings"][:12]:
        lines.append(
            f"- {s['when'][-8:]} {s['symbol']} {s['move_30m']:+.1%} (30m, z={s['z']}): {s['cause']}"
        )
    if not report["swings"]:
        lines.append("- No 2-sigma 30-minute swings recorded.")
    arch = report.get("archetypes") or {}
    if arch.get("counts"):
        lines += ["", "## Mover archetypes"]
        for k, v in sorted(arch["counts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"- {k}: {v}")
        if arch.get("midday_pump_fade_rate") is not None:
            lines.append(
                f"- Midday-pump fade rate: **{arch['midday_pump_fade_rate']:.0%}** "
                f"of {arch.get('midday_pump_n', 0)} cases "
                "(the RNG/WKC trap, quantified)"
            )
    lead = report.get("buzz_lead_lag") or {}
    if lead.get("by_source"):
        lines += ["", "## Buzz lead-lag (minutes before move)"]
        for row in lead["by_source"]:
            lines.append(
                f"- {row['source']}: median {row['median_lead_min']:.0f}m "
                f"lead (n={row['n']})"
            )
    path = _pm_md_dir() / f"{date}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_postmortem(
    date: str | None = None,
    cfg: PipelineConfig | None = None,
) -> dict[str, Any] | None:
    """Full post-mortem for one session. Returns the structured report."""
    date = date or latest_completed_session()
    cfg = cfg or PipelineConfig.load(None)
    ga_map, day_map = _load_day_frames(date)
    if not day_map:
        logger.info("post-mortem %s: no 5m session data yet; skipping", date)
        return None
    logger.info("post-mortem %s starting: %d symbols on the day tape", date, len(day_map))

    ga = _ga_replay(date, ga_map, cfg)
    oracle = _oracle_report(date, day_map)
    swings = _swings(date, day_map)
    archetypes = _archetypes(date, day_map)
    lead_lag = _buzz_lead_lag(swings)

    report = {
        "date": date,
        "generated": f"{dt.datetime.now(tz=NY):%Y-%m-%d %H:%M:%S}",
        "symbols": len(day_map),
        "ga": {k: v for k, v in ga.items() if k != "winners"} | {
            "winners": [w["gid"] for w in ga["winners"]],
        },
        "oracle": oracle,
        "swings": swings,
        "archetypes": archetypes,
        "buzz_lead_lag": lead_lag,
    }
    (_pm_json_dir() / f"{date}.json").write_text(
        json.dumps(report, indent=1, default=str), encoding="utf-8",
    )
    md_path = _write_md(date, {**report, "ga": ga})
    if ga["winners"]:
        export_gate_passers_for_livetrader(
            ga["winners"], path=state_dir() / WINNERS_NAME,
        )
    logger.info(
        "post-mortem %s done: oracle $%.2f vs actual $%+.2f, %d swings attributed, "
        "%d winner genomes exported (%s)",
        date, oracle["oracle_total_usd"], oracle["actual_total_usd"],
        len(swings), len(ga["winners"]), md_path,
    )
    return report


def maybe_run_postmortem(cfg: PipelineConfig | None = None) -> dict[str, Any] | None:
    """Run the post-mortem for the latest completed session if not done yet."""
    date = latest_completed_session()
    if (_pm_json_dir() / f"{date}.json").exists():
        return None
    try:
        return run_postmortem(date, cfg)
    except Exception:  # noqa: BLE001 — the daemon loop must survive
        logger.exception("post-mortem %s failed", date)
        return None


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    target = sys.argv[1] if len(sys.argv) > 1 else None
    out = run_postmortem(target)
    print(json.dumps(out, indent=1, default=str) if out else "no data for session")
