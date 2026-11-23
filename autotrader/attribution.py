"""Winner attribution: figure out WHY the winning symbols/trades succeeded.

For the top winning symbols of a run this module gathers deterministic
context (trade stats, causal price/volume stats, anomaly events, PIT-gated
news via company briefs) and asks the LLM to attribute each win to a concrete
driver — momentum burst, news catalyst, sector move, liquidity event — and to
judge whether the strategy CAPTURED the driver or just got lucky.

The output feeds two places:
  - a ``gene_bias`` map (gene -> +1/-1) reinforcing what worked, consumed by
    the optimizer alongside the failure-driven bias, and
  - the check-in payload, so the research-director LLM sees attribution
    when proposing amendments.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Dict

import pandas as pd

from autotrader.company_intel import get_company_brief
from autotrader.config import PipelineConfig
from autotrader.diagnostics import parse_round_trips
from autotrader.llm import chat, extract_json_block
from autotrader.simulator import detect_anomalies
from autotrader.strategy_space import GENE_SPACE

logger = logging.getLogger(__name__)

ATTRIBUTION_SYSTEM_PROMPT = (
    "You are a trade-attribution analyst. For each winning symbol you get "
    "trade statistics, causal price/volume context, anomaly events, and "
    "recent headlines. For each symbol reply with: the most likely driver "
    "of the win (one of: momentum_burst, news_catalyst, sector_move, "
    "liquidity_event, mean_reversion, unknown), whether the strategy "
    "captured that driver deliberately or was lucky, and which strategy "
    "parameters to reinforce. Reply with ONLY a JSON array of objects: "
    '{"symbol": str, "driver": str, "captured": bool, "explanation": str, '
    '"gene_bias": {gene_name: 1 or -1}}. Valid gene names: '
    + ", ".join(sorted(GENE_SPACE)) + "."
)


def winning_symbols(trades_csv: str, top_n: int = 5) -> list[tuple[str, float, int]]:
    """Top symbols by realized PnL: (symbol, pnl, trade_count)."""
    trips = parse_round_trips(trades_csv)
    pnl: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    for t in trips:
        pnl[t.code] += t.pnl
        count[t.code] += 1
    winners = [(s, p, count[s]) for s, p in pnl.items() if p > 0]
    winners.sort(key=lambda x: -x[1])
    return winners[:top_n]


def _symbol_context(
    symbol: str,
    trades_csv: str,
    data_map: Dict[str, pd.DataFrame],
    *,
    include_news: bool,
) -> dict[str, Any]:
    trips = [t for t in parse_round_trips(trades_csv) if t.code == symbol]
    wins = [t for t in trips if t.pnl > 0]
    last_exit = max((t.exit_ts for t in trips), default=None)
    ctx: dict[str, Any] = {
        "symbol": symbol,
        "trades": len(trips),
        "wins": len(wins),
        "total_pnl": round(sum(t.pnl for t in trips), 4),
        "avg_win_return_pct": round(
            sum(t.return_pct for t in wins) / len(wins), 3
        ) if wins else 0.0,
        "avg_holding_days": round(
            sum((t.exit_ts - t.entry_ts).days for t in trips) / len(trips), 1
        ) if trips else 0.0,
    }
    frame = data_map.get(symbol)
    if frame is not None and last_exit is not None:
        brief = get_company_brief(
            symbol, str(last_exit.date()), frame,
            include_news=include_news, include_profile=include_news,
        )
        ctx.update(brief)
        events = detect_anomalies({symbol: frame}, max_events=10)
        ctx["anomaly_events"] = [vars(e) for e in events]
    return ctx


def attribute_run(
    trades_csv: str,
    data_map: Dict[str, pd.DataFrame],
    cfg: PipelineConfig,
    *,
    top_n: int = 4,
    include_news: bool = True,
) -> dict[str, Any]:
    """Attribute a run's winners. Returns attribution list + merged gene bias.

    Fully offline-safe: without an LLM (or news access) the deterministic
    context is still returned with driver="unknown" and an empty gene bias.
    """
    winners = winning_symbols(trades_csv, top_n=top_n)
    if not winners:
        return {"attributions": [], "gene_bias": {}}

    contexts = [
        _symbol_context(sym, trades_csv, data_map, include_news=include_news)
        for sym, _pnl, _n in winners
    ]

    attributions: list[dict[str, Any]] = []
    gene_bias: dict[str, int] = {}
    reply = chat(
        cfg.llm,
        ATTRIBUTION_SYSTEM_PROMPT,
        json.dumps(contexts, default=str),
        max_tokens=1400,
    )
    parsed = extract_json_block(reply or "")
    if isinstance(parsed, list):
        known = {c["symbol"] for c in contexts}
        for item in parsed:
            if not isinstance(item, dict) or item.get("symbol") not in known:
                continue
            entry = {
                "symbol": item["symbol"],
                "driver": str(item.get("driver", "unknown"))[:40],
                "captured": bool(item.get("captured", False)),
                "explanation": str(item.get("explanation", ""))[:500],
            }
            raw_bias = item.get("gene_bias")
            if isinstance(raw_bias, dict) and entry["captured"]:
                for gene, direction in raw_bias.items():
                    if gene in GENE_SPACE and direction in (1, -1, 1.0, -1.0):
                        gene_bias[gene] = int(direction)
                        entry.setdefault("gene_bias", {})[gene] = int(direction)
            attributions.append(entry)
    else:
        attributions = [
            {"symbol": c["symbol"], "driver": "unknown", "captured": False,
             "explanation": "(no LLM attribution available)"}
            for c in contexts
        ]

    return {"attributions": attributions, "gene_bias": gene_bias}
