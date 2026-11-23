"""Live news ingestion: markets, geopolitics, energy/oil, weather, macro.

All sources are free and keyless:
  - market/business RSS (CNBC, MarketWatch, Yahoo Finance),
  - geopolitics (BBC World, Al Jazeera),
  - energy (OilPrice, CNBC energy),
  - central banks (Federal Reserve press feed),
  - severe weather (NWS active-alerts API — hurricanes/freezes move natgas,
    oil, insurers, utilities).

Each headline is scored locally (keyword sentiment, zero cost, every poll)
and the aggregate stream feeds both the trading engine (as a live sentiment
regime input) and the periodic LLM macro assessment in sentiment.py.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field

from livetrader.config import NewsConfig, state_dir

logger = logging.getLogger("livetrader.news")

RSS_SOURCES: dict[str, str] = {
    "cnbc_top": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "cnbc_markets": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "cnbc_energy": "https://www.cnbc.com/id/19836768/device/rss/rss.html",
    "cnbc_world": "https://www.cnbc.com/id/100727362/device/rss/rss.html",
    "marketwatch_top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "marketwatch_pulse": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "bbc_business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "aljazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "oilprice": "https://oilprice.com/rss/main",
    "fed_press": "https://www.federalreserve.gov/feeds/press_all.xml",
}

NWS_ALERTS_URL = (
    "https://api.weather.gov/alerts/active?severity=Severe,Extreme&limit=50"
)

# -- local keyword sentiment (fast, free, runs on every headline) -------------
POSITIVE = {
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies",
    "record", "upgrade", "upgraded", "bullish", "growth", "profit", "gains",
    "jumps", "strong", "boost", "boosts", "approval", "approved", "deal",
    "acquisition", "buyback", "dividend", "expands", "wins", "breakthrough",
    "peace", "ceasefire", "agreement", "resolves", "cooling", "eases", "cut rates",
}
NEGATIVE = {
    "miss", "misses", "plunge", "plunges", "crash", "crashes", "sink", "sinks",
    "selloff", "sell-off", "downgrade", "downgraded", "bearish", "loss",
    "losses", "layoffs", "bankruptcy", "default", "fraud", "probe", "lawsuit",
    "recall", "warning", "warns", "cuts guidance", "weak", "slump", "slumps",
    "war", "strike", "strikes", "attack", "attacks", "invasion", "sanctions",
    "escalation", "escalates", "conflict", "missile", "tariff", "tariffs",
    "hurricane", "storm", "wildfire", "flood", "drought", "shutdown",
    "recession", "inflation", "stagflation", "hike rates", "embargo",
}
# Headlines matching these tags also get routed into topic buckets that the
# LLM assessment and the engine's regime logic can read directly.
TOPIC_PATTERNS: dict[str, re.Pattern] = {
    "geopolitics": re.compile(
        r"\b(war|missile|invasion|sanction|ceasefire|nato|iran|russia|ukraine|"
        r"taiwan|china|conflict|troops|strike[s]? on|nuclear|opec)\b", re.I),
    "energy": re.compile(
        r"\b(oil|crude|opec|natural gas|natgas|barrel|refinery|pipeline|lng)\b", re.I),
    "fed_rates": re.compile(
        r"\b(fed|fomc|rate[s]?|powell|inflation|cpi|ppi|treasury yield)\b", re.I),
    "weather": re.compile(
        r"\b(hurricane|storm|blizzard|heat wave|wildfire|flood|drought|freeze)\b", re.I),
    "crypto": re.compile(
        r"\b(bitcoin|btc|ethereum|crypto|etf inflow|stablecoin|binance|coinbase)\b", re.I),
}

_TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")
_COMMON_WORDS = {
    "CEO", "CFO", "IPO", "ETF", "GDP", "CPI", "PPI", "FBI", "SEC", "FDA",
    "OPEC", "NATO", "USA", "USD", "AI", "EV", "THE", "FOR", "NEW", "WHO",
    "NYSE", "US", "UK", "EU", "PM", "AM", "TV", "APP", "WAR", "OIL", "GAS",
}


@dataclass
class Headline:
    ts: float
    source: str
    title: str
    sentiment: float = 0.0            # -1..+1 local keyword score
    topics: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts, "source": self.source, "title": self.title,
            "sentiment": round(self.sentiment, 3),
            "topics": self.topics, "symbols": self.symbols,
        }


def score_headline(title: str) -> float:
    text = title.lower()
    pos = sum(1 for w in POSITIVE if w in text)
    neg = sum(1 for w in NEGATIVE if w in text)
    if pos == neg == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / max(pos + neg, 1)))


def tag_topics(title: str) -> list[str]:
    return [name for name, pat in TOPIC_PATTERNS.items() if pat.search(title)]


def extract_symbols(title: str, universe: set[str]) -> list[str]:
    hits = []
    for token in _TICKER_RE.findall(title):
        if token in universe and token not in _COMMON_WORDS:
            hits.append(token)
    return hits[:5]


class NewsStore:
    """Thread-safe rolling store of scored headlines + aggregates."""

    def __init__(self, cfg: NewsConfig) -> None:
        self.cfg = cfg
        self._lock = threading.RLock()
        self._headlines: deque[Headline] = deque(maxlen=cfg.max_headlines_kept)
        self._seen_titles: deque[str] = deque(maxlen=2000)
        self.llm_assessment: dict = {}    # set by sentiment.assess_with_llm

    def add(self, headline: Headline) -> bool:
        with self._lock:
            if headline.title in self._seen_titles:
                return False
            self._seen_titles.append(headline.title)
            self._headlines.append(headline)
        # Persist to the rolling news log so offline analysis (the nightly
        # post-mortem's swing attribution) can see yesterday's headlines
        # after a restart. Best effort; never blocks ingestion.
        try:
            with (state_dir() / "news_log.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(headline.to_dict(), default=str) + "\n")
        except Exception:  # noqa: BLE001
            pass
        return True

    def recent(self, max_age_s: float = 7200.0, limit: int = 100) -> list[Headline]:
        cutoff = time.time() - max_age_s
        with self._lock:
            return [h for h in list(self._headlines)[-limit * 2:] if h.ts >= cutoff][-limit:]

    def market_sentiment(self, max_age_s: float = 7200.0) -> float:
        """Recency-weighted mean sentiment of recent headlines (-1..+1)."""
        now = time.time()
        num = den = 0.0
        for h in self.recent(max_age_s=max_age_s, limit=200):
            w = max(0.1, 1.0 - (now - h.ts) / max_age_s)
            num += h.sentiment * w
            den += w
        return num / den if den else 0.0

    def symbol_sentiment(self, symbol: str, max_age_s: float = 14400.0) -> float:
        scores = [h.sentiment for h in self.recent(max_age_s=max_age_s, limit=300)
                  if symbol in h.symbols]
        return sum(scores) / len(scores) if scores else 0.0

    def has_catalyst(self, symbol: str, max_age_s: float = 14400.0) -> bool:
        """True if a recent headline mentions the symbol (soft catalyst gate)."""
        bare = symbol.split("-")[0].upper()
        for h in self.recent(max_age_s=max_age_s, limit=200):
            if bare in h.symbols or symbol.upper() in h.symbols:
                return True
            # Title mention fallback for names not in extract_symbols universe
            if f" {bare} " in f" {h.title.upper()} " or h.title.upper().startswith(bare + " "):
                return True
        return False

    def topic_pressure(self, max_age_s: float = 7200.0) -> dict[str, float]:
        """topic -> mean sentiment over recent headlines carrying that topic."""
        agg: dict[str, list[float]] = {}
        for h in self.recent(max_age_s=max_age_s, limit=300):
            for t in h.topics:
                agg.setdefault(t, []).append(h.sentiment)
        return {t: sum(v) / len(v) for t, v in agg.items()}

    def set_llm_assessment(self, assessment: dict) -> None:
        with self._lock:
            self.llm_assessment = dict(assessment)

    def get_llm_assessment(self) -> dict:
        with self._lock:
            return dict(self.llm_assessment)


def _entry_time(entry) -> float:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return calendar.timegm(parsed)
    return time.time()


class NewsFeed(threading.Thread):
    def __init__(
        self,
        store: NewsStore,
        cfg: NewsConfig,
        stop: threading.Event,
        universe_provider=None,
    ) -> None:
        super().__init__(name="news-feed", daemon=True)
        self.store = store
        self.cfg = cfg
        self.stop_event = stop
        # callable returning the set of bare tickers currently traded
        self.universe_provider = universe_provider or (lambda: set())
        self.healthy = False
        self.last_ok = 0.0

    def _poll_rss(self) -> int:
        import feedparser

        universe = set(self.universe_provider())
        added = 0
        cutoff = time.time() - self.cfg.headline_max_age_s
        for source, url in RSS_SOURCES.items():
            if self.stop_event.is_set():
                break
            try:
                parsed = feedparser.parse(url)
                for entry in parsed.entries[:30]:
                    title = (getattr(entry, "title", "") or "").strip()
                    if not title:
                        continue
                    ts = _entry_time(entry)
                    if ts < cutoff:
                        continue
                    h = Headline(
                        ts=ts, source=source, title=title,
                        sentiment=score_headline(title),
                        topics=tag_topics(title),
                        symbols=extract_symbols(title, universe),
                    )
                    if self.store.add(h):
                        added += 1
            except Exception as exc:  # noqa: BLE001 — a dead feed must not stop the rest
                logger.debug("rss %s failed: %s", source, exc)
        return added

    def _poll_weather(self) -> int:
        """NWS severe/extreme alerts — the weather input that moves energy."""
        try:
            req = urllib.request.Request(
                NWS_ALERTS_URL,
                headers={"User-Agent": "livetrader (research; contact: local)",
                         "Accept": "application/geo+json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            added = 0
            for feature in data.get("features", [])[:25]:
                props = feature.get("properties", {})
                event = props.get("event", "")
                area = (props.get("areaDesc", "") or "")[:80]
                if not event:
                    continue
                title = f"NWS ALERT: {event} — {area}"
                h = Headline(
                    ts=time.time(), source="nws", title=title,
                    sentiment=-0.3,  # severe weather skews risk-negative
                    topics=["weather"], symbols=[],
                )
                if self.store.add(h):
                    added += 1
            return added
        except Exception as exc:  # noqa: BLE001
            logger.debug("nws poll failed: %s", exc)
            return 0

    def run(self) -> None:
        while not self.stop_event.is_set():
            start = time.time()
            added = self._poll_rss() + self._poll_weather()
            if added >= 0:
                self.healthy = True
                self.last_ok = time.time()
            if added:
                logger.info(
                    "news: +%d headlines (market sentiment %.2f)",
                    added, self.store.market_sentiment(),
                )
            elapsed = time.time() - start
            self.stop_event.wait(max(self.cfg.poll_s - elapsed, 5.0))
        logger.info("news feed stopped")
