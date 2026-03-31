"""
Options Manager: Tracks open straddle/strangle positions and manages exits.

Exit logic:
  - TP:          combined premium value >= entry_cost * tp_multiplier  (e.g. 2x = 100% gain)
  - SL:          combined premium value <= entry_cost * sl_multiplier  (e.g. 0.5x = 50% loss)
  - Time decay:  DTE <= min_dte_exit hours before expiry — exit to avoid theta crush
  - Max hold:    close after max_hold_hours regardless
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .options_execution import OpenOptionsTrade, OptionsExecutionEngine

logger = logging.getLogger(__name__)


class OptionsManager:
    def __init__(self, execution: OptionsExecutionEngine, config: dict):
        self.execution = execution
        opts_cfg = config.get("options", {})
        self.tp_multiplier: float   = opts_cfg.get("tp_multiplier", 2.0)   # 100% gain
        self.sl_multiplier: float   = opts_cfg.get("sl_multiplier", 0.5)   # 50% loss
        self.min_dte_exit: float    = opts_cfg.get("min_dte_exit_hours", 4.0) / 24
        self.max_hold_hours: float  = opts_cfg.get("max_hold_hours", 48.0)

        self._trades: Dict[str, OpenOptionsTrade] = {}  # symbol -> trade

    # ── Open / register ──────────────────────────────────────────────────

    def register(self, trade: OpenOptionsTrade):
        self._trades[trade.symbol] = trade
        logger.info(
            "Options trade REGISTERED: %s %s cost=$%.2f TP@$%.2f SL@$%.2f",
            trade.symbol, trade.strategy.upper(), trade.total_cost_usdt,
            trade.total_cost_usdt * self.tp_multiplier,
            trade.total_cost_usdt * self.sl_multiplier,
        )

    def has_open_trade(self, symbol: str) -> bool:
        t = self._trades.get(symbol)
        return t is not None and not t.closed

    def get_trade(self, symbol: str) -> Optional[OpenOptionsTrade]:
        return self._trades.get(symbol)

    def all_trades(self) -> List[OpenOptionsTrade]:
        return [t for t in self._trades.values() if not t.closed]

    # ── Update (called each cycle) ───────────────────────────────────────

    async def update(
        self,
        symbol: str,
        spot_price: float,
    ) -> Optional[dict]:
        """
        Fetch current option prices and check exit conditions.
        Returns a close-result dict if the trade was exited, else None.
        """
        trade = self._trades.get(symbol)
        if trade is None or trade.closed:
            return None

        # Fetch current prices (in underlying: BTC/ETH)
        if trade.paper:
            call_price, put_price = self._paper_prices(trade, spot_price)
        else:
            call_price = await self.execution.get_option_price(trade.call_symbol)
            put_price  = await self.execution.get_option_price(trade.put_symbol)

        if call_price <= 0 or put_price <= 0:
            logger.debug("%s: could not fetch option prices this cycle", symbol)
            return None

        value_ratio = trade.current_value_ratio(call_price, put_price)
        dte         = self._dte_from_deribit_symbol(trade.call_symbol)
        hours_held  = (datetime.now(timezone.utc) - trade.opened_at).total_seconds() / 3600
        pnl_usdt    = trade.realized_pnl_usdt(call_price, put_price, spot_price)

        logger.debug(
            "%s options: value_ratio=%.2fx pnl=$%.2f dte=%.2fd held=%.1fh",
            symbol, value_ratio, pnl_usdt, dte, hours_held,
        )

        # ── Exit conditions ──────────────────────────────────────────────
        reason = None

        if value_ratio >= self.tp_multiplier:
            reason = f"TP_HIT (value_ratio={value_ratio:.2f}x)"
        elif value_ratio <= self.sl_multiplier:
            reason = f"SL_HIT (value_ratio={value_ratio:.2f}x)"
        elif dte <= self.min_dte_exit:
            reason = f"TIME_DECAY (dte={dte:.2f}d)"
        elif hours_held >= self.max_hold_hours:
            reason = f"MAX_HOLD ({hours_held:.1f}h)"

        if reason:
            return await self.execution.close_position(
                trade, call_price, put_price, reason, spot_price
            )

        return None

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _dte_from_deribit_symbol(option_symbol: str) -> float:
        """Parse DTE from Deribit instrument name e.g. BTC-260401-84000-C (YYMMDD)."""
        try:
            parts = option_symbol.split("-")
            expiry = datetime.strptime(parts[1], "%y%m%d").replace(
                hour=8, minute=0, second=0, tzinfo=timezone.utc
            )
            return max(0.0, (expiry - datetime.now(timezone.utc)).total_seconds() / 86400)
        except Exception:
            return 999.0  # unknown — don't exit on time decay

    @staticmethod
    def _paper_prices(trade: OpenOptionsTrade, spot: float) -> tuple:
        """
        Approximate paper option prices using intrinsic value + small time value.
        This is a simple simulation — not a real pricing model.
        """
        move_pct = abs(spot - trade.spot_at_entry) / trade.spot_at_entry

        # Scale premium by how much the underlying moved
        # A large move increases the winning leg; losing leg decays to near zero
        if spot > trade.spot_at_entry:
            # Call gains, put loses
            call_multiplier = 1 + move_pct * 8   # roughly 8x leverage on big moves
            put_multiplier  = max(0.05, 1 - move_pct * 5)
        elif spot < trade.spot_at_entry:
            # Put gains, call loses
            call_multiplier = max(0.05, 1 - move_pct * 5)
            put_multiplier  = 1 + move_pct * 8
        else:
            call_multiplier = 1.0
            put_multiplier  = 1.0

        call_price = trade.call_premium * call_multiplier
        put_price  = trade.put_premium  * put_multiplier
        return call_price, put_price
