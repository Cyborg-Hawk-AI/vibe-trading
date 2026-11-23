"""Paper broker: realistic simulated fills against live quotes.

Each arena trader owns one ``PaperBook``. Fills always pay the full friction
stack — half-spread (crossing), slippage, and fees — so the simulation can
only be *pessimistic* relative to a patient real execution:

    buy  fill = ask * (1 + slippage) ;  cost += fee
    sell fill = bid * (1 - slippage) ;  cost += fee

Crypto quotes carry a real live bid/ask (Binance bookTicker); equity quotes
carry a synthetic spread by price tier (see feeds.equities). No leverage, no
negative cash. Shorts are hunter-only (see ``sell_short`` / ``cover``) with
punitive borrow fees on cheap / SIP names.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from livetrader.config import SimConfig
from livetrader.feeds.base import MarketData


@dataclass
class Position:
    qty: float = 0.0            # >0 long, <0 short
    avg_cost: float = 0.0
    opened_ts: float = 0.0
    stop: float = 0.0           # long: floor; short: ceiling
    bars_held: int = 0
    entry_reason: str = ""
    is_short: bool = False


@dataclass
class Fill:
    ts: float
    symbol: str
    side: str            # "buy" | "sell" | "sell_short" | "cover"
    qty: float
    price: float
    fee: float
    reason: str
    realized_pnl: float = 0.0
    entry_price: float = 0.0   # closes only: avg cost of the closed position
    entry_reason: str = ""     # closes only: why the position was opened
    held_s: float = 0.0        # closes only: seconds the position was held
    position_side: str = "long"  # "long" | "short" — for journal/scoreboard

    def to_dict(self) -> dict:
        return {
            "ts": round(self.ts, 1), "symbol": self.symbol, "side": self.side,
            "qty": round(self.qty, 8), "price": round(self.price, 8),
            "fee": round(self.fee, 6), "reason": self.reason,
            "realized_pnl": round(self.realized_pnl, 6),
            "entry_price": round(self.entry_price, 8),
            "entry_reason": self.entry_reason,
            "held_s": round(self.held_s, 1),
            "position_side": self.position_side,
        }


@dataclass
class PaperBook:
    """One trader's simulated account."""

    cash: float
    sim: SimConfig
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    realized_pnl: float = 0.0
    peak_equity: float = 0.0
    max_drawdown: float = 0.0
    wins: int = 0
    losses: int = 0
    created_ts: float = field(default_factory=time.time)
    starting_cash: float = 0.0

    def __post_init__(self) -> None:
        self.starting_cash = self.cash
        self.peak_equity = self.cash

    def _borrow_bps(self, price: float, *, kind: str = "") -> float:
        """Punitive borrow fee for shorts on cheap / SIP names."""
        if price <= float(getattr(self.sim, "short_borrow_price_max", 5.0)):
            return float(getattr(self.sim, "short_borrow_bps", 30.0))
        if kind == "sip":
            return float(getattr(self.sim, "short_borrow_bps", 30.0))
        return 0.0

    # -- valuation -----------------------------------------------------------
    def equity(self, market: MarketData) -> float:
        value = self.cash
        for symbol, pos in self.positions.items():
            price = market.price(symbol) or pos.avg_cost
            # long: +qty*price; short: +qty*price with qty < 0 (liability)
            value += pos.qty * price
        return value

    def mark(self, market: MarketData) -> float:
        eq = self.equity(market)
        self.peak_equity = max(self.peak_equity, eq)
        if self.peak_equity > 0:
            self.max_drawdown = max(
                self.max_drawdown, 1.0 - eq / self.peak_equity
            )
        return eq

    # -- order execution -------------------------------------------------------
    def buy(self, market: MarketData, symbol: str, notional: float, *,
            stop: float = 0.0, reason: str = "") -> Fill | None:
        if symbol in self.positions and self.positions[symbol].is_short:
            return None  # cover via cover(); don't flip in one call
        quote = market.quote(symbol)
        if quote is None or quote.ask <= 0:
            return None
        notional = min(notional, self.cash * 0.98)
        if notional < 1.0:  # dust guard
            return None
        price = quote.ask * (1.0 + self.sim.slippage_bps / 1e4)
        fee = notional * self.sim.fee_bps / 1e4
        qty = (notional - fee) / price
        if qty <= 0:
            return None
        self.cash -= notional
        pos = self.positions.get(symbol)
        if pos is None:
            self.positions[symbol] = Position(
                qty=qty, avg_cost=price, opened_ts=time.time(),
                stop=stop, entry_reason=reason, is_short=False,
            )
        else:
            total_cost = pos.avg_cost * pos.qty + price * qty
            pos.qty += qty
            pos.avg_cost = total_cost / pos.qty
            pos.stop = max(pos.stop, stop)
        fill = Fill(ts=time.time(), symbol=symbol, side="buy", qty=qty,
                    price=price, fee=fee, reason=reason, position_side="long")
        self.fills.append(fill)
        return fill

    def sell(self, market: MarketData, symbol: str, *, reason: str = "") -> Fill | None:
        pos = self.positions.get(symbol)
        if pos is None or pos.qty <= 0 or pos.is_short:
            return None
        quote = market.quote(symbol)
        if quote is None or quote.bid <= 0:
            return None
        price = quote.bid * (1.0 - self.sim.slippage_bps / 1e4)
        gross = pos.qty * price
        fee = gross * self.sim.fee_bps / 1e4
        pnl = gross - fee - pos.qty * pos.avg_cost
        self.cash += gross - fee
        self.realized_pnl += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
        fill = Fill(ts=time.time(), symbol=symbol, side="sell", qty=pos.qty,
                    price=price, fee=fee, reason=reason, realized_pnl=pnl,
                    entry_price=pos.avg_cost, entry_reason=pos.entry_reason,
                    held_s=time.time() - pos.opened_ts, position_side="long")
        self.fills.append(fill)
        del self.positions[symbol]
        if len(self.fills) > 5000:
            self.fills = self.fills[-2500:]
        return fill

    def sell_short(
        self,
        market: MarketData,
        symbol: str,
        notional: float,
        *,
        stop: float = 0.0,
        reason: str = "",
        kind: str = "",
    ) -> Fill | None:
        """Open a short: proceeds + cash, qty negative, no leverage."""
        if symbol in self.positions:
            return None  # no flip / add in one call
        quote = market.quote(symbol)
        if quote is None or quote.bid <= 0:
            return None
        # Cap short notional by free cash (no leverage).
        notional = min(notional, self.cash * 0.98)
        if notional < 1.0:
            return None
        price = quote.bid * (1.0 - self.sim.slippage_bps / 1e4)
        fee_bps = self.sim.fee_bps + self._borrow_bps(price, kind=kind)
        fee = notional * fee_bps / 1e4
        qty = notional / price
        if qty <= 0:
            return None
        # Proceeds credited; fee paid; cash holds the proceeds as margin.
        self.cash += notional - fee
        self.positions[symbol] = Position(
            qty=-qty, avg_cost=price, opened_ts=time.time(),
            stop=stop, entry_reason=reason, is_short=True,
        )
        fill = Fill(ts=time.time(), symbol=symbol, side="sell_short", qty=qty,
                    price=price, fee=fee, reason=reason, position_side="short")
        self.fills.append(fill)
        return fill

    def cover(self, market: MarketData, symbol: str, *, reason: str = "",
              kind: str = "") -> Fill | None:
        """Close a short by buying to cover."""
        pos = self.positions.get(symbol)
        if pos is None or not pos.is_short or pos.qty >= 0:
            return None
        quote = market.quote(symbol)
        if quote is None or quote.ask <= 0:
            return None
        price = quote.ask * (1.0 + self.sim.slippage_bps / 1e4)
        qty = abs(pos.qty)
        cost = qty * price
        fee_bps = self.sim.fee_bps + self._borrow_bps(price, kind=kind)
        fee = cost * fee_bps / 1e4
        # Short P&L: entry proceeds - cover cost - fees.
        pnl = qty * pos.avg_cost - cost - fee
        self.cash -= cost + fee
        self.realized_pnl += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
        fill = Fill(ts=time.time(), symbol=symbol, side="cover", qty=qty,
                    price=price, fee=fee, reason=reason, realized_pnl=pnl,
                    entry_price=pos.avg_cost, entry_reason=pos.entry_reason,
                    held_s=time.time() - pos.opened_ts, position_side="short")
        self.fills.append(fill)
        del self.positions[symbol]
        if len(self.fills) > 5000:
            self.fills = self.fills[-2500:]
        return fill

    def liquidate(self, market: MarketData, *, reason: str = "liquidate") -> int:
        count = 0
        for symbol in list(self.positions):
            pos = self.positions[symbol]
            if pos.is_short:
                if self.cover(market, symbol, reason=reason,
                              kind=market.kind(symbol)):
                    count += 1
            else:
                if self.sell(market, symbol, reason=reason):
                    count += 1
        return count

    # -- stats -----------------------------------------------------------------
    def stats(self, market: MarketData) -> dict:
        eq = self.equity(market)
        trades = self.wins + self.losses
        return {
            "equity": round(eq, 4),
            "cash": round(self.cash, 4),
            "return": round(eq / self.starting_cash - 1.0, 6) if self.starting_cash else 0.0,
            "realized_pnl": round(self.realized_pnl, 4),
            "max_drawdown": round(self.max_drawdown, 6),
            "open_positions": len(self.positions),
            "closed_trades": trades,
            "win_rate": round(self.wins / trades, 4) if trades else 0.0,
            "age_s": round(time.time() - self.created_ts, 0),
        }
