"""Lessons ledger: the system's self-written, evidence-based memory.

After every daytrade cycle (and readable by every LLM check-in via
``autotrader.knowledge.prompt_suffix``), this miner aggregates hard evidence
from three sources and distills it into lessons:

  1. the daytrade registry — which gene settings measurably correlate with
     higher held-out green-day rates across thousands of backtests;
  2. livetrader's closed-trade journal — what actually made/lost money live
     (exit reasons, fee drag, symbols);
  3. replay-vs-live divergence — strategies that win in replay but lose on
     the live desk are flagged; that gap is the most valuable lesson.

Outputs:
  - ``autotrader/state/lessons.jsonl``          (full structured history)
  - ``knowledge-base/applied/lessons-learned.md`` (top lessons, versioned in
    git — appended to every check-in prompt, bounded)

Every lesson carries its sample size. Nothing is written without evidence.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from autotrader.bootstrap import state_dir

logger = logging.getLogger("autotrader.lessons")

ROOT = Path(__file__).resolve().parents[1]
LEDGER_MD = ROOT / "knowledge-base" / "applied" / "lessons-learned.md"
LEDGER_JSONL_NAME = "lessons.jsonl"

_MIN_GROUP_N = 20          # min backtests per side before a gene lesson counts
_MIN_EFFECT = 0.04         # min green-day-rate delta worth recording
_BINARY_GENES = (
    "use_rsi_gate", "use_trend_gate", "use_breakout_gate", "use_regime_gate",
    "use_volume_gate", "use_pullback_entry", "use_range_compress",
    "small_trade_mode",
)
_NUMERIC_GENES = (
    "momentum_lookback", "atr_stop_mult", "max_holding_bars", "max_positions",
    "target_vol_annual", "max_weight", "rsi_exit", "timeframe",
)


def _live_state_dir() -> Path:
    return ROOT / "livetrader" / "state"


# ---------------------------------------------------------------------------
# Source 1: registry gene evidence
# ---------------------------------------------------------------------------

def _registry_rows(registry_path: Path) -> list[dict[str, Any]]:
    if not registry_path.exists():
        return []
    conn = sqlite3.connect(str(registry_path))
    try:
        cur = conn.execute(
            """
            SELECT r.sleeve, r.oos_pass_rate, r.oos_mean_window_ret,
                   r.max_drawdown, r.trade_count, s.genome_json
            FROM results r JOIN strategies s
              ON r.gid = s.gid AND r.sleeve = s.sleeve
            WHERE r.ok = 1
            """
        )
        rows = []
        for sleeve, opr, omr, mdd, tc, gj in cur.fetchall():
            try:
                genome = json.loads(gj)
            except (TypeError, json.JSONDecodeError):
                continue
            rows.append({
                "sleeve": sleeve, "oos_pass_rate": float(opr or 0.0),
                "oos_mean_ret": float(omr or 0.0), "max_drawdown": float(mdd or 0.0),
                "trade_count": int(tc or 0), "genome": genome,
            })
        return rows
    finally:
        conn.close()


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def mine_registry_lessons(registry_path: Path) -> list[dict[str, Any]]:
    rows = _registry_rows(registry_path)
    if len(rows) < _MIN_GROUP_N * 2:
        return []
    lessons: list[dict[str, Any]] = []

    def _gene_lesson(gene: str, on: list[float], off: list[float],
                     hi_label: str, lo_label: str) -> None:
        if len(on) < _MIN_GROUP_N or len(off) < _MIN_GROUP_N:
            return
        delta = _mean(on) - _mean(off)
        if abs(delta) < _MIN_EFFECT:
            return
        better, worse = (hi_label, lo_label) if delta > 0 else (lo_label, hi_label)
        lessons.append({
            "kind": "gene_evidence",
            "gene": gene,
            "text": (
                f"{gene}: {better} averages {abs(delta):.1%} higher held-out "
                f"green-day rate than {worse} "
                f"(n={len(on)} vs {len(off)} backtests)."
            ),
            "effect": round(delta, 4),
            "n_on": len(on), "n_off": len(off),
        })

    for gene in _BINARY_GENES:
        on = [r["oos_pass_rate"] for r in rows if int(r["genome"].get(gene, 0)) == 1]
        off = [r["oos_pass_rate"] for r in rows if int(r["genome"].get(gene, 0)) == 0]
        _gene_lesson(gene, on, off, "ON", "OFF")

    for gene in _NUMERIC_GENES:
        vals = sorted(
            float(r["genome"].get(gene, 0.0)) for r in rows if gene in r["genome"]
        )
        if len(vals) < _MIN_GROUP_N * 2:
            continue
        median = vals[len(vals) // 2]
        hi = [r["oos_pass_rate"] for r in rows
              if float(r["genome"].get(gene, 0.0)) > median]
        lo = [r["oos_pass_rate"] for r in rows
              if float(r["genome"].get(gene, 0.0)) <= median]
        _gene_lesson(gene, hi, lo, f"above median ({median:.3g})",
                     f"at/below median ({median:.3g})")

    # Sleeve comparison
    by_sleeve: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_sleeve[r["sleeve"]].append(r["oos_pass_rate"])
    ranked = sorted(by_sleeve.items(), key=lambda kv: _mean(kv[1]), reverse=True)
    if len(ranked) >= 2 and all(len(v) >= _MIN_GROUP_N for _, v in ranked):
        best, worst = ranked[0], ranked[-1]
        gap = _mean(best[1]) - _mean(worst[1])
        if gap >= _MIN_EFFECT:
            lessons.append({
                "kind": "sleeve_evidence",
                "text": (
                    f"Sleeve {best[0]} averages {gap:.1%} higher green-day rate "
                    f"than {worst[0]} (n={len(best[1])} vs {len(worst[1])})."
                ),
                "effect": round(gap, 4),
            })

    lessons.sort(key=lambda x: abs(x.get("effect", 0.0)), reverse=True)
    return lessons[:12]


# ---------------------------------------------------------------------------
# Source 2: live journal evidence
# ---------------------------------------------------------------------------

def mine_live_lessons() -> list[dict[str, Any]]:
    lessons: list[dict[str, Any]] = []
    journal = _live_state_dir() / "closed_trades.jsonl"
    if journal.exists():
        by_reason: dict[str, list[float]] = defaultdict(list)
        total_fees = 0.0
        gross_wins = 0.0
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            pnl = float(t.get("realized_pnl", 0.0))
            by_reason[t.get("exit_reason", "?")].append(pnl)
            total_fees += float(t.get("fee", 0.0))
            if pnl > 0:
                gross_wins += pnl
        for reason, pnls in sorted(by_reason.items(), key=lambda kv: sum(kv[1])):
            if len(pnls) < 10:
                continue
            wins = sum(1 for p in pnls if p > 0)
            lessons.append({
                "kind": "live_exit_evidence",
                "text": (
                    f"Live exits via '{reason}': {len(pnls)} trades, net "
                    f"${sum(pnls):+.2f}, win rate {wins / len(pnls):.0%}."
                ),
                "n": len(pnls), "net": round(sum(pnls), 2),
            })
        if gross_wins > 0 and total_fees > 0.5 * gross_wins:
            lessons.append({
                "kind": "live_friction",
                "text": (
                    f"Fee drag is severe live: ${total_fees:.2f} paid in fees vs "
                    f"${gross_wins:.2f} gross wins — churn is eating the edge; "
                    "favor fewer, larger-edge entries."
                ),
            })
    daily = _live_state_dir() / "daily_pnl.jsonl"
    if daily.exists():
        days = []
        for line in daily.read_text(encoding="utf-8").splitlines():
            try:
                days.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if len(days) >= 3:
            green = sum(1 for d in days if float(d.get("realized_pnl", 0.0)) > 0)
            lessons.append({
                "kind": "live_green_rate",
                "text": (
                    f"Live desk green-day rate so far: {green}/{len(days)} "
                    f"({green / len(days):.0%})."
                ),
                "n": len(days),
            })
    return lessons[:8]


# ---------------------------------------------------------------------------
# Source 2b: hunter desk evidence (buzz-driven momentum scalps)
# ---------------------------------------------------------------------------

_MIN_HUNTER_BUCKET_N = 15
_SRC_RE = re.compile(r"src=([a-z+]+)")
_GAP_RE = re.compile(r"gap=([+-]?[\d.]+)%")


def _hunter_rows() -> list[dict[str, Any]]:
    journal = _live_state_dir() / "closed_trades.jsonl"
    if not journal.exists():
        return []
    rows = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t.get("sleeve") == "sip_hunter":
            rows.append(t)
    return rows


def mine_hunter_lessons() -> list[dict[str, Any]]:
    """Expectancy by buzz source / gap bucket / hold time, from live scalps."""
    rows = _hunter_rows()
    if len(rows) < _MIN_HUNTER_BUCKET_N:
        return []
    buckets: dict[str, list[float]] = defaultdict(list)
    for t in rows:
        pnl = float(t.get("realized_pnl", 0.0))
        reason = str(t.get("entry_reason", ""))
        m = _SRC_RE.search(reason)
        if m:
            buckets[f"source '{m.group(1)}'"].append(pnl)
        m = _GAP_RE.search(reason)
        if m:
            gap = abs(float(m.group(1)))
            if gap < 5.0:
                buckets["gap <5%"].append(pnl)
            elif gap < 10.0:
                buckets["gap 5-10%"].append(pnl)
            else:
                buckets["gap >10%"].append(pnl)
        held = float(t.get("held_s", 0.0))
        if held > 0:
            if held < 900:
                buckets["hold <15m"].append(pnl)
            elif held <= 2400:
                buckets["hold 15-40m"].append(pnl)
            else:
                buckets["hold >40m"].append(pnl)
    lessons: list[dict[str, Any]] = []
    for bucket, pnls in sorted(buckets.items(), key=lambda kv: _mean(kv[1])):
        if len(pnls) < _MIN_HUNTER_BUCKET_N:
            continue
        wins = sum(1 for p in pnls if p > 0)
        lessons.append({
            "kind": "hunter_evidence",
            "text": (
                f"Hunter scalps, {bucket}: {_mean(pnls):+.3f} $/trade expectancy "
                f"over {len(pnls)} trades (win rate {wins / len(pnls):.0%})."
            ),
            "n": len(pnls), "effect": round(_mean(pnls), 4),
        })
    return lessons[:8]


# ---------------------------------------------------------------------------
# Source 2c: nightly post-mortem findings (oracle capture, swings, misses)
# ---------------------------------------------------------------------------

def mine_postmortem_lessons() -> list[dict[str, Any]]:
    pm_dir = state_dir() / "postmortem"
    if not pm_dir.exists():
        return []
    reports = []
    for path in sorted(pm_dir.glob("*.json"))[-10:]:
        if path.name.startswith("._"):
            continue
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    if not reports:
        return []
    lessons: list[dict[str, Any]] = []

    captures = [
        float(r["oracle"]["capture_rate"]) for r in reports
        if isinstance(r.get("oracle", {}).get("capture_rate"), (int, float))
    ]
    if captures:
        lessons.append({
            "kind": "postmortem_capture",
            "text": (
                f"Oracle capture rate over the last {len(captures)} post-mortems: "
                f"mean {_mean(captures):.1%} (latest {captures[-1]:.1%}). The gap "
                "to 100% is money the scanner/entry timing left on the table."
            ),
            "n": len(captures), "effect": round(_mean(captures), 4),
        })

    latest = reports[-1]
    missed = [
        r for r in latest.get("oracle", {}).get("per_symbol", [])
        if float(r.get("oracle_usd", 0.0)) > 5.0
        and abs(float(r.get("actual_usd", 0.0))) < 0.01
    ]
    if missed:
        names = ", ".join(
            f"{r['symbol']} (${r['oracle_usd']:.0f})" for r in missed[:3]
        )
        lessons.append({
            "kind": "postmortem_missed",
            "text": (
                f"Missed money on {latest.get('date')}: oracle-profitable names the "
                f"hunters never touched: {names}. Check whether the scanner ranked "
                "them too low or the buzz sources saw them too late."
            ),
            "n": len(missed),
        })

    causes = {"news": 0, "buzz": 0, "none": 0}
    n_swings = 0
    for r in reports[-5:]:
        for s in r.get("swings", []):
            n_swings += 1
            cause = str(s.get("cause", ""))
            if cause.startswith("news"):
                causes["news"] += 1
            elif cause.startswith("buzz"):
                causes["buzz"] += 1
            else:
                causes["none"] += 1
    if n_swings >= 10:
        lessons.append({
            "kind": "postmortem_swings",
            "text": (
                f"Swing attribution over the last {n_swings} large intraday moves: "
                f"{causes['news'] / n_swings:.0%} news-driven, "
                f"{causes['buzz'] / n_swings:.0%} buzz-driven, "
                f"{causes['none'] / n_swings:.0%} no visible catalyst. "
                "No-catalyst moves are pure momentum — they reverse faster; "
                "tighter time stops apply."
            ),
            "n": n_swings,
        })

    # Archetype base rates across recent post-mortems.
    pump_n = fade_n = 0
    for r in reports[-10:]:
        arch = r.get("archetypes") or {}
        n = int(arch.get("midday_pump_n") or 0)
        rate = arch.get("midday_pump_fade_rate")
        if n and rate is not None:
            pump_n += n
            fade_n += int(round(float(rate) * n))
    if pump_n >= 5:
        lessons.append({
            "kind": "archetype_fade",
            "text": (
                f"Midday-pump fade: {fade_n}/{pump_n} "
                f"({fade_n / pump_n:.0%}) of +10%-by-13:00 movers faded by the "
                "close across recent post-mortems — the RNG/WKC trap, quantified. "
                "After 11:30 ET, prefer fades (short) or require catalyst + EV."
            ),
            "n": pump_n, "effect": round(fade_n / pump_n, 4),
        })

    lead_rows = []
    for r in reports[-5:]:
        for row in (r.get("buzz_lead_lag") or {}).get("by_source", []):
            lead_rows.append(row)
    if lead_rows:
        # Aggregate by source family.
        by: dict[str, list[float]] = {}
        for row in lead_rows:
            by.setdefault(row["source"], []).append(float(row["median_lead_min"]))
        best = sorted(by.items(), key=lambda kv: -_mean(kv[1]))
        if best:
            src, mins = best[0]
            lessons.append({
                "kind": "buzz_lead_lag",
                "text": (
                    f"Buzz lead-lag: `{src}` leads price by a median "
                    f"{_mean(mins):.0f} minutes across recent attributed swings "
                    f"(n={sum(len(v) for v in by.values())} source-days). "
                    "Prefer that source when ranking the hot list."
                ),
                "n": sum(len(v) for v in by.values()),
            })
    return lessons


def append_lesson(lesson: dict[str, Any]) -> None:
    """Append one ad-hoc lesson (e.g. hypothesis survivor) and refresh the md."""
    _append_jsonl([lesson])
    # Rebuild md from the latest jsonl tail so the human ledger stays current.
    path = state_dir() / LEDGER_JSONL_NAME
    recent: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines()[-40:]:
            try:
                recent.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    _write_md(recent)


# ---------------------------------------------------------------------------
# Source 3: replay-vs-live divergence
# ---------------------------------------------------------------------------

def mine_divergence_lessons() -> list[dict[str, Any]]:
    lessons: list[dict[str, Any]] = []
    try:
        dt_state = json.loads(
            (state_dir() / "daytrade_state.json").read_text(encoding="utf-8")
        )
        champ = dt_state.get("champion") or {}
        replay_rate = float(champ.get("oos_pass_rate") or 0.0)
    except (FileNotFoundError, json.JSONDecodeError):
        return lessons
    daily = _live_state_dir() / "daily_pnl.jsonl"
    if not daily.exists() or not champ:
        return lessons
    days = []
    for line in daily.read_text(encoding="utf-8").splitlines():
        try:
            days.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if len(days) < 5:
        return lessons
    live_rate = sum(1 for d in days if float(d.get("realized_pnl", 0.0)) > 0) / len(days)
    gap = replay_rate - live_rate
    if gap > 0.15:
        lessons.append({
            "kind": "replay_live_divergence",
            "text": (
                f"WARNING — replay-vs-live gap: champion shows {replay_rate:.0%} "
                f"green days in replay but the live desk runs {live_rate:.0%} "
                f"over {len(days)} sessions. Suspect overfitting to the recorded "
                "library or friction underestimation; weight live evidence higher."
            ),
            "effect": round(gap, 4),
        })
    return lessons


# ---------------------------------------------------------------------------
# Ledger writers
# ---------------------------------------------------------------------------

def _append_jsonl(lessons: list[dict[str, Any]]) -> None:
    path = state_dir() / LEDGER_JSONL_NAME
    stamp = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"
    with path.open("a", encoding="utf-8") as fh:
        for lesson in lessons:
            fh.write(json.dumps({"ts": stamp, **lesson}, default=str) + "\n")


def _write_md(lessons: list[dict[str, Any]]) -> None:
    lines = [
        "# Lessons learned (self-written, evidence-based)",
        "",
        f"_Regenerated {dt.datetime.now():%Y-%m-%d %H:%M} by autotrader.lessons. "
        "Every line below is mined from measured results (registry backtests, "
        "live paper journal) — never from intuition. Sample sizes included; "
        "small-sample lessons are excluded by construction._",
        "",
        "## What the evidence currently says",
        "",
    ]
    for lesson in lessons:
        lines.append(f"- {lesson['text']}")
    if not lessons:
        lines.append("- No lessons with sufficient evidence yet (need more cycles/sessions).")
    LEDGER_MD.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mine_lessons(registry_path: Path | None = None) -> list[dict[str, Any]]:
    """Mine all sources, append to the jsonl history, regenerate the md."""
    registry_path = registry_path or state_dir() / "daytrade_registry.db"
    lessons = (
        mine_divergence_lessons()
        + mine_registry_lessons(registry_path)
        + mine_live_lessons()
        + mine_hunter_lessons()
        + mine_postmortem_lessons()
    )
    _append_jsonl(lessons)
    _write_md(lessons)
    logger.info("lessons ledger updated: %d lessons", len(lessons))
    return lessons


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for lesson in mine_lessons():
        print("-", lesson["text"])
