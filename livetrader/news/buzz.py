"""Buzz monitor: free social + screener signals -> per-symbol buzz score.

The hunter desk's eyes. Every ``buzz_poll_s`` this thread polls keyless
public sources and folds them into one decayed score per symbol:

  - StockTwits trending symbols (``api.stocktwits.com/api/2/trending``),
  - Reddit r/wallstreetbets + r/pennystocks hot posts (public JSON) with
    $TICKER extraction and mention-velocity tracking,
  - fresh news catalysts from the shared ``NewsStore``.

score = decay(age) * (2.0*trending + min(velocity, 5) + 1.5*catalyst)

Every poll persists the current table to ``livetrader/state/buzz.json``
(inspectable: "why did we buy this?") and appends a timestamped row to
``buzz_log.jsonl`` so the nightly post-mortem can attribute intraday
swings to buzz spikes. All sources are optional enrichment — a dead API
degrades the score to whatever the remaining sources support, silently.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request

from livetrader.config import NewsConfig, state_dir
from livetrader.news.rss import NewsStore

logger = logging.getLogger("livetrader.buzz")

STOCKTWITS_TRENDING_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"
REDDIT_SUBS = ("wallstreetbets", "pennystocks")
REDDIT_URL = "https://www.reddit.com/r/{sub}/hot.json?limit=50"
_UA = {"User-Agent": "livetrader-research/1.0 (paper trading simulation)"}

# $TSLA style (always accepted) and bare uppercase tokens (accepted only
# when the token is a symbol the desk already knows about).
_DOLLAR_TICKER = re.compile(r"\$([A-Za-z]{1,5})\b")
_BARE_TICKER = re.compile(r"\b([A-Z]{2,5})\b")
_NOISE_WORDS = {
    "YOLO", "DD", "CEO", "CFO", "IPO", "ETF", "GDP", "CPI", "FBI", "SEC",
    "FDA", "OPEC", "NATO", "USA", "USD", "THE", "FOR", "NEW", "WHO", "NYSE",
    "US", "UK", "EU", "PM", "AM", "TV", "APP", "WAR", "OIL", "GAS", "ATH",
    "FOMO", "WSB", "EDIT", "TLDR", "IMO", "LOL", "AI", "EV", "OTM", "ITM",
    "PUTS", "CALLS", "BUY", "SELL", "HOLD", "MOON", "APES", "GAIN", "LOSS",
}

_HALF_LIFE_S = 3600.0     # buzz components halve every hour
_STALE_S = 4 * 3600.0     # drop symbols not re-confirmed within 4h


def _decay(age_s: float) -> float:
    return 0.5 ** (max(age_s, 0.0) / _HALF_LIFE_S)


def _get_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class BuzzStore:
    """Thread-safe per-symbol buzz table with hourly decay."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # sym -> {"trending": 0/1, "mentions": float, "velocity": float,
        #         "catalyst": 0/1, "sources": [..], "updated": ts}
        self._symbols: dict[str, dict] = {}

    # -- writers (BuzzFeed only) ---------------------------------------------
    def update(self, symbol: str, **fields) -> None:
        with self._lock:
            row = self._symbols.setdefault(
                symbol,
                {"trending": 0.0, "mentions": 0.0, "velocity": 0.0,
                 "catalyst": 0.0, "sources": [], "updated": 0.0},
            )
            sources = set(row["sources"]) | set(fields.pop("sources", []))
            row.update(fields)
            row["sources"] = sorted(sources)
            row["updated"] = time.time()

    def prune(self) -> None:
        cutoff = time.time() - _STALE_S
        with self._lock:
            for sym in [s for s, r in self._symbols.items() if r["updated"] < cutoff]:
                del self._symbols[sym]

    # -- readers ---------------------------------------------------------------
    def score(self, symbol: str) -> float:
        with self._lock:
            row = self._symbols.get(symbol.split("-")[0].upper())
        if row is None:
            return 0.0
        raw = (
            2.0 * float(row["trending"])
            + min(float(row["velocity"]), 5.0)
            + 1.5 * float(row["catalyst"])
        )
        return raw * _decay(time.time() - row["updated"])

    def meta(self, symbol: str) -> dict:
        bare = symbol.split("-")[0].upper()
        with self._lock:
            row = dict(self._symbols.get(bare, {}))
        row["score"] = self.score(bare)
        return row

    def top(self, n: int = 20) -> list[tuple[str, float]]:
        with self._lock:
            syms = list(self._symbols)
        scored = [(s, self.score(s)) for s in syms]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return [(s, sc) for s, sc in scored[:n] if sc > 0.0]

    def snapshot(self) -> dict:
        with self._lock:
            table = {s: dict(r) for s, r in self._symbols.items()}
        for sym, row in table.items():
            row["score"] = round(self.score(sym), 3)
        return table


