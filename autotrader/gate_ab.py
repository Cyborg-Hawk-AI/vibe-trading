"""Offline A/B of hunter gates over the labeled setup library.

Every setup the hunter saw (taken or skipped) is logged to
``livetrader/state/setup_snapshots/`` and labeled nightly by
``edge_model.label_snapshots``. This harness replays those setups under
alternative gate configurations and measures what each gate actually earns
or saves, so knob changes are evidence-based instead of vibes.

Output: ``autotrader/state/gate_ab/YYYY-MM-DD.json`` + ``.md``, and winning
knob diffs staged to the research director's pending gate proposals (never
auto-applied to the live arena).

    python -m autotrader.gate_ab
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from pathlib import Path
from typing import Any

from autotrader.bootstrap import state_dir
from autotrader.edge_model import FRICTION, STOP_PCT, TARGET_PCT, label_snapshots

logger = logging.getLogger("autotrader.gate_ab")

PRIME_END_MIN = 120.0  # 11:30 ET


def _labeled_library() -> list[dict[str, Any]]:
    """All labeled setup rows across every snapshot day on disk."""
    snap_dir = Path(__file__).resolve().parents[1] / "livetrader" / "state" / "setup_snapshots"
    rows: list[dict[str, Any]] = []
    if not snap_dir.exists():
        return rows
    for path in sorted(snap_dir.glob("*.jsonl")):
        day = path.stem
        try:
            rows.extend(label_snapshots(day))
        except Exception:  # noqa: BLE001 — one bad day must not kill the A/B
            logger.exception("labeling failed for %s", day)
    return rows


def _passes(row: dict[str, Any], gate: dict[str, Any]) -> bool:
    side = str(row.get("side", "long"))
    ext = float(row.get("ext_vwap", 0.0) or 0.0)
    buzz = float(row.get("buzz", 0.0) or 0.0)
    catalyst = bool(row.get("catalyst"))
    minutes = float(row.get("minutes_open", 0.0) or 0.0)
    rvol = float(row.get("rvol", 0.0) or 0.0)
    if side == "long" and ext > gate.get("max_ext", 1e9):
        return False
    if side == "short" and ext < -gate.get("max_ext_short", 1e9):
        return False
    if buzz < gate.get("min_buzz", 0.0) and not (gate.get("buzz_or_catalyst") and catalyst):
        return False
    if gate.get("require_catalyst") and not catalyst:
        return False
    lo, hi = gate.get("window", (0.0, 390.0))
    if not (lo <= minutes <= hi):
        return False
    if rvol < gate.get("min_rvol", 0.0):
        return False
    return True


def _wilson_lb(hits: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = hits / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (center - margin) / denom)


def _eval_gate(rows: list[dict[str, Any]], gate: dict[str, Any]) -> dict[str, Any]:
    taken = [r for r in rows if _passes(r, gate)]
    n = len(taken)
    if n == 0:
        return {"n": 0, "hit_rate": 0.0, "wilson_lb": 0.0, "ev_per_trade": 0.0,
                "total_ev": 0.0}
    hits = sum(1 for r in taken if int(r.get("label", 0)) == 1)
    hit_rate = hits / n
    ev = hit_rate * TARGET_PCT - (1 - hit_rate) * STOP_PCT - FRICTION
    return {
        "n": n,
        "hit_rate": round(hit_rate, 4),
        "wilson_lb": round(_wilson_lb(hits, n), 4),
        "ev_per_trade": round(ev, 5),
        "total_ev": round(ev * n, 4),
    }


def _grid() -> dict[str, dict[str, Any]]:
    """Named gate configs: production baseline + one-factor variants."""
    configs: dict[str, dict[str, Any]] = {
        "take_all": {},
        "production": {
            "max_ext": 0.06, "min_buzz": 0.0, "buzz_or_catalyst": True,
            "window": (0.0, 390.0), "min_rvol": 1.5,
        },
    }
    for cap in (0.03, 0.05, 0.08, 0.12):
        configs[f"ext_{int(cap*100)}pct"] = {
            "max_ext": cap, "buzz_or_catalyst": True, "min_rvol": 1.5,
        }
    for mb in (1.0, 2.0):
        configs[f"buzz_min_{int(mb)}"] = {
            "max_ext": 0.06, "min_buzz": mb, "buzz_or_catalyst": True,
            "min_rvol": 1.5,
        }
    configs["catalyst_required"] = {
        "max_ext": 0.06, "require_catalyst": True, "min_rvol": 1.5,
    }
    configs["prime_only"] = {
        "max_ext": 0.06, "buzz_or_catalyst": True, "min_rvol": 1.5,
        "window": (0.0, PRIME_END_MIN),
    }
    configs["afternoon_only"] = {
        "max_ext": 0.06, "buzz_or_catalyst": True, "min_rvol": 1.5,
        "window": (PRIME_END_MIN, 390.0),
    }
    for rv in (2.0, 3.0):
        configs[f"rvol_min_{int(rv)}"] = {
            "max_ext": 0.06, "buzz_or_catalyst": True, "min_rvol": rv,
        }
    return configs


def run_gate_ab() -> dict[str, Any]:
    rows = _labeled_library()
    if len(rows) < 40:
        logger.warning("gate A/B: only %d labeled setups — results are thin", len(rows))
    base_rate = (
        sum(1 for r in rows if int(r.get("label", 0)) == 1) / len(rows) if rows else 0.0
    )
    results: dict[str, Any] = {
        "date": dt.datetime.now().strftime("%Y-%m-%d"),
        "n_setups": len(rows),
        "base_rate": round(base_rate, 4),
        "configs": {},
    }
    for name, gate in _grid().items():
        results["configs"][name] = _eval_gate(rows, gate)

    # What the hunter actually took (reality check against the gates).
    taken = [r for r in rows if r.get("taken")]
    if taken:
        hits = sum(1 for r in taken if int(r.get("label", 0)) == 1)
        results["actually_taken"] = {
            "n": len(taken),
            "hit_rate": round(hits / len(taken), 4),
            "wilson_lb": round(_wilson_lb(hits, len(taken)), 4),
        }

    # Winner: max total EV with a hit-rate floor above base rate and n >= 15.
    eligible = {
        k: v for k, v in results["configs"].items()
        if v["n"] >= 15 and v["wilson_lb"] > base_rate
    }
    if eligible:
        winner = max(eligible, key=lambda k: eligible[k]["total_ev"])
        results["winner"] = winner
        results["winner_stats"] = eligible[winner]
    _write(results)
    _stage_proposals(results)
    return results


def _write(results: dict[str, Any]) -> None:
    out_dir = state_dir() / "gate_ab"
    out_dir.mkdir(parents=True, exist_ok=True)
    day = results["date"]
    (out_dir / f"{day}.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    lines = [
        f"# Gate A/B — {day}",
        "",
        f"Labeled setups: {results['n_setups']} | base hit rate: {results['base_rate']:.1%}",
        "",
        "| config | n | hit% | wilson_lb | EV/trade | total EV |",
        "|---|---|---|---|---|---|",
    ]
    for name, s in sorted(
        results["configs"].items(), key=lambda kv: -kv[1]["total_ev"]
    ):
        lines.append(
            f"| {name} | {s['n']} | {s['hit_rate']:.1%} | {s['wilson_lb']:.1%} "
            f"| {s['ev_per_trade']:+.3%} | {s['total_ev']:+.3f} |"
        )
    if "actually_taken" in results:
        t = results["actually_taken"]
        lines += [
            "",
            f"Hunter actually took: {t['n']} setups, hit {t['hit_rate']:.1%} "
            f"(wilson lb {t['wilson_lb']:.1%})",
        ]
    if "winner" in results:
        lines += ["", f"**Winner: {results['winner']}**"]
    (out_dir / f"{day}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("gate A/B written to %s", out_dir / f"{day}.md")


def _stage_proposals(results: dict[str, Any]) -> None:
    """Stage knob diffs between production and the winner for human review."""
    winner = results.get("winner")
    if not winner or winner == "production":
        return
    grid = _grid()
    prod, win = grid["production"], grid[winner]
    proposals = []
    knob_map = {"max_ext": "hunter_max_ext_pct"}
    for key in ("max_ext",):
        if win.get(key) != prod.get(key):
            proposals.append({
                "knob": knob_map[key],
                "current": prod.get(key),
                "proposed": win.get(key),
                "rationale": (
                    f"gate A/B {results['date']}: '{winner}' total EV "
                    f"{results['winner_stats']['total_ev']:+.3f} vs production "
                    f"{results['configs']['production']['total_ev']:+.3f} "
                    f"on {results['n_setups']} labeled setups"
                ),
                "staged_at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
            })
    if not proposals:
        return
    path = state_dir() / "research_director" / "pending_gate_proposals.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    path.write_text(
        json.dumps((existing + proposals)[-50:], indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("staged %d gate proposal(s) from A/B winner '%s'", len(proposals), winner)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    res = run_gate_ab()
    print(json.dumps({k: res[k] for k in ("n_setups", "base_rate", "winner")
                      if k in res}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
