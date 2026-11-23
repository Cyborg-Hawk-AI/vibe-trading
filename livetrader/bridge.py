"""Thin bridge: autotrader gate-passers → livetrader arena seeds (no real orders)."""

from __future__ import annotations

import json
from pathlib import Path

from autotrader.validation_ladder import load_gate_passer_seeds
from livetrader.engine.arena import Arena

_DAYTRADE_SEED_PATH = (
    Path(__file__).resolve().parents[1]
    / "autotrader" / "state" / "daytrade_gate_passers_seed.json"
)


def load_daytrade_seeds(limit: int = 16) -> list[dict]:
    """Genomes that passed the make-money-daily gate on intraday replay.

    These were *born* on the 5m/15m session library the live desk actually
    trades, so they outrank every legacy daily-bar seed.
    """
    try:
        rows = json.loads(_DAYTRADE_SEED_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    out: list[dict] = []
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("genome"), dict):
            out.append(r["genome"])
        if len(out) >= limit:
            break
    return out


def enrich_seed_genomes(existing: list[dict] | None = None) -> list[dict]:
    """Merge seeds, best evidence first: daytrade gate-passers (intraday-
    native) > explicit existing seeds > historical champions > legacy
    daily-bar gate-passers. Dedupe keeps the first occurrence, so order IS
    the priority."""
    seeds = load_daytrade_seeds()
    seeds.extend(existing or [])
    seeds.extend(Arena.load_seed_genomes())
    for r in load_gate_passer_seeds():
        if isinstance(r, dict) and isinstance(r.get("genome"), dict):
            seeds.append(r["genome"])
        elif isinstance(r, dict):
            seeds.append(r)
    # Dedupe by genome_id
    from autotrader.strategy_space import genome_id

    seen: set[str] = set()
    unique: list[dict] = []
    for g in seeds:
        if not isinstance(g, dict) or "momentum_lookback" not in g:
            continue
        gid = genome_id(g)
        if gid not in seen:
            seen.add(gid)
            unique.append(g)
    return unique
