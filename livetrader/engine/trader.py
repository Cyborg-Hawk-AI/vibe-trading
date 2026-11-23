"""One live paper trader: an autotrader genome executed against live data.

The SAME genome dict the historical optimizer evolves (strategy_space.py)
drives live behavior here, with identical semantics mapped to intraday
timeframes:

  - cross-sectional momentum ranking picks candidates,
  - RSI band / trend MA / prior-high breakout gates filter entries,
  - ATR trailing stop + RSI exit + max-holding-bars exits,
  - inverse-vol sizing toward a per-position vol budget, max_weight cap,
  - own-equity drawdown circuit breaker with cooldown,
  - small_trade_mode forces many small positions.

Live-only inputs the historical simulator cannot have:
  - news: per-symbol sentiment veto, LLM avoid-list, exposure scalar,
  - macro: risk-off regime halves gross exposure.

Timeframe gene 0/1/2 -> 5m/15m/60m bars (day-trading horizons).
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from livetrader.engine import indicators as ind
from livetrader.engine import journal
from livetrader.feeds.base import MarketData
from livetrader.feeds.equities import market_open_now
from livetrader.news import sentiment as senti
from livetrader.news.rss import NewsStore
from livetrader.sim.broker import PaperBook

NY = ZoneInfo("America/New_York")
EOD_FLAT_ET = dt.time(15, 55)          # all non-crypto books flat by here
EQUITY_ENTRY_CUTOFF_ET = dt.time(15, 40)  # no fresh equity entries into the close
TF_MINUTES = (5, 15, 60)
BARS_PER_YEAR = {5: 19_656, 15: 6_552, 60: 1_638}  # 6.5h US session equiv
MIN_TAIL_BARS = 30


@dataclass
class LiveTrader:
    genome: dict[str, Any]
    gid: str
    book: PaperBook
    sleeve: str = "mixed"                 # which symbol kinds it may trade
    last_bar_seen: dict[str, float] = field(default_factory=dict)
    bars_held: dict[str, int] = field(default_factory=dict)
    cooldown_until: float = 0.0
    evaluations: int = 0
    born_ts: float = field(default_factory=time.time)
    # Hunter desk (sleeve == "sip_hunter") scalp-bracket state
    max_entries_per_day: int = 25
    entries_today: int = 0
    _entries_date: str = ""
    targets: dict[str, float] = field(default_factory=dict)      # take-profit px
    time_stops: dict[str, float] = field(default_factory=dict)   # deadline epoch

    # ------------------------------------------------------------------ helpers
    @property
    def tf_minutes(self) -> int:
        idx = int(self.genome.get("timeframe", 0))
        return TF_MINUTES[max(0, min(idx, len(TF_MINUTES) - 1))]

    def _tradeable(self, market: MarketData, symbol: str) -> bool:
        kind = market.kind(symbol)
        if self.sleeve == "mixed":
            return True
        if self.sleeve == "crypto":
            return kind == "crypto"
        if self.sleeve == "equities":
            return kind in ("equity", "discovered")
        if self.sleeve == "penny":
            return kind in ("penny", "discovered")
        if self.sleeve in ("stocks_in_play", "sip_hunter"):
            return kind == "sip"
        return True

    def _equity_flat_eod(self, market: MarketData, actions: list[str], now: float) -> None:
        """Day-desk discipline: every non-crypto position is flat after 15:55 ET.

        No overnight equity risk in any sleeve; crypto trades 24/7.
        """
        et = dt.datetime.now(tz=NY)
        if et.time() < EOD_FLAT_ET:
            return
        for symbol in list(self.book.positions):
            if market.kind(symbol) == "crypto":
                continue
            pos = self.book.positions[symbol]
            if pos.is_short:
                fill = self.book.cover(
                    market, symbol, reason="session_eod_flat",
                    kind=market.kind(symbol),
                )
            else:
                fill = self.book.sell(market, symbol, reason="session_eod_flat")
            if fill:
                actions.append(
                    f"EOD FLAT {symbol} @{fill.price:.4g} pnl {fill.realized_pnl:+.2f}"
                )
                self.bars_held.pop(symbol, None)
                self.targets.pop(symbol, None)
                self.time_stops.pop(symbol, None)

    def _regime_ok(self, market: MarketData, tf: int, g: dict) -> bool:
        if not int(g.get("use_regime_gate", 0)):
            return True
        period = int(g.get("regime_ma_period", 100))
        for proxy in ("SPY", "QQQ", "BTC-USDT"):
            arrays = market.arrays(proxy, tf_minutes=tf, tail=period + 20)
            if arrays is None or len(arrays["close"]) < period + 2:
                continue
            close = arrays["close"][:-1]
            ma = ind.sma(close, period, (proxy, tf, "regime"))
            return bool(close[-1] > ma)
        return True  # no proxy available → do not block

    # ------------------------------------------------------------------ evaluate
    def evaluate(
        self,
        market: MarketData,
        news: NewsStore,
        *,
        regime: str = "neutral",
        now: float | None = None,
    ) -> list[str]:
        """One evaluation tick; journals every sell fill it produced."""
        n_fills = len(self.book.fills)
        try:
            return self._evaluate(market, news, regime=regime, now=now)
        finally:
            self._journal_exits(n_fills, market, news)

    def _journal_exits(self, n_before: int, market: MarketData, news: NewsStore) -> None:
        start = min(n_before, len(self.book.fills))
        for fill in self.book.fills[start:]:
            if fill.side not in ("sell", "cover"):
                continue
            journal.record_closed_trade(
                fill, gid=self.gid, sleeve=self.sleeve,
                kind=market.kind(fill.symbol), news=news,
            )

    # ------------------------------------------------------------------ hunter path
    def _hunter_brackets(self) -> tuple[float, float, float]:
        """(target_pct, stop_pct, time_stop_s) derived from existing genes.

        No new GENE_SPACE keys (extra genome keys would break ``_repair``);
        the hunter bracket is a deterministic mapping of genes the GA already
        tunes: atr_stop_mult -> take-profit 5-8%, dd_breaker_pct -> hard stop
        3-4%, max_holding_bars -> time stop 15-40 minutes.
        """
        g = self.genome
        atr_mult = float(g.get("atr_stop_mult", 3.0))
        target_pct = min(max(0.05 + 0.03 * (atr_mult - 1.5) / 3.5, 0.05), 0.08)
        dd = float(g.get("dd_breaker_pct", 0.07))
        stop_pct = min(max(0.03 + 0.01 * (dd - 0.05) / 0.05, 0.03), 0.04)
        hold = int(g.get("max_holding_bars", 6))
        time_stop_s = float(min(max(hold, 3), 8) * 300)   # 15-40 min
        return target_pct, stop_pct, time_stop_s

    def _hunter_cfg(self) -> dict:
        """Read discipline knobs from arena cfg attached at birth, with defaults."""
        cfg = getattr(self, "arena_cfg", None)
        return {
            "require_buzz": bool(getattr(cfg, "hunter_require_buzz", True)),
            "max_ext_pct": float(getattr(cfg, "hunter_max_ext_pct", 0.06)),
            "prime_end": str(getattr(cfg, "hunter_prime_end_et", "11:30")),
            "max_per_symbol": int(getattr(cfg, "hunter_max_per_symbol", 5)),
            "enable_short": bool(getattr(cfg, "hunter_enable_short", True)),
            "use_edge": bool(getattr(cfg, "hunter_use_edge_model", True)),
            "lost_vwap_exit": bool(getattr(cfg, "hunter_lost_vwap_exit", True)),
        }

    @staticmethod
    def _parse_hhmm(s: str) -> dt.time:
        try:
            hh, mm = s.split(":")
            return dt.time(int(hh), int(mm))
        except (ValueError, AttributeError):
            return dt.time(11, 30)

    def _close_hunter_pos(
        self, market: MarketData, symbol: str, reason: str, actions: list[str],
    ) -> None:
        pos = self.book.positions.get(symbol)
        if pos is None:
            return
        if pos.is_short:
            fill = self.book.cover(
                market, symbol, reason=reason, kind=market.kind(symbol),
            )
        else:
            fill = self.book.sell(market, symbol, reason=reason)
        if fill:
            actions.append(
                f"{reason.upper()} {symbol} @{fill.price:.4g} "
                f"pnl {fill.realized_pnl:+.2f}"
            )
        self.targets.pop(symbol, None)
        self.time_stops.pop(symbol, None)
        self.bars_held.pop(symbol, None)

    def _evaluate_hunter(
        self, market: MarketData, news: NewsStore, now: float
    ) -> list[str]:
        """Momentum-spike / fade path with discipline gates + optional edge model.

        Long: hot-list break of 30-min high on 2x volume, buzz/catalyst required,
        not extended above VWAP, prime-hours preferred. Short: extended name
        making a lower high with fading volume. Exits: take-profit, hard stop,
        time stop, lost-VWAP (longs), or session EOD flat.
        """
        from livetrader.engine import snapshots as snaplog

        actions: list[str] = []
        equity = self.book.mark(market)
        self._equity_flat_eod(market, actions, now)
        hcfg = self._hunter_cfg()

        et_now = dt.datetime.now(tz=NY)
        today = et_now.strftime("%Y-%m-%d")
        if self._entries_date != today:
            self._entries_date = today
            self.entries_today = 0

        if self.book.peak_equity > 0:
            dd = 1.0 - equity / self.book.peak_equity
            if dd > float(self.genome.get("dd_breaker_pct", 0.08)) and now >= self.cooldown_until:
                n = self.book.liquidate(market, reason="dd_breaker")
                self.cooldown_until = now + 1800.0
                if n:
                    actions.append(f"BREAKER flat {n} positions (dd {dd:.1%})")
                return actions
        if now < self.cooldown_until:
            return actions
        if not market_open_now():
            return actions

        target_pct, stop_pct, time_stop_s = self._hunter_brackets()
        prime_end = self._parse_hhmm(hcfg["prime_end"])
        in_prime = et_now.time() < prime_end

        # --- bracket exits on every tick --------------------------------------
        for symbol in list(self.book.positions):
            pos = self.book.positions[symbol]
            price = market.price(symbol)
            if price is None:
                continue
            reason = ""
            if pos.is_short:
                if pos.stop > 0 and price >= pos.stop:
                    reason = "hard_stop"
                elif price <= self.targets.get(symbol, -float("inf")):
                    reason = "take_profit"
            else:
                if pos.stop > 0 and price <= pos.stop:
                    reason = "hard_stop"
                elif price >= self.targets.get(symbol, float("inf")):
                    reason = "take_profit"
                elif hcfg["lost_vwap_exit"] and price > pos.avg_cost:
                    meta = market.hot_meta(symbol)
                    vwap = float(meta.get("vwap", 0.0) or 0.0)
                    if vwap > 0 and price < vwap:
                        reason = "lost_vwap"
            if not reason and now >= self.time_stops.get(symbol, float("inf")):
                reason = "time_stop"
            if reason:
                self._close_hunter_pos(market, symbol, reason, actions)

        if self.book.positions:
            return actions
        if et_now.time() >= EQUITY_ENTRY_CUTOFF_ET:
            return actions
        if self.entries_today >= int(self.max_entries_per_day):
            return actions

        tf = self.tf_minutes
        spike_bars = max(30 // tf, 2)
        blocked = senti.blocked_symbols(news)
        held_counts = getattr(self, "_cohort_held", {}) or {}
        max_per = hcfg["max_per_symbol"]

        long_cands: list[tuple[float, str, dict]] = []
        short_cands: list[tuple[float, str, dict]] = []

        for symbol in market.symbols("sip"):
            meta = dict(market.hot_meta(symbol) or {})
            if not meta:
                continue
            bare = symbol.split("-")[0]
            if bare in blocked or symbol in blocked:
                continue
            if held_counts.get(symbol, 0) >= max_per:
                arrays_skip = market.arrays(symbol, tf_minutes=tf, tail=5)
                bar_ts = float(arrays_skip["ts"][-1]) if arrays_skip is not None else now
                snaplog.record_setup(
                    symbol, meta, bar_ts=bar_ts, taken=False,
                    skip_reason="pile_on",
                )
                continue

            arrays = market.arrays(symbol, tf_minutes=tf, tail=spike_bars + 20)
            self.evaluations += 1
            if arrays is None or len(arrays["close"]) < spike_bars + 3:
                continue
            last_ts = float(arrays["ts"][-1])
            if self.last_bar_seen.get(symbol, 0.0) >= last_ts:
                continue
            self.last_bar_seen[symbol] = last_ts
            close = arrays["close"][:-1]
            high = arrays["high"][:-1]
            volume = arrays.get("volume", np.ones_like(arrays["close"]))[:-1]
            key = (symbol, tf, float(arrays["ts"][-2]), "hunter")
            mom5 = float(close[-1] / close[-min(len(close), max(5 // tf, 1)) - 1] - 1.0) if len(close) > 2 else 0.0
            extras = {
                "mom_5m": round(mom5, 4),
                "minutes_open": round(
                    (et_now - et_now.replace(hour=9, minute=30, second=0, microsecond=0)
                     ).total_seconds() / 60.0, 1
                ),
            }

            # Discipline: buzz/catalyst gate
            has_buzz = float(meta.get("buzz", 0.0) or 0.0) > 0.0 or bool(meta.get("catalyst"))
            if hcfg["require_buzz"] and not has_buzz:
                snaplog.record_setup(
                    symbol, meta, bar_ts=last_ts, taken=False,
                    skip_reason="no_buzz", extras=extras,
                )
                continue

            ext = float(meta.get("ext_vwap", 0.0) or 0.0)
            after_prime_ok = in_prime or bool(meta.get("catalyst"))

            # Long spike: break of 30-min high + volume, not extended
            long_ok = (
                close[-1] > ind.prior_high(high, spike_bars, key)
                and ind.volume_ok(volume, 2.0, key=key)
                and ext <= hcfg["max_ext_pct"]
                and after_prime_ok
            )
            if long_ok:
                long_cands.append((float(meta.get("score", 0.0)), symbol, meta))
            elif ext > hcfg["max_ext_pct"] and close[-1] > ind.prior_high(high, spike_bars, key):
                snaplog.record_setup(
                    symbol, meta, bar_ts=last_ts, taken=False,
                    skip_reason="extended", extras=extras,
                )

            # Short fade: extended + lower high + fading volume
            if hcfg["enable_short"] and after_prime_ok:
                ph = ind.prior_high(high, spike_bars, key)
                vol_fade = (
                    len(volume) >= 5
                    and float(volume[-1]) < float(np.median(volume[-6:-1])) * 0.8
                )
                if ext > hcfg["max_ext_pct"] and close[-1] < ph and vol_fade:
                    short_cands.append((float(meta.get("score", 0.0)), symbol, meta))
                elif not long_ok:
                    snaplog.record_setup(
                        symbol, meta, bar_ts=last_ts, taken=False,
                        skip_reason="no_trigger", side="short", extras=extras,
                    )

        # Prefer longs in prime hours; allow shorts when extended names dominate.
        side = "long"
        candidates = long_cands
        if not candidates and short_cands:
            side = "short"
            candidates = short_cands
        if not candidates:
            return actions
        candidates.sort(reverse=True, key=lambda c: c[0])
        _score, symbol, meta = candidates[0]
        price = market.price(symbol)
        if price is None or price <= 0:
            return actions

        # Edge model EV gate + sizing (falls back to flat 90% when no model).
        size_frac = 0.9
        p_hat = None
        if hcfg["use_edge"]:
            try:
                from autotrader.edge_model import predict_edge

                features = {
                    "gap": float(meta.get("gap", 0.0) or 0.0),
                    "rvol": float(meta.get("rvol", 0.0) or 0.0),
                    "buzz": float(meta.get("buzz", 0.0) or 0.0),
                    "catalyst": 1.0 if meta.get("catalyst") else 0.0,
                    "ext_vwap": float(meta.get("ext_vwap", 0.0) or 0.0),
                    "minutes_open": float(
                        (et_now - et_now.replace(hour=9, minute=30, second=0, microsecond=0)
                         ).total_seconds() / 60.0
                    ),
                    "side_short": 1.0 if side == "short" else 0.0,
                    "buzz_lead_min": float(meta.get("buzz_lead_min", 999.0) or 999.0),
                }
                try:
                    from autotrader.edge_features import arch_onehots

                    features.update(arch_onehots(str(meta.get("arch", "chop"))))
                except Exception:  # noqa: BLE001 — one-hots default to 0
                    pass
                pred = predict_edge(features, target_pct=target_pct, stop_pct=stop_pct)
                if pred is not None:
                    p_hat, ev, size_frac = pred
                    if ev <= 0:
                        snaplog.record_setup(
                            symbol, meta, bar_ts=now, taken=False,
                            skip_reason="neg_ev", side=side,
                            extras={"p_hat": p_hat, "ev": ev},
                        )
                        return actions
                    # After prime hours: require positive EV (already gated) + catalyst
                    if not in_prime and not meta.get("catalyst"):
                        snaplog.record_setup(
                            symbol, meta, bar_ts=now, taken=False,
                            skip_reason="after_prime", side=side,
                        )
                        return actions
            except Exception:  # noqa: BLE001 — model is optional enrichment
                size_frac = 0.9

        notional = float(size_frac) * self.book.equity(market)
        srcs = "+".join(meta.get("sources", [])) or "scan"
        p_str = f" p={p_hat:.2f}" if p_hat is not None else ""
        reason = (
            f"{'fade' if side == 'short' else 'spike'} buzz={meta.get('buzz', 0.0):.1f} "
            f"gap={meta.get('gap', 0.0):+.1%} rvol={meta.get('rvol', 0.0):.1f} "
            f"ext={meta.get('ext_vwap', 0.0):+.1%} src={srcs}"
            f"{' cat' if meta.get('catalyst') else ''}{p_str}"
        )

        if side == "short":
            fill = self.book.sell_short(
                market, symbol, notional,
                stop=price * (1.0 + stop_pct), reason=reason,
                kind=market.kind(symbol),
            )
            if fill:
                self.targets[symbol] = fill.price * (1.0 - target_pct)
        else:
            fill = self.book.buy(
                market, symbol, notional,
                stop=price * (1.0 - stop_pct), reason=reason,
            )
            if fill:
                self.targets[symbol] = fill.price * (1.0 + target_pct)

        if fill:
            self.entries_today += 1
            self.time_stops[symbol] = now + time_stop_s
            self.bars_held[symbol] = 0
            snaplog.record_setup(
                symbol, meta, bar_ts=now, taken=True, side=side,
                extras={"p_hat": p_hat, "size_frac": size_frac},
            )
            verb = "SHORT" if side == "short" else "BUY"
            actions.append(
                f"{verb} {symbol} {fill.qty:.6g} @{fill.price:.4g} "
                f"(tp {target_pct:.1%} sl {stop_pct:.1%} "
                f"ts {time_stop_s / 60:.0f}m #{self.entries_today}) {reason}"
            )
        return actions

    def _evaluate(
        self,
        market: MarketData,
        news: NewsStore,
        *,
        regime: str = "neutral",
        now: float | None = None,
    ) -> list[str]:
        """One evaluation tick. Returns human-readable action strings."""
        g = self.genome
        now = now or time.time()
        if self.sleeve == "sip_hunter":
            return self._evaluate_hunter(market, news, now)
        tf = self.tf_minutes
        actions: list[str] = []

        equity = self.book.mark(market)
        self._equity_flat_eod(market, actions, now)

        # --- own-equity circuit breaker ---------------------------------------
        if self.book.peak_equity > 0:
            dd = 1.0 - equity / self.book.peak_equity
            if dd > float(g["dd_breaker_pct"]) and now >= self.cooldown_until:
                n = self.book.liquidate(market, reason="dd_breaker")
                self.cooldown_until = now + float(g["dd_cooldown_bars"]) * tf * 60
                if n:
                    actions.append(f"BREAKER flat {n} positions (dd {dd:.1%})")
                return actions
        if now < self.cooldown_until:
            return actions

        # Equity quotes freeze outside regular hours; trading them then would
        # fill against stale prices with fictional liquidity. Crypto is 24/7.
        equities_open = market_open_now()

        # --- stop checks on every tick (live price, not bar close) -------------
        for symbol in list(self.book.positions):
            if not equities_open and market.kind(symbol) != "crypto":
                continue
            pos = self.book.positions[symbol]
            price = market.price(symbol)
            if price is None:
                continue
            if pos.stop > 0 and price <= pos.stop:
                fill = self.book.sell(market, symbol, reason="atr_stop")
                if fill:
                    actions.append(f"STOP {symbol} @{fill.price:.4g} pnl {fill.realized_pnl:+.2f}")

        # --- bar-close logic per symbol ----------------------------------------
        candidates: list[tuple[float, str, dict]] = []
        blocked = senti.blocked_symbols(news)
        needed = max(
            int(g["momentum_lookback"]) + int(g.get("mom_skip_bars", 0)),
            int(g["trend_ma_period"]),
            int(g["vol_lookback"]),
            int(g["breakout_lookback"]),
            int(g.get("regime_ma_period", 50)) if int(g.get("use_regime_gate", 0)) else 0,
            int(g.get("compress_lookback", 10)) if int(g.get("use_range_compress", 0)) else 0,
            MIN_TAIL_BARS,
        ) + 5

        if not self._regime_ok(market, tf, g):
            return actions

        et_now = dt.datetime.now(tz=NY).time()
        for symbol in market.symbols():
            if not self._tradeable(market, symbol):
                continue
            if not equities_open and market.kind(symbol) != "crypto":
                continue
            # Day desk: no fresh equity entries into the close (they would be
            # force-flattened minutes later at 15:55 ET anyway).
            no_new_equity_entry = (
                market.kind(symbol) != "crypto" and et_now >= EQUITY_ENTRY_CUTOFF_ET
            )
            if no_new_equity_entry and symbol not in self.book.positions:
                continue
            arrays = market.arrays(symbol, tf_minutes=tf, tail=needed + 10)
            self.evaluations += 1
            if arrays is None or len(arrays["close"]) < max(needed, MIN_TAIL_BARS):
                continue
            last_ts = float(arrays["ts"][-1])
            new_bar = self.last_bar_seen.get(symbol, 0.0) < last_ts
            if new_bar:
                self.last_bar_seen[symbol] = last_ts
            # completed bars only for indicator math
            close = arrays["close"][:-1]
            high = arrays["high"][:-1]
            low = arrays["low"][:-1]
            volume = arrays.get("volume", np.ones_like(close))[:-1]
            if len(close) < needed:
                continue
            key = (symbol, tf, float(arrays["ts"][-2]))

            pos = self.book.positions.get(symbol)
            if pos is not None and new_bar:
                self.bars_held[symbol] = self.bars_held.get(symbol, 0) + 1
                held = self.bars_held[symbol]
                # trail the ATR stop upward on each completed bar
                a = ind.atr(high, low, close, int(g["atr_period"]), key)
                if a > 0:
                    pos.stop = max(pos.stop, close[-1] - float(g["atr_stop_mult"]) * a)
                exit_now = False
                if held >= int(g.get("min_holding_bars", 0)):
                    r = ind.rsi(close, int(g["rsi_period"]), key)
                    if r >= float(g["rsi_exit"]) or held >= int(g["max_holding_bars"]):
                        exit_now = True
                if exit_now:
                    fill = self.book.sell(market, symbol, reason="rule_exit")
                    if fill:
                        actions.append(
                            f"EXIT {symbol} @{fill.price:.4g} pnl {fill.realized_pnl:+.2f}"
                        )
                    self.bars_held.pop(symbol, None)
                continue

            if pos is not None or not new_bar:
                continue

            # --- entry gates on the freshly completed bar ----------------------
            bare = symbol.split("-")[0]
            if bare in blocked or symbol in blocked:
                continue
            if news.symbol_sentiment(bare) < -0.5:
                continue  # hard news veto on strongly negative names
            mom = ind.momentum_skip(
                close, int(g["momentum_lookback"]), int(g.get("mom_skip_bars", 0)), key
            )
            if mom <= 0:
                continue
            if int(g["use_rsi_gate"]):
                r = ind.rsi(close, int(g["rsi_period"]), key)
                if not (float(g["rsi_entry_low"]) <= r <= float(g["rsi_entry_high"])):
                    continue
            ma = ind.sma(close, int(g["trend_ma_period"]), key)
            if int(g["use_trend_gate"]):
                if close[-1] <= ma * (1.0 + float(g["vwap_offset_pct"])):
                    continue
            if int(g["use_breakout_gate"]):
                if close[-1] <= ind.prior_high(high, int(g["breakout_lookback"]), key):
                    continue
            if int(g.get("use_volume_gate", 0)):
                if not ind.volume_ok(volume, float(g.get("volume_mult", 1.5)), key=key):
                    continue
            if int(g.get("use_range_compress", 0)):
                if not ind.range_compressed(high, low, int(g.get("compress_lookback", 20)), key):
                    continue
            if int(g.get("use_pullback_entry", 0)):
                if not ind.pullback_ok(close, ma, key):
                    continue
            # SIP sleeve: ORB breakout + optional catalyst
            if self.sleeve == "stocks_in_play":
                ts = arrays["ts"][:-1]
                orb = ind.opening_range(high, low, ts, orb_minutes=5)
                if orb is None:
                    continue
                orb_high, _orb_low = orb
                if close[-1] <= orb_high:
                    continue
                if not ind.volume_ok(volume, 2.0, key=key):
                    continue
                if news.has_catalyst(bare) is False and news.has_catalyst(symbol) is False:
                    # Soft preference: allow without catalyst but down-rank
                    mom *= 0.5
            candidates.append((mom, symbol, {"close": close, "high": high, "low": low,
                                             "key": key}))

        # --- rank + size entries ------------------------------------------------
        if not candidates:
            return actions
        candidates.sort(reverse=True, key=lambda c: c[0])
        max_pos = int(g["max_positions"])
        room = max_pos - len(self.book.positions)
        if room <= 0:
            return actions

        scale = senti.exposure_scalar(news, self.sleeve)
        if regime == "risk_off":
            scale *= 0.5
        tf_bars_year = BARS_PER_YEAR[self.tf_minutes]

        for mom, symbol, ctx in candidates[:room]:
            close, high, low, key = ctx["close"], ctx["high"], ctx["low"], ctx["key"]
            vol = ind.realized_vol(close, int(g["vol_lookback"]), tf_bars_year, key)
            a = ind.atr(high, low, close, int(g["atr_period"]), key)
            if vol <= 1e-8 or a <= 0:
                continue
            per_pos_target = float(g["target_vol_annual"]) / max(max_pos, 1)
            weight = min(max(per_pos_target / vol, 0.0), float(g["max_weight"]))
            notional = self.book.equity(market) * weight * scale
            price = market.price(symbol)
            if price is None or notional < 1.0:
                continue
            stop = price - float(g["atr_stop_mult"]) * a
            fill = self.book.buy(market, symbol, notional, stop=stop,
                                 reason=f"mom={mom:.3f}")
            if fill:
                self.bars_held[symbol] = 0
                actions.append(f"BUY {symbol} {fill.qty:.6g} @{fill.price:.4g} (w={weight:.3f})")
        return actions

    # ------------------------------------------------------------------ snapshot
    def snapshot(self, market: MarketData) -> dict:
        return {
            "gid": self.gid,
            "sleeve": self.sleeve,
            "timeframe_min": self.tf_minutes,
            "genome": self.genome,
            "evaluations": self.evaluations,
            **self.book.stats(market),
        }
