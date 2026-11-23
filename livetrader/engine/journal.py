"""Trade journal: every closed paper trade, stamped with the news around it.

Each sell fill from the arena is appended to ``livetrader/state/
closed_trades.jsonl`` with entry/exit prices, realized P&L, the exit reason,
and the top recent headlines that mention the symbol — so a human (or the
LLM check-in) can look at any exit and ask "why did it move?" without
guessing. Headlines are only attached when they actually matched; no cause
is ever invented.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from livetrader.config import state_dir

if TYPE_CHECKING:
    from livetrader.news.rss import NewsStore
    from livetrader.sim.broker import Fill

logger = logging.getLogger("livetrader.journal")

NY = ZoneInfo("America/New_York")
JOURNAL_NAME = "closed_trades.jsonl"


def journal_path():
    return state_dir() / JOURNAL_NAME


def attribute_news(
    news: "NewsStore", symbol: str, *, max_age_s: float = 14400.0, top: int = 3,
) -> list[dict]:
    """Top recent headlines that mention this symbol (may be empty)."""
    bare = symbol.split("-")[0].upper()
    hits: list[dict] = []
    for h in news.recent(max_age_s=max_age_s, limit=300):
        title_padded = f" {h.title.upper()} "
        if (
            bare in h.symbols
            or symbol.upper() in h.symbols
            or f" {bare} " in title_padded
        ):
            hits.append({
                "title": h.title,
                "source": h.source,
                "sentiment": round(h.sentiment, 3),
                "age_min": round((dt.datetime.now().timestamp() - h.ts) / 60.0, 1),
            })
    # Strongest signal first: prefer headlines with a definite sentiment.
    hits.sort(key=lambda x: abs(x["sentiment"]), reverse=True)
    return hits[:top]


def record_closed_trade(
    fill: "Fill",
    *,
    gid: str,
    sleeve: str,
    kind: str,
    news: "NewsStore | None" = None,
) -> None:
    """Append one closed trade to the journal (best effort, never raises)."""
    try:
        headlines = attribute_news(news, fill.symbol) if news is not None else []
        row = {
            "ts": round(fill.ts, 1),
            "date_ny": dt.datetime.fromtimestamp(fill.ts, tz=NY).strftime("%Y-%m-%d"),
            "gid": gid,
            "sleeve": sleeve,
            "kind": kind,
            "symbol": fill.symbol,
            "qty": round(fill.qty, 8),
            "entry_price": round(fill.entry_price, 8),
            "exit_price": round(fill.price, 8),
            "fee": round(fill.fee, 6),
            "realized_pnl": round(fill.realized_pnl, 6),
            "held_s": round(fill.held_s, 1),
            "entry_reason": fill.entry_reason,
            "exit_reason": fill.reason,
            "side": getattr(fill, "position_side", "long") or "long",
            "headlines": headlines,
        }
        with journal_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except Exception:  # noqa: BLE001 — journaling must never break trading
        logger.exception("failed to journal closed trade %s", fill.symbol)


def load_trades() -> list[dict]:
    """All journaled trades (skips malformed lines)."""
    path = journal_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
