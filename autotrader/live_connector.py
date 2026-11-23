"""Gated live/paper order routing. OFF by default, always.

Order flow only exists behind four independent interlocks, ALL required:

  1. the strategy passed the hard backtest gate (registry ``gate_passed``);
  2. it passed the recent-data paper gate (``paper_trader.is_live_eligible``);
  3. ``AUTOTRADER_LIVE_ENABLE=1`` is set in the environment — a config file
     can never arm live trading by itself;
  4. per-order notional and daily-loss limits are configured and enforced
     on every single order.

Even when armed, equities default to Alpaca's PAPER endpoint unless
``live.alpaca_paper`` is explicitly set false. Broker SDKs (alpaca-py, ccxt)
are imported lazily so the research pipeline runs without them installed.

Every order attempt — accepted or refused — is appended to
``autotrader/state/live_orders.jsonl`` for audit.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autotrader.bootstrap import state_dir
from autotrader.config import PipelineConfig
from autotrader.paper_trader import is_live_eligible

logger = logging.getLogger(__name__)


class InterlockError(RuntimeError):
    """An order was refused by a safety interlock."""


@dataclass
class OrderRequest:
    symbol: str            # e.g. "AAPL" (equity) or "BTC/USDT" (crypto)
    side: str              # "buy" | "sell"
    notional_usd: float
    sleeve: str            # "equities" | "equities_penny" | "crypto"


def _audit_path() -> Path:
    return state_dir() / "live_orders.jsonl"


def _audit(event: dict[str, Any]) -> None:
    event = {"ts": time.time(), **event}
    with _audit_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, default=str) + "\n")


class LiveConnector:
    """Order router for ONE validated strategy. Constructing it does not
    place orders or open network connections; every order re-checks every
    interlock."""

    def __init__(self, cfg: PipelineConfig, gid: str, *, gate_passed: bool):
        self.cfg = cfg
        self.gid = gid
        self.gate_passed = gate_passed
        self._day = dt.date.today()
        self._daily_pnl_usd = 0.0

    # ------------------------------------------------------------------
    # Interlocks
    # ------------------------------------------------------------------
    def check_interlocks(self, order: OrderRequest | None = None) -> None:
        """Raise InterlockError unless every safety condition holds."""
        live = self.cfg.live
        if not self.gate_passed:
            raise InterlockError("strategy has not passed the hard backtest gate")
        if not is_live_eligible(self.gid):
            raise InterlockError("strategy has no fresh PASSING paper-trading record")
        if not live.armed:
            raise InterlockError("AUTOTRADER_LIVE_ENABLE=1 is not set (system is disarmed)")
        if live.max_order_notional_usd <= 0 or live.max_daily_loss_usd <= 0:
            raise InterlockError("order/daily-loss limits are not configured")
        if order is not None:
            if order.notional_usd > live.max_order_notional_usd:
                raise InterlockError(
                    f"order notional ${order.notional_usd:.2f} exceeds the "
                    f"${live.max_order_notional_usd:.2f} per-order cap"
                )
            self._roll_day()
            if -self._daily_pnl_usd >= live.max_daily_loss_usd:
                raise InterlockError(
                    f"daily loss limit ${live.max_daily_loss_usd:.2f} hit; "
                    "no more orders today"
                )

    def _roll_day(self) -> None:
        today = dt.date.today()
        if today != self._day:
            self._day = today
            self._daily_pnl_usd = 0.0

    def record_fill_pnl(self, pnl_usd: float) -> None:
        """Feed realized PnL back so the daily-loss kill switch has data."""
        self._roll_day()
        self._daily_pnl_usd += pnl_usd

    # ------------------------------------------------------------------
    # Order routing
    # ------------------------------------------------------------------
    def place_order(self, order: OrderRequest) -> dict[str, Any]:
        """Submit one order through the interlocks. Raises InterlockError
        (and audits the refusal) if any safety condition fails."""
        try:
            self.check_interlocks(order)
        except InterlockError as exc:
            _audit({"gid": self.gid, "refused": str(exc), "order": vars(order)})
            raise
        if order.sleeve == "crypto":
            receipt = self._place_crypto(order)
        else:
            receipt = self._place_equity(order)
        _audit({"gid": self.gid, "accepted": True, "order": vars(order), "receipt": receipt})
        return receipt

    def _place_equity(self, order: OrderRequest) -> dict[str, Any]:
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
        except ImportError as exc:
            raise InterlockError("alpaca-py is not installed") from exc
        key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            raise InterlockError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        client = TradingClient(key, secret, paper=self.cfg.live.alpaca_paper)
        request = MarketOrderRequest(
            symbol=order.symbol,
            notional=round(order.notional_usd, 2),
            side=OrderSide.BUY if order.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        submitted = client.submit_order(request)
        return {"broker": "alpaca", "paper": self.cfg.live.alpaca_paper,
                "order_id": str(submitted.id), "status": str(submitted.status)}

    def _place_crypto(self, order: OrderRequest) -> dict[str, Any]:
        try:
            import ccxt
        except ImportError as exc:
            raise InterlockError("ccxt is not installed") from exc
        exchange_cls = getattr(ccxt, self.cfg.live.crypto_exchange, None)
        if exchange_cls is None:
            raise InterlockError(f"unknown exchange {self.cfg.live.crypto_exchange!r}")
        api_key = os.environ.get("CRYPTO_API_KEY", "")
        secret = os.environ.get("CRYPTO_SECRET_KEY", "")
        password = os.environ.get("CRYPTO_API_PASSPHRASE", "")
        if not api_key or not secret:
            raise InterlockError("CRYPTO_API_KEY / CRYPTO_SECRET_KEY not set")
        exchange = exchange_cls({
            "apiKey": api_key, "secret": secret, "password": password,
            "enableRateLimit": True,
        })
        ticker = exchange.fetch_ticker(order.symbol)
        price = float(ticker["last"])
        amount = order.notional_usd / price
        placed = exchange.create_order(order.symbol, "market", order.side, amount)
        return {"broker": self.cfg.live.crypto_exchange, "order_id": str(placed.get("id")),
                "amount": amount, "ref_price": price}


def live_status(cfg: PipelineConfig, gid: str, *, gate_passed: bool) -> dict[str, Any]:
    """Non-raising interlock report, for the daemon's track-record output."""
    connector = LiveConnector(cfg, gid, gate_passed=gate_passed)
    try:
        connector.check_interlocks()
        return {"live_eligible": True, "blocked_by": None}
    except InterlockError as exc:
        return {"live_eligible": False, "blocked_by": str(exc)}
