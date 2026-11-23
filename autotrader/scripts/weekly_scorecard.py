"""Weekly scorecard: one markdown summary of the whole learning loop.

Pulls registry aggregates, edge-model quality, gate A/B, postmortem oracle
gap, quantum results, and live P&L into ``state/scorecards/YYYY-Www.md``.

    python -m autotrader.scripts.weekly_scorecard
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from autotrader.bootstrap import state_dir

logger = logging.getLogger("autotrader.scorecard")
ROOT = Path(__file__).resolve().parents[2]


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _registry_section() -> list[str]:
    from autotrader.registry import Registry

    lines = ["## Strategy registry", ""]
    try:
        reg = Registry(state_dir() / "daytrade_registry.db")
        for sleeve in ("equities", "crypto", "equities_penny"):
            board = reg.leaderboard(sleeve, limit=1)
            if not board:
                lines.append(f"- **{sleeve}**: no results yet")
                continue
            b = board[0]
            lines.append(
                f"- **{sleeve}**: best OOS pass {b.get('oos_pass_rate', 0):.0%}, "
                f"mean {b.get('oos_mean_window_ret', 0):+.3%}/session, "
                f"gate {'PASSED' if b.get('gate_passed') else 'open'} "
                f"(`{str(b.get('gid'))[:8]}`)"
            )
        reg.close()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"- registry unavailable: {exc}")
    return lines


def _edge_section() -> list[str]:
    art = _load_json(state_dir() / "edge_model.json") or {}
    if not art:
        return ["## Edge model", "", "- no artifact"]
    return [
        "## Edge model", "",
        f"- class `{art.get('model_class')}`, usable: **{art.get('usable')}**, "
        f"Brier {art.get('brier_val')} vs base {art.get('brier_base')}",
        f"- features: {len(art.get('feature_keys', []))} "
        f"(archetype one-hots + buzz lead-lag included)",
    ]


def _gate_ab_section() -> list[str]:
    d = state_dir() / "gate_ab"
    files = sorted(d.glob("*.json")) if d.exists() else []
    if not files:
        return ["## Gate A/B", "", "- no runs yet"]
    res = _load_json(files[-1]) or {}
    lines = [
        "## Gate A/B", "",
        f"- {res.get('n_setups', 0)} labeled setups, base hit rate "
        f"{res.get('base_rate', 0):.1%}",
    ]
    if res.get("winner"):
        lines.append(f"- winner: **{res['winner']}** {res.get('winner_stats')}")
    else:
        lines.append("- no gate config cleared the evidence bar this run")
    if res.get("actually_taken"):
        t = res["actually_taken"]
        lines.append(f"- hunter actually took {t['n']} setups, hit {t['hit_rate']:.1%}")
    return lines


def _quantum_section() -> list[str]:
    d = state_dir() / "quantum" / "results"
    files = sorted(
        f for f in d.glob("*.json") if not f.name.startswith("._")
    ) if d.exists() else []
    lines = ["## Quantum (Braket)", ""]
    if not files:
        return lines + ["- no quantum results yet"]
    for f in files[-4:]:
        res = _load_json(f) or {}
        t = res.get("type", f.stem)
        if t == "kernel_benchmark":
            lines.append(
                f"- kernel bench ({f.stem[:10]}): quantum {res.get('quantum_brier')} "
                f"vs rbf {res.get('rbf_brier')} vs linear {res.get('linear_brier')} "
                f"→ **{res.get('winner')}**"
            )
        elif t == "qubo_allocation":
            alloc = (res.get("classical") or {}).get("allocation_usd", {})
            lines.append(f"- allocation ({f.stem[:10]}): {alloc}")
        elif t == "qubo_feature_selection":
            sel = (res.get("classical") or {}).get("selected", [])
            lines.append(f"- feature selection ({f.stem[:10]}): {sel}")
        else:
            lines.append(f"- {t} ({f.stem[:10]})")
    return lines


def _live_pnl_section() -> list[str]:
    path = ROOT / "livetrader" / "state" / "daily_pnl.jsonl"
    lines = ["## Live paper P&L (last 7 days)", ""]
    if not path.exists():
        return lines + ["- no live P&L yet"]
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-7:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        return lines + ["- no live P&L yet"]
    for r in rows:
        pnl = r.get("pnl", r.get("daily_pnl", 0.0))
        lines.append(f"- {r.get('date', '?')}: ${float(pnl):+.2f}")
    total = sum(float(r.get("pnl", r.get("daily_pnl", 0.0))) for r in rows)
    lines.append(f"- **week total: ${total:+.2f}**")
    return lines


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = dt.datetime.now()
    week = today.strftime("%Y-W%V")
    sections = [
        f"# Weekly scorecard — {week} (generated {today:%Y-%m-%d %H:%M})",
        "",
        f"Objective: grow the $1,000 paper book via evidence-gated day trading.",
        "",
    ]
    for fn in (_registry_section, _edge_section, _gate_ab_section,
               _quantum_section, _live_pnl_section):
        try:
            sections.extend(fn())
        except Exception:  # noqa: BLE001 — a section must not kill the card
            logger.exception("scorecard section failed: %s", fn.__name__)
        sections.append("")
    out_dir = state_dir() / "scorecards"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{week}.md"
    path.write_text("\n".join(sections), encoding="utf-8")
    print(path.read_text(encoding="utf-8"))
    logger.info("scorecard written to %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
