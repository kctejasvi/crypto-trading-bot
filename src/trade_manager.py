"""
Trade Manager: Tracks open positions, updates trailing stops, and triggers exits.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .execution_engine import ExecutionEngine
from .risk_engine import RiskEngine

logger = logging.getLogger(__name__)


@dataclass
class OpenTrade:
    symbol: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    risk_amount: float
    entry_order_id: str
    sl_order_id: Optional[str]
    tp_order_id: Optional[str]
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    paper: bool = False

    # Trailing stop tracking
    trail_activated: bool = False
    highest_price: float = 0.0   # for long trades
    lowest_price: float = float("inf")  # for short trades

    @property
    def is_paper(self) -> bool:
        return self.paper

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "buy":
            return (current_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - current_price) * self.quantity


class TradeManager:
    """
    Manages all open trades:
      - Stores and retrieves open positions
      - Implements trailing stop logic (activates at 1x ATR profit, trails at 0.5x ATR)
      - Polls SL/TP order status for non-paper trades
      - Triggers manual exits when price violates SL/TP in paper mode
    """

    def __init__(
        self,
        execution: ExecutionEngine,
        risk: RiskEngine,
        config: dict,
    ):
        self.execution = execution
        self.risk = risk
        self._trades: Dict[str, OpenTrade] = {}  # symbol -> OpenTrade
        self.trail_activation_multiplier = 1.0   # activate after 1x ATR gain
        self.trail_distance_multiplier = 0.5     # trail at 0.5x ATR below peak

    # ── Open/Close ──────────────────────────────────────────────────────

    def open_trade(self, order_result: dict, atr: float) -> OpenTrade:
        """Register a new trade after successful order execution."""
        trade = OpenTrade(
            symbol=order_result["symbol"],
            side=order_result["side"],
            entry_price=order_result["entry_price"],
            stop_loss=order_result["stop_loss"],
            take_profit=order_result["take_profit"],
            quantity=order_result["quantity"],
            risk_amount=order_result["risk_amount"],
            entry_order_id=order_result["entry_order_id"],
            sl_order_id=order_result.get("sl_order_id"),
            tp_order_id=order_result.get("tp_order_id"),
            paper=order_result.get("paper", False),
            highest_price=order_result["entry_price"],
            lowest_price=order_result["entry_price"],
        )
        self._trades[trade.symbol] = trade
        self.risk.record_trade_open()
        logger.info(
            "Trade OPENED: %s %s qty=%.6f entry=%.4f SL=%.4f TP=%.4f",
            trade.symbol, trade.side.upper(),
            trade.quantity, trade.entry_price,
            trade.stop_loss, trade.take_profit,
        )
        return trade

    async def close_trade(
        self,
        symbol: str,
        exit_price: float,
        reason: str,
    ) -> Optional[dict]:
        """Close a trade and record results."""
        trade = self._trades.pop(symbol, None)
        if trade is None:
            logger.warning("close_trade called for unknown symbol %s", symbol)
            return None

        pnl = trade.unrealized_pnl(exit_price)
        self.risk.record_trade_close(pnl)

        result = {
            "symbol": trade.symbol,
            "side": trade.side,
            "entry_price": trade.entry_price,
            "exit_price": exit_price,
            "quantity": trade.quantity,
            "pnl": round(pnl, 4),
            "reason": reason,
            "opened_at": trade.opened_at.isoformat(),
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "paper": trade.paper,
        }

        # Cancel any pending SL/TP orders
        if not trade.paper:
            for oid in [trade.sl_order_id, trade.tp_order_id]:
                if oid:
                    await self.execution.cancel_order(symbol, oid)

        logger.info(
            "Trade CLOSED: %s reason=%s pnl=%.4f USDT",
            symbol, reason, pnl,
        )
        return result

    # ── State queries ───────────────────────────────────────────────────

    def has_open_trade(self, symbol: str) -> bool:
        return symbol in self._trades

    def get_trade(self, symbol: str) -> Optional[OpenTrade]:
        return self._trades.get(symbol)

    def all_trades(self) -> List[OpenTrade]:
        return list(self._trades.values())

    # ── Update loop (called each scan cycle) ────────────────────────────

    async def update(self, symbol: str, current_price: float, atr: float) -> Optional[dict]:
        """
        Check if trade should be exited (paper) or if SL/TP orders were hit (live).
        Also update trailing stop.
        Returns a close-result dict if trade was closed, else None.
        """
        trade = self._trades.get(symbol)
        if trade is None:
            return None

        # ── Paper mode: manually check SL/TP ────────────────────────────
        if trade.paper:
            return await self._check_paper_exit(trade, current_price, atr)

        # ── Live mode: poll order status ─────────────────────────────────
        return await self._check_live_exit(trade, current_price, atr)

    async def _check_paper_exit(
        self, trade: OpenTrade, price: float, atr: float
    ) -> Optional[dict]:
        # Update price extremes for trailing stop
        self._update_trailing(trade, price, atr)

        if trade.side == "buy":
            if price <= trade.stop_loss:
                return await self.close_trade(trade.symbol, trade.stop_loss, "SL_HIT")
            if price >= trade.take_profit:
                return await self.close_trade(trade.symbol, trade.take_profit, "TP_HIT")
        else:  # short
            if price >= trade.stop_loss:
                return await self.close_trade(trade.symbol, trade.stop_loss, "SL_HIT")
            if price <= trade.take_profit:
                return await self.close_trade(trade.symbol, trade.take_profit, "TP_HIT")
        return None

    async def _check_live_exit(
        self, trade: OpenTrade, price: float, atr: float
    ) -> Optional[dict]:
        # Check if SL or TP order has been filled
        for order_id, label in [
            (trade.sl_order_id, "SL_HIT"),
            (trade.tp_order_id, "TP_HIT"),
        ]:
            if not order_id:
                continue
            status = await self.execution.fetch_order_status(trade.symbol, order_id)
            if status and status.get("status") == "closed":
                exit_price = float(status.get("average") or price)
                return await self.close_trade(trade.symbol, exit_price, label)

        # Update trailing stop
        self._update_trailing(trade, price, atr)
        return None

    def _update_trailing(self, trade: OpenTrade, price: float, atr: float):
        """
        Activate trailing stop when unrealized gain >= 1x ATR.
        Once active, move SL up (for longs) to follow price at 0.5x ATR below peak.
        """
        if trade.side == "buy":
            trade.highest_price = max(trade.highest_price, price)
            gain = trade.highest_price - trade.entry_price

            if not trade.trail_activated and gain >= atr * self.trail_activation_multiplier:
                trade.trail_activated = True
                logger.info("%s: trailing stop ACTIVATED at price=%.4f", trade.symbol, price)

            if trade.trail_activated:
                new_sl = trade.highest_price - atr * self.trail_distance_multiplier
                if new_sl > trade.stop_loss:
                    logger.debug(
                        "%s: trailing SL moved %.4f -> %.4f",
                        trade.symbol, trade.stop_loss, new_sl,
                    )
                    trade.stop_loss = new_sl

        else:  # short
            trade.lowest_price = min(trade.lowest_price, price)
            gain = trade.entry_price - trade.lowest_price

            if not trade.trail_activated and gain >= atr * self.trail_activation_multiplier:
                trade.trail_activated = True
                logger.info("%s: trailing stop ACTIVATED at price=%.4f", trade.symbol, price)

            if trade.trail_activated:
                new_sl = trade.lowest_price + atr * self.trail_distance_multiplier
                if new_sl < trade.stop_loss:
                    logger.debug(
                        "%s: trailing SL moved %.4f -> %.4f",
                        trade.symbol, trade.stop_loss, new_sl,
                    )
                    trade.stop_loss = new_sl
