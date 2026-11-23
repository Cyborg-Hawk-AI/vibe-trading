"""Live US equity feed: yfinance 1m bars, polled during market hours.

Free real-time-ish coverage: Yahoo 1m bars lag reality by well under a
minute, which is honest enough for a paper simulation whose fills already
charge spread + slippage. Off-hours the thread idles (crypto keeps the
arena alive) and refreshes daily history so indicators stay warm.

Dynamic discovery: every ``discover_every_s`` the feed pulls Yahoo's
most-active / top-gainer screens and registers new movers into the tape —
the "find stocks left and right" requirement, live.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from zoneinfo import ZoneInfo

import numpy as np

from livetrader.config import FeedConfig
from livetrader.feeds.base import MarketData, Quote

logger = logging.getLogger("livetrader.feeds.equities")

NY = ZoneInfo("America/New_York")

CORE_EQUITIES = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD",
    "NFLX", "CRM", "ORCL", "INTC", "QCOM", "MU", "PLTR", "UBER", "COIN",
    "JPM", "BAC", "WFC", "GS", "V", "MA", "XOM", "CVX", "OXY", "SLB",
    "LLY", "UNH", "PFE", "MRK", "WMT", "COST", "HD", "MCD", "DIS", "NKE",
    "BA", "CAT", "GE", "F", "GM", "T", "VZ", "PYPL", "SOFI", "XYZ",
    "SPY", "QQQ", "IWM", "XLE", "XLF", "SMH", "GDX", "TLT", "GLD", "USO",
]

PENNY_EQUITIES = [
    "SNDL", "PLUG", "NOK", "SIRI", "LCID", "NIO", "AMC", "BB", "OPEN",
    "GRAB", "IONQ", "RIG", "BTG", "TLRY", "CGC", "ACB", "VALE", "ITUB",
    "BBD", "ABEV", "KGC", "UEC", "DNN", "FCEL", "CHPT", "MARA", "RIOT",
    "HUT", "CLSK", "SOUN", "BBAI", "RKLB", "ASTS", "SPCE",
]


def market_open_now(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(tz=NY)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt.time(9, 30) <= t <= dt.time(16, 0)


def _spread_bps_for(price: float, tiers) -> float:
    for ceiling, bps in tiers:
        if price <= ceiling:
            return bps
    return tiers[-1][1]


class EquityFeed(threading.Thread):
    def __init__(
        self,
        market: MarketData,
        cfg: FeedConfig,
        stop: threading.Event,
        spread_tiers=((1.0, 30.0), (5.0, 15.0), (20.0, 8.0), (1e12, 3.0)),
        buzz=None,
        news=None,
    ) -> None:
        super().__init__(name="equity-feed", daemon=True)
        self.market = market
        self.cfg = cfg
        self.stop_event = stop
        self.spread_tiers = spread_tiers
        self.buzz = buzz              # BuzzStore | None (hot-list ranking input)
        self.news = news              # NewsStore | None (catalyst bonus)
        self.symbols: dict[str, str] = {}  # ticker -> kind
        for s in CORE_EQUITIES[: cfg.equity_universe_size]:
            self.symbols[s] = "equity"
        for s in PENNY_EQUITIES[: cfg.penny_universe_size]:
            self.symbols[s] = "penny"
        self.healthy = False
        self.last_ok = 0.0
        self._discovered: set[str] = set()
        # Daily baselines for true gap / 20-day rvol: sym -> (prev_close, avg_vol)
        self._baselines: dict[str, tuple[float, float]] = {}
        self._baseline_date: str = ""

    # -- data pulls ----------------------------------------------------------
    def _download(self, tickers: list[str], period: str, interval: str):
        import yfinance as yf

        return yf.download(
            tickers=" ".join(tickers), period=period, interval=interval,
            group_by="ticker", threads=True, progress=False,
            prepost=False, auto_adjust=False,
        )

    def _push_frame(self, df, tickers: list[str]) -> int:
        """Convert a yfinance multi-ticker frame into tape bars + quotes."""
        import pandas as pd

        pushed = 0
        now = time.time()
        for ticker in tickers:
            try:
                sub = df[ticker] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                continue
            bars = [
                (idx.timestamp(), float(r["Open"]), float(r["High"]),
                 float(r["Low"]), float(r["Close"]), float(r.get("Volume", 0.0)))
                for idx, r in sub.iterrows()
            ]
            self.market.push_bars(ticker, bars)
            last_close = bars[-1][4]
            half = last_close * _spread_bps_for(last_close, self.spread_tiers) / 2e4
            self.market.push_quote(
                ticker,
                Quote(bid=last_close - half, ask=last_close + half,
                      last=last_close, ts=now),
            )
            pushed += 1
        return pushed

    def warmup(self) -> int:
        tickers = sorted(self.symbols)
        for t in tickers:
            self.market.register(t, self.symbols[t])
        ok = 0
        for chunk_start in range(0, len(tickers), 40):
            chunk = tickers[chunk_start:chunk_start + 40]
            try:
                df = self._download(chunk, f"{self.cfg.equity_warmup_days}d", "1m")
                ok += self._push_frame(df, chunk)
            except Exception as exc:  # noqa: BLE001
                logger.warning("equity warmup chunk failed: %s", exc)
            if self.stop_event.is_set():
                break
        logger.info("equity warmup: %d/%d symbols backfilled", ok, len(tickers))
        return ok

    def _poll_live(self) -> None:
        tickers = sorted(self.symbols)
        pushed = 0
        for chunk_start in range(0, len(tickers), 40):
            chunk = tickers[chunk_start:chunk_start + 40]
            try:
                df = self._download(chunk, "1d", "1m")
                pushed += self._push_frame(df, chunk)
            except Exception as exc:  # noqa: BLE001
                logger.warning("equity poll chunk failed: %s", exc)
            if self.stop_event.is_set():
                return
        if pushed:
            self.healthy = True
            self.last_ok = time.time()

    # -- daily baselines (true gap + 20-day rvol denominators) ------------------
    def _refresh_baselines(self, only: list[str] | None = None) -> None:
        """Fetch prev daily close + 20-day average volume, once per day.

        ``only`` restricts the fetch to symbols missing a baseline (used when
        buzz/screener discovery registers new names mid-session).
        """
        import pandas as pd

        today = dt.datetime.now(tz=NY).strftime("%Y-%m-%d")
        if only is None and self._baseline_date == today:
            return
        tickers = sorted(only if only is not None else self.symbols)
        tickers = [t for t in tickers if only is None or t not in self._baselines]
        if not tickers:
            if only is None:
                self._baseline_date = today
            return
        for chunk_start in range(0, len(tickers), 40):
            chunk = tickers[chunk_start:chunk_start + 40]
            try:
                df = self._download(chunk, "21d", "1d")
            except Exception as exc:  # noqa: BLE001
                logger.warning("baseline chunk failed: %s", exc)
                continue
            for ticker in chunk:
                try:
                    sub = df[ticker] if isinstance(df.columns, pd.MultiIndex) else df
                except KeyError:
                    continue
                sub = sub.dropna(subset=["Close"])
                # Drop today's (partial) row so baselines are prior sessions only.
                sub = sub[[str(idx.date()) != today for idx in sub.index]]
                if len(sub) < 5:
                    continue
                prev_close = float(sub["Close"].iloc[-1])
                avg_vol = float(sub["Volume"].tail(20).mean())
                if prev_close > 0 and avg_vol > 0:
                    self._baselines[ticker] = (prev_close, avg_vol)
            if self.stop_event.is_set():
                return
        if only is None:
            self._baseline_date = today
            logger.info("daily baselines refreshed: %d symbols", len(self._baselines))

    # -- hot-list scan -----------------------------------------------------------
    def _scan_metrics(self, ticker: str) -> dict | None:
        """Gap / rvol / buzz / catalyst metrics for one candidate, or None."""
        arrays = self.market.arrays(ticker, tf_minutes=1, tail=420)
        if arrays is None or len(arrays["close"]) < 15:
            return None
        px = float(arrays["close"][-1])
        if not (self.cfg.sip_price_min <= px <= self.cfg.sip_price_max):
            return None
        baseline = self._baselines.get(ticker)
        if baseline is None:
            return None
        prev_close, avg_vol = baseline
        gap = px / prev_close - 1.0
        # Today's cumulative volume vs the 20-day average, scaled by elapsed
        # session time (a stock at 3x pace at 10:00 is genuinely in play).
        now_et = dt.datetime.now(tz=NY)
        open_ts = now_et.replace(hour=9, minute=30, second=0, microsecond=0).timestamp()
        session_mask = arrays["ts"] >= open_ts
        cum_vol = float(np.sum(arrays["volume"][session_mask]))
        elapsed = max((now_et.timestamp() - open_ts) / (6.5 * 3600.0), 0.05)
        elapsed = min(elapsed, 1.0)
        rvol = cum_vol / (avg_vol * elapsed) if avg_vol > 0 else 0.0
        buzz_score = float(self.buzz.score(ticker)) if self.buzz is not None else 0.0
        catalyst = bool(self.news.has_catalyst(ticker)) if self.news is not None else False
        buzz_meta = self.buzz.meta(ticker) if self.buzz is not None else {}
        # Session VWAP extension (how far above/below today's volume-weighted avg).
        from livetrader.engine import indicators as ind

        vwap = ind.session_vwap(
            arrays["high"], arrays["low"], arrays["close"], arrays["volume"],
            arrays["ts"], session_open_epoch=open_ts,
        )
        ext_vwap = (px / vwap - 1.0) if vwap and vwap > 0 else 0.0
        # Partial-day archetype + buzz lead-lag (same features the edge
        # model trains on — computed with no lookahead).
        arch = "chop"
        buzz_lead = 999.0
        try:
            from autotrader.edge_features import buzz_lead_from_store, partial_archetype

            sess = arrays["ts"] >= open_ts
            if int(sess.sum()) >= 3:
                times = [
                    dt.datetime.fromtimestamp(float(t), tz=NY).time()
                    for t in arrays["ts"][sess]
                ]
                arch = partial_archetype(
                    times,
                    arrays["open"][sess].astype(float).tolist(),
                    arrays["close"][sess].astype(float).tolist(),
                    int(sess.sum()) - 1,
                )
            buzz_lead = buzz_lead_from_store(ticker, now_et.timestamp(), self.news)
        except Exception:  # noqa: BLE001 — features are best-effort
            pass
        return {
            "symbol": ticker,
            "price": round(px, 4),
            "gap": round(gap, 4),
            "rvol": round(rvol, 2),
            "buzz": round(buzz_score, 2),
            "catalyst": catalyst,
            "sources": buzz_meta.get("sources", []),
            "ext_vwap": round(ext_vwap, 4),
            "vwap": round(vwap, 4) if vwap else 0.0,
            "arch": arch,
            "buzz_lead_min": round(buzz_lead, 1),
            "score": round(
                buzz_score + abs(gap) * 100.0 + min(rvol, 20.0)
                + (3.0 if catalyst else 0.0), 3,
            ),
        }

    def _rank_hot_list(self) -> None:
        """Rank all candidates; tag the top ``sip_top_k`` as kind='sip'."""
        if not self.cfg.sip_enabled:
            return
        candidates: list[dict] = []
        for sym in list(self.symbols):
            if self.symbols[sym] not in ("discovered", "sip", "equity", "penny"):
                continue
            m = self._scan_metrics(sym)
            if m is None:
                continue
            # In-play requires abnormal volume AND either a real gap or buzz.
            if m["rvol"] < self.cfg.sip_daily_rvol_min:
                continue
            if abs(m["gap"]) < self.cfg.sip_min_gap_pct and m["buzz"] < 2.0:
                continue
            candidates.append(m)
        candidates.sort(key=lambda m: m["score"], reverse=True)
        hot = candidates[: self.cfg.sip_top_k]
        hot_syms = {m["symbol"] for m in hot}
        promoted = demoted = 0
        for sym in list(self.symbols):
            if sym in hot_syms and self.symbols[sym] != "sip":
                self.symbols[sym] = "sip"
                self.market.register(sym, "sip")
                promoted += 1
            elif self.symbols[sym] == "sip" and sym not in hot_syms:
                self.symbols[sym] = "discovered"
                self.market.register(sym, "discovered")
                demoted += 1
        self.market.set_hot_meta({m["symbol"]: m for m in hot})
        # Log only on membership change (re-rank runs every poll).
        hot_key = tuple(sorted(hot_syms))
        if hot_key != getattr(self, "_last_hot_key", None):
            self._last_hot_key = hot_key
            if hot:
                logger.info(
                    "hot list (%d, +%d/-%d): %s",
                    len(hot), promoted, demoted,
                    " | ".join(
                        f"{m['symbol']} s={m['score']:.1f} gap={m['gap']:+.1%} "
                        f"rvol={m['rvol']:.1f} buzz={m['buzz']:.1f}"
                        f"{' CAT' if m['catalyst'] else ''}"
                        f" ext={m.get('ext_vwap', 0):+.1%}"
                        for m in hot[:8]
                    ),
                )
            else:
                logger.info("hot list empty")

    # -- dynamic discovery ------------------------------------------------------
    def _discover_movers(self) -> None:
        """Pull Yahoo screens + buzz symbols into the tape; re-rank the hot list."""
        try:
            import yfinance as yf

            found: list[str] = []
            for screen in ("most_actives", "day_gainers", "day_losers", "small_cap_gainers"):
                try:
                    res = yf.screen(screen, count=self.cfg.discover_count)
                    quotes = res.get("quotes", []) if isinstance(res, dict) else []
                    found += [q.get("symbol", "") for q in quotes]
                except Exception:  # noqa: BLE001 — screener API shifts; optional
                    continue
            # Buzz-discovered symbols join the tape too (registered like movers).
            if self.buzz is not None:
                found += [s for s, _score in self.buzz.top(20)]
            added = 0
            new_syms: list[str] = []
            for sym in found:
                if (not sym or "." in sym or "-" in sym or "^" in sym
                        or not sym.isalpha() or len(sym) > 5
                        or sym.upper() != sym or sym in self.symbols):
                    continue
                self.symbols[sym] = "discovered"
                self.market.register(sym, "discovered")
                self._discovered.add(sym)
                new_syms.append(sym)
                added += 1
            if added:
                logger.info(
                    "discovery: +%d movers/buzz (%s...)",
                    added, ",".join(sorted(new_syms)[:8]),
                )
            # Baselines: full refresh once/day, incremental for new names.
            self._refresh_baselines()
            if new_syms:
                self._refresh_baselines(only=new_syms)
            # Pull bars for newly discovered names before ranking so
            # _scan_metrics has arrays (otherwise the hot list stays empty
            # until the next discover cycle ~15m later).
            if new_syms:
                try:
                    self._poll_live()
                except Exception:  # noqa: BLE001
                    pass
            self._rank_hot_list()
        except Exception as exc:  # noqa: BLE001
            logger.debug("discovery failed: %s", exc)

    # -- thread loop -----------------------------------------------------------
    def run(self) -> None:
        self.warmup()
        next_discover = 0.0
        while not self.stop_event.is_set():
            start = time.time()
            if market_open_now():
                if self.cfg.discover_movers and start >= next_discover:
                    self._discover_movers()
                    next_discover = start + self.cfg.discover_every_s
                self._poll_live()
                # Re-rank every poll so hunters see fresh ext_vwap / rvol.
                try:
                    self._rank_hot_list()
                except Exception:  # noqa: BLE001
                    logger.debug("hot-list rank failed", exc_info=True)
                wait = self.cfg.equity_bar_poll_s
            else:
                # Closed: idle cheaply; refresh once/hour so history stays warm
                # around the open (pre-market gaps arrive with the first poll).
                wait = 300.0
            elapsed = time.time() - start
            self.stop_event.wait(max(wait - elapsed, 1.0))
        logger.info("equity feed stopped")