class BuzzFeed(threading.Thread):
    def __init__(
        self,
        store: BuzzStore,
        news: NewsStore,
        cfg: NewsConfig,
        stop: threading.Event,
        universe_provider=None,
    ) -> None:
        super().__init__(name="buzz-feed", daemon=True)
        self.store = store
        self.news = news
        self.cfg = cfg
        self.stop_event = stop
        self.universe_provider = universe_provider or (lambda: set())
        self.healthy = False
        self.last_ok = 0.0
        # EWMA baseline of reddit mention counts per symbol (velocity denom).
        self._mention_baseline: dict[str, float] = {}

    # -- sources ---------------------------------------------------------------
    def _poll_stocktwits(self) -> int:
        try:
            data = _get_json(STOCKTWITS_TRENDING_URL)
        except Exception as exc:  # noqa: BLE001 — optional source
            logger.debug("stocktwits poll failed: %s", exc)
            return 0
        n = 0
        for item in (data.get("symbols") or [])[:30]:
            sym = str(item.get("symbol", "")).upper()
            if not sym or not sym.isalpha() or len(sym) > 5:
                continue
            self.store.update(sym, trending=1.0, sources=["stocktwits"])
            n += 1
        return n

    def _poll_reddit(self) -> int:
        universe = {s.split("-")[0].upper() for s in self.universe_provider()}
        counts: dict[str, int] = {}
        polled_any = False
        for sub in REDDIT_SUBS:
            try:
                data = _get_json(REDDIT_URL.format(sub=sub))
                polled_any = True
            except Exception as exc:  # noqa: BLE001 — optional source
                logger.debug("reddit %s poll failed: %s", sub, exc)
                continue
            for child in (data.get("data", {}).get("children") or [])[:50]:
                title = str(child.get("data", {}).get("title", ""))
                seen: set[str] = set()
                for m in _DOLLAR_TICKER.finditer(title):
                    seen.add(m.group(1).upper())
                for m in _BARE_TICKER.finditer(title):
                    tok = m.group(1)
                    if tok in universe and tok not in _NOISE_WORDS:
                        seen.add(tok)
                for sym in seen:
                    if sym in _NOISE_WORDS:
                        continue
                    counts[sym] = counts.get(sym, 0) + 1
        if not polled_any:
            return 0
        for sym, count in counts.items():
            base = self._mention_baseline.get(sym, 1.0)
            velocity = count / max(base, 0.5)
            self._mention_baseline[sym] = 0.8 * base + 0.2 * count
            self.store.update(
                sym, mentions=float(count),
                velocity=round(min(velocity, 10.0), 2), sources=["reddit"],
            )
        return len(counts)

    def _refresh_catalysts(self) -> None:
        """Fold NewsStore catalyst freshness into every tracked symbol."""
        for sym, _score in self.store.top(60):
            has = self.news.has_catalyst(sym, max_age_s=7200.0)
            self.store.update(sym, catalyst=1.0 if has else 0.0,
                              sources=["news"] if has else [])

    # -- persistence -------------------------------------------------------------
    def _persist(self) -> None:
        table = self.store.snapshot()
        payload = {
            "ts": round(time.time(), 1),
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbols": table,
        }
        try:
            path = state_dir() / "buzz.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
            tmp.replace(path)
            # Compact history row for the nightly post-mortem (swing attribution).
            top = [
                {"symbol": s, "score": round(sc, 2),
                 "sources": table.get(s, {}).get("sources", [])}
                for s, sc in self.store.top(15)
            ]
            with (state_dir() / "buzz_log.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": payload["ts"], "top": top}) + "\n")
        except Exception:  # noqa: BLE001 — persistence must not stop polling
            logger.exception("buzz persist failed")

    # -- thread loop ---------------------------------------------------------------
    def run(self) -> None:
        while not self.stop_event.is_set():
            start = time.time()
            try:
                n_st = self._poll_stocktwits()
                n_rd = self._poll_reddit()
                self._refresh_catalysts()
                self.store.prune()
                self._persist()
                if n_st or n_rd:
                    self.healthy = True
                    self.last_ok = time.time()
                    top = self.store.top(5)
                    logger.info(
                        "buzz: %d trending, %d reddit symbols | top: %s",
                        n_st, n_rd,
                        ", ".join(f"{s}={sc:.1f}" for s, sc in top) or "-",
                    )
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("buzz poll failed")
            elapsed = time.time() - start
            self.stop_event.wait(max(self.cfg.buzz_poll_s - elapsed, 10.0))
        logger.info("buzz feed stopped")
