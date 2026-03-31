"""
Execution Engine: Places orders on the exchange with retry logic and error handling.
"""

import asyncio
import logging
from typing import Optional

import ccxt.async_support as ccxt

from .risk_engine import RiskParams

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2.0  # seconds


class ExecutionEngine:
    """
    Wraps ccxt order placement with:
      - Retry on transient errors
      - Paper trading mode (no real orders)
      - SL/TP order attachment
      - Partial fill detection
    """

    def __init__(self, exchange: ccxt.Exchange, config: dict):
        self.exchange = exchange
        self.paper_trading: bool = config.get("paper_trading", True)
        self.live_confirmed: bool = config.get("live_trading_confirmed", False)

        if not self.paper_trading and not self.live_confirmed:
            raise ValueError(
                "Live trading requires both paper_trading=false AND "
                "live_trading_confirmed=true in config.yaml"
            )

        mode = "PAPER" if self.paper_trading else "LIVE"
        logger.warning("ExecutionEngine initialized in %s mode", mode)

    # ── Public API ──────────────────────────────────────────────────────

    async def open_position(self, params: RiskParams) -> Optional[dict]:
        """
        Place a market entry order and attach SL/TP orders.
        Returns a trade dict with all order details, or None on failure.
        """
        if self.paper_trading:
            return self._simulate_fill(params)

        entry_order = await self._place_market_order(
            symbol=params.symbol,
            side=params.side,
            quantity=params.quantity,
        )
        if entry_order is None:
            return None

        # Detect partial fills
        filled_qty = float(entry_order.get("filled", params.quantity))
        avg_price = float(entry_order.get("average") or params.entry_price)

        if filled_qty < params.quantity * 0.99:
            logger.warning(
                "%s: partial fill %.6f / %.6f — adjusting SL/TP accordingly",
                params.symbol, filled_qty, params.quantity,
            )

        # Place SL and TP as limit/stop orders
        sl_order = await self._place_stop_order(params, filled_qty, avg_price)
        tp_order = await self._place_limit_order(params, filled_qty, avg_price)

        return {
            "symbol": params.symbol,
            "side": params.side,
            "entry_order_id": entry_order["id"],
            "sl_order_id": sl_order["id"] if sl_order else None,
            "tp_order_id": tp_order["id"] if tp_order else None,
            "quantity": filled_qty,
            "entry_price": avg_price,
            "stop_loss": params.stop_loss,
            "take_profit": params.take_profit,
            "risk_amount": params.risk_amount,
        }

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        if self.paper_trading:
            return True
        try:
            await self.exchange.cancel_order(order_id, symbol)
            return True
        except ccxt.OrderNotFound:
            logger.debug("%s: order %s already closed/cancelled", symbol, order_id)
            return True
        except Exception as e:
            logger.error("%s: failed to cancel order %s: %s", symbol, order_id, e)
            return False

    async def fetch_order_status(self, symbol: str, order_id: str) -> Optional[dict]:
        """Fetch the current status of an order."""
        if self.paper_trading:
            return {"status": "closed", "filled": 1.0}
        try:
            return await self.exchange.fetch_order(order_id, symbol)
        except Exception as e:
            logger.error("%s: failed to fetch order %s: %s", symbol, order_id, e)
            return None

    async def get_balance(self) -> float:
        """Return available USDT balance."""
        if self.paper_trading:
            return 10_000.0  # simulated paper balance

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                balance = await self.exchange.fetch_balance()
                usdt = balance.get("USDT", {})
                return float(usdt.get("free", 0.0))
            except ccxt.NetworkError as e:
                logger.warning("Balance fetch attempt %d failed: %s", attempt, e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
            except Exception as e:
                logger.error("Unexpected error fetching balance: %s", e)
                break
        return 0.0

    # ── Internal helpers ────────────────────────────────────────────────

    async def _place_market_order(
        self, symbol: str, side: str, quantity: float
    ) -> Optional[dict]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=side,
                    amount=quantity,
                )
                logger.info(
                    "%s: market %s order placed id=%s qty=%.6f",
                    symbol, side, order["id"], quantity,
                )
                return order
            except ccxt.InsufficientFunds as e:
                logger.error("%s: insufficient funds for %s order: %s", symbol, side, e)
                return None
            except ccxt.InvalidOrder as e:
                logger.error("%s: invalid order params: %s", symbol, e)
                return None
            except ccxt.NetworkError as e:
                logger.warning("%s: network error attempt %d: %s", symbol, attempt, e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
            except ccxt.ExchangeError as e:
                logger.error("%s: exchange error placing order: %s", symbol, e)
                return None
            except Exception as e:
                logger.exception("%s: unexpected error placing order: %s", symbol, e)
                return None
        logger.error("%s: exhausted retries placing market order", symbol)
        return None

    async def _place_stop_order(
        self, params: RiskParams, qty: float, entry_price: float
    ) -> Optional[dict]:
        """Place a stop-loss order. Binance uses OCO or stop-market depending on account type."""
        try:
            order = await self.exchange.create_order(
                symbol=params.symbol,
                type="stop_market",
                side="sell" if params.side == "buy" else "buy",
                amount=qty,
                params={"stopPrice": params.stop_loss, "closePosition": False},
            )
            logger.info("%s: SL order placed id=%s at %.4f", params.symbol, order["id"], params.stop_loss)
            return order
        except Exception as e:
            logger.error("%s: failed to place SL order: %s", params.symbol, e)
            return None

    async def _place_limit_order(
        self, params: RiskParams, qty: float, entry_price: float
    ) -> Optional[dict]:
        """Place a take-profit limit order."""
        try:
            order = await self.exchange.create_order(
                symbol=params.symbol,
                type="limit",
                side="sell" if params.side == "buy" else "buy",
                amount=qty,
                price=params.take_profit,
            )
            logger.info("%s: TP order placed id=%s at %.4f", params.symbol, order["id"], params.take_profit)
            return order
        except Exception as e:
            logger.error("%s: failed to place TP order: %s", params.symbol, e)
            return None

    def _simulate_fill(self, params: RiskParams) -> dict:
        """Return a paper-trade fill record (no real order placed)."""
        logger.info(
            "[PAPER] %s %s entry=%.4f SL=%.4f TP=%.4f qty=%.6f",
            params.symbol, params.side.upper(),
            params.entry_price, params.stop_loss,
            params.take_profit, params.quantity,
        )
        return {
            "symbol": params.symbol,
            "side": params.side,
            "entry_order_id": "PAPER",
            "sl_order_id": "PAPER_SL",
            "tp_order_id": "PAPER_TP",
            "quantity": params.quantity,
            "entry_price": params.entry_price,
            "stop_loss": params.stop_loss,
            "take_profit": params.take_profit,
            "risk_amount": params.risk_amount,
            "paper": True,
        }
