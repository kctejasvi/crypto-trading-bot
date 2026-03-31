"""
Options Execution (Deribit): Places both legs of a straddle/strangle.

Key Deribit differences vs Binance:
  - Premiums quoted in underlying (BTC/ETH), not USDT
  - Contracts are 1 BTC or 1 ETH each (Deribit standard)
  - Testnet at test.deribit.com — full API parity with live
  - Orders use instrument name directly (e.g. BTC-4APR25-84000-C)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import ccxt.async_support as ccxt

from .options_chain import StraddlePair

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2.0


@dataclass
class OpenOptionsTrade:
    symbol: str                      # underlying pair e.g. "BTC/USDT"
    strategy: str                    # "straddle" | "strangle"
    call_symbol: str                 # short instrument name e.g. BTC-260401-84000-C
    put_symbol: str
    call_ccxt_symbol: str            # full ccxt symbol for API calls
    put_ccxt_symbol: str
    call_order_id: str
    put_order_id: str
    call_premium: float              # in underlying (BTC/ETH)
    put_premium: float
    call_qty: float                  # contracts (1 contract = 1 BTC on Deribit)
    put_qty: float
    call_strike: float
    put_strike: float
    total_cost_underlying: float     # total premium in BTC/ETH
    total_cost_usdt: float           # total premium in USDT
    spot_at_entry: float
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    paper: bool = True
    underlying: str = "BTC"

    # Filled on close
    call_exit_price: float = 0.0
    put_exit_price: float = 0.0
    closed: bool = False
    closed_at: Optional[datetime] = None
    close_reason: str = ""

    def realized_pnl_usdt(self, call_exit: float, put_exit: float, spot: float) -> float:
        """PnL in USDT: (exit_premium - entry_premium) * qty * spot_price."""
        call_pnl = (call_exit - self.call_premium) * self.call_qty * spot
        put_pnl  = (put_exit  - self.put_premium)  * self.put_qty  * spot
        return call_pnl + put_pnl

    def current_value_ratio(
        self, call_price: float, put_price: float
    ) -> float:
        """How much the combined position is worth relative to what we paid."""
        entry = self.call_premium * self.call_qty + self.put_premium * self.put_qty
        current = call_price * self.call_qty + put_price * self.put_qty
        return current / entry if entry > 0 else 1.0


class OptionsExecutionEngine:
    """
    Places straddle/strangle legs on Deribit. Falls back to paper simulation
    when paper_trading=true.
    """

    def __init__(self, exchange: ccxt.Exchange, config: dict):
        self.exchange = exchange
        self.paper_trading: bool = config.get("paper_trading", True)
        opts_cfg = config.get("options", {})
        # Max % of total capital to spend on options premium
        self.max_premium_pct: float = opts_cfg.get("max_premium_pct_capital", 2.0) / 100
        # Deribit min contract size is 0.1 BTC for BTC options, 1 ETH for ETH
        self.min_contract: dict = {"BTC": 0.1, "ETH": 1.0}

    async def enter_position(
        self, pair: StraddlePair, balance_usdt: float
    ) -> Optional[OpenOptionsTrade]:
        """
        Calculate position size (in contracts) from premium budget,
        then place both legs simultaneously.
        """
        underlying = pair.call.underlying
        spot = pair.underlying_price

        if pair.total_premium_usdt <= 0 or spot <= 0:
            logger.error("Invalid premium or spot price: %.6f / %.2f", pair.total_premium_usdt, spot)
            return None

        # Budget in USDT → convert to underlying units
        max_spend_usdt = balance_usdt * self.max_premium_pct
        max_spend_underlying = max_spend_usdt / spot

        # qty in underlying units (Deribit contract = 1 underlying)
        min_size = self.min_contract.get(underlying, 0.1)
        raw_qty = max_spend_underlying / pair.total_premium_underlying
        qty = max(min_size, round(raw_qty / min_size) * min_size)

        total_cost_underlying = pair.total_premium_underlying * qty
        total_cost_usdt = total_cost_underlying * spot

        # Hard cap: never spend more than 50% of balance
        if total_cost_usdt > balance_usdt * 0.5:
            logger.warning(
                "Options cost $%.2f exceeds 50%% of balance $%.2f — reducing to minimum",
                total_cost_usdt, balance_usdt,
            )
            qty = min_size
            total_cost_underlying = pair.total_premium_underlying * qty
            total_cost_usdt = total_cost_underlying * spot

        logger.info(
            "%s %s: qty=%.2f  call@%.6f %s  put@%.6f %s  "
            "total_cost=%.6f %s ($%.2f, %.1f%% of balance)  move_needed=±%.2f%%",
            underlying, pair.strategy.upper(), qty,
            pair.call.mid_price, underlying,
            pair.put.mid_price, underlying,
            total_cost_underlying, underlying, total_cost_usdt,
            total_cost_usdt / balance_usdt * 100,
            pair.move_needed_pct,
        )

        if self.paper_trading:
            return self._simulate(pair, qty, total_cost_underlying, total_cost_usdt)

        # ── Live: place both legs using ccxt unified symbols ─────────────
        call_order = await self._place_order(pair.call.ccxt_symbol, qty, "buy")
        if call_order is None:
            logger.error("Call leg failed — aborting")
            return None

        put_order = await self._place_order(pair.put.ccxt_symbol, qty, "buy")
        if put_order is None:
            logger.error("Put leg failed — cancelling call leg")
            await self._cancel(pair.call.ccxt_symbol, call_order["id"])
            return None

        call_fill = float(call_order.get("average") or pair.call.mid_price)
        put_fill  = float(put_order.get("average") or pair.put.mid_price)

        return OpenOptionsTrade(
            symbol=underlying + "/USDT",
            strategy=pair.strategy,
            call_symbol=pair.call.symbol,
            put_symbol=pair.put.symbol,
            call_ccxt_symbol=pair.call.ccxt_symbol,
            put_ccxt_symbol=pair.put.ccxt_symbol,
            call_order_id=call_order["id"],
            put_order_id=put_order["id"],
            call_premium=call_fill,
            put_premium=put_fill,
            call_qty=qty,
            put_qty=qty,
            call_strike=pair.call.strike,
            put_strike=pair.put.strike,
            total_cost_underlying=(call_fill + put_fill) * qty,
            total_cost_usdt=(call_fill + put_fill) * qty * spot,
            spot_at_entry=spot,
            paper=False,
            underlying=underlying,
        )

    async def close_position(
        self,
        trade: OpenOptionsTrade,
        call_price: float,
        put_price: float,
        reason: str,
        spot: float,
    ) -> dict:
        """Sell both legs and record result."""
        if not self.paper_trading:
            await self._place_order(trade.call_ccxt_symbol, trade.call_qty, "sell")
            await self._place_order(trade.put_ccxt_symbol,  trade.put_qty,  "sell")

        pnl_usdt = trade.realized_pnl_usdt(call_price, put_price, spot)

        trade.closed = True
        trade.closed_at = datetime.now(timezone.utc)
        trade.close_reason = reason
        trade.call_exit_price = call_price
        trade.put_exit_price  = put_price

        logger.info(
            "%s %s CLOSED: reason=%s  pnl=%.4f USDT  "
            "(call %.6f→%.6f  put %.6f→%.6f)",
            trade.symbol, trade.strategy.upper(), reason, pnl_usdt,
            trade.call_premium, call_price,
            trade.put_premium, put_price,
        )
        return {
            "symbol": trade.symbol,
            "strategy": trade.strategy,
            "call_symbol": trade.call_symbol,
            "put_symbol": trade.put_symbol,
            "call_strike": trade.call_strike,
            "put_strike": trade.put_strike,
            "call_premium_entry": trade.call_premium,
            "put_premium_entry": trade.put_premium,
            "call_premium_exit": call_price,
            "put_premium_exit": put_price,
            "quantity": trade.call_qty,
            "total_cost_usdt": trade.total_cost_usdt,
            "pnl": round(pnl_usdt, 4),
            "reason": reason,
            "spot_at_entry": trade.spot_at_entry,
            "spot_at_exit": spot,
            "opened_at": trade.opened_at.isoformat(),
            "closed_at": trade.closed_at.isoformat(),
            "paper": trade.paper,
            "side": "options",
        }

    async def get_option_price(self, instrument_name: str) -> float:
        """Fetch current mark price of a Deribit option (in underlying)."""
        if self.paper_trading:
            return 0.0
        try:
            ticker = await self.exchange.fetch_ticker(instrument_name)
            info = ticker.get("info", {})
            return float(
                info.get("mark_price") or ticker.get("last") or 0
            )
        except Exception as e:
            logger.error("Failed to fetch Deribit option price %s: %s", instrument_name, e)
            return 0.0

    # ── Internals ────────────────────────────────────────────────────────

    async def _place_order(
        self, instrument: str, qty: float, side: str
    ) -> Optional[dict]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=instrument,
                    type="market",
                    side=side,
                    amount=qty,
                )
                logger.info(
                    "Deribit %s %s qty=%.2f  id=%s",
                    side.upper(), instrument, qty, order["id"],
                )
                return order
            except ccxt.InsufficientFunds as e:
                logger.error("Insufficient funds for %s %s: %s", side, instrument, e)
                return None
            except ccxt.NetworkError as e:
                logger.warning("Network error attempt %d (%s): %s", attempt, instrument, e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
            except ccxt.ExchangeError as e:
                logger.error("Exchange error %s %s: %s", side, instrument, e)
                return None
        logger.error("Exhausted retries for %s %s", side, instrument)
        return None

    async def _cancel(self, instrument: str, order_id: str):
        try:
            await self.exchange.cancel_order(order_id, instrument)
            logger.info("Cancelled order %s on %s", order_id, instrument)
        except Exception as e:
            logger.warning("Failed to cancel %s: %s", order_id, e)

    def _simulate(
        self,
        pair: StraddlePair,
        qty: float,
        total_underlying: float,
        total_usdt: float,
    ) -> OpenOptionsTrade:
        logger.warning(
            "[PAPER DERIBIT] %s %s  qty=%.2f  "
            "call %s@%.6f %s  put %s@%.6f %s  "
            "total=$%.2f  move_needed=±%.2f%%  DTE=%.1fd",
            pair.call.underlying, pair.strategy.upper(), qty,
            pair.call.symbol, pair.call.mid_price, pair.call.underlying,
            pair.put.symbol, pair.put.mid_price, pair.put.underlying,
            total_usdt, pair.move_needed_pct, pair.call.days_to_expiry,
        )
        return OpenOptionsTrade(
            symbol=pair.call.underlying + "/USDT",
            strategy=pair.strategy,
            call_symbol=pair.call.symbol,
            put_symbol=pair.put.symbol,
            call_ccxt_symbol=pair.call.ccxt_symbol,
            put_ccxt_symbol=pair.put.ccxt_symbol,
            call_order_id="PAPER_DERIBIT_CALL",
            put_order_id="PAPER_DERIBIT_PUT",
            call_premium=pair.call.mid_price,
            put_premium=pair.put.mid_price,
            call_qty=qty,
            put_qty=qty,
            call_strike=pair.call.strike,
            put_strike=pair.put.strike,
            total_cost_underlying=total_underlying,
            total_cost_usdt=total_usdt,
            spot_at_entry=pair.underlying_price,
            paper=True,
            underlying=pair.call.underlying,
        )
