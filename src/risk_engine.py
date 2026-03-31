"""
Risk Engine: Enforces position sizing, daily limits, and drawdown controls.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskParams:
    """Computed risk parameters for a single trade."""
    symbol: str
    side: str                 # "buy" or "sell"
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float           # base currency units
    risk_amount: float        # USDT at risk
    capital_snapshot: float   # account balance at signal time


@dataclass
class DailyStats:
    date: date = field(default_factory=date.today)
    trades_taken: int = 0
    consecutive_losses: int = 0
    starting_balance: float = 0.0
    realized_pnl: float = 0.0

    def reset(self, balance: float):
        self.date = date.today()
        self.trades_taken = 0
        self.consecutive_losses = 0
        self.starting_balance = balance
        self.realized_pnl = 0.0

    @property
    def drawdown_pct(self) -> float:
        if self.starting_balance <= 0:
            return 0.0
        return max(0.0, -self.realized_pnl / self.starting_balance * 100)


class RiskEngine:
    """
    Enforces:
      - 1% capital-at-risk per trade
      - SL = 1x ATR, TP = 2x ATR from entry
      - Max 3 trades/day
      - Stop after 2 consecutive losses
      - Daily drawdown limit of 3%
    """

    def __init__(self, config: dict):
        risk = config["risk"]
        self.capital_risk_pct = risk["capital_risk_pct"] / 100.0
        self.sl_multiplier = risk["atr_sl_multiplier"]
        self.tp_multiplier = risk["atr_tp_multiplier"]
        self.max_trades_per_day = risk["max_trades_per_day"]
        self.max_consecutive_losses = risk["max_consecutive_losses"]
        self.dd_limit_pct = risk["daily_drawdown_limit_pct"]

        self._stats: Dict[str, DailyStats] = {}  # per-symbol, plus global "_all"
        self._global = DailyStats()

    # ── Public API ──────────────────────────────────────────────────────

    def can_trade(self, balance: float) -> tuple[bool, str]:
        """Check all risk gates before allowing a new trade."""
        self._maybe_reset_daily(balance)

        if self._global.trades_taken >= self.max_trades_per_day:
            return False, f"Daily trade limit reached ({self.max_trades_per_day})"

        if self._global.consecutive_losses >= self.max_consecutive_losses:
            return False, f"Consecutive loss limit reached ({self.max_consecutive_losses})"

        if self._global.drawdown_pct >= self.dd_limit_pct:
            return False, (
                f"Daily drawdown limit reached "
                f"({self._global.drawdown_pct:.2f}% >= {self.dd_limit_pct}%)"
            )

        return True, "OK"

    def calculate(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        atr: float,
        balance: float,
    ) -> Optional[RiskParams]:
        """
        Compute position size and SL/TP levels.

        Position size formula:
            risk_amount = balance * capital_risk_pct
            distance_to_sl = atr * sl_multiplier
            quantity = risk_amount / distance_to_sl
        """
        if atr <= 0 or entry_price <= 0 or balance <= 0:
            logger.warning("%s: invalid inputs for risk calculation", symbol)
            return None

        risk_amount = balance * self.capital_risk_pct
        sl_distance = atr * self.sl_multiplier
        tp_distance = atr * self.tp_multiplier

        quantity = risk_amount / sl_distance
        if quantity <= 0:
            logger.warning("%s: calculated quantity is zero or negative", symbol)
            return None

        if side == "buy":
            stop_loss = entry_price - sl_distance
            take_profit = entry_price + tp_distance
        else:  # short / sell
            stop_loss = entry_price + sl_distance
            take_profit = entry_price - tp_distance

        # Sanity: SL must not be on the wrong side of entry
        if side == "buy" and stop_loss >= entry_price:
            logger.error("%s: stop_loss >= entry_price for BUY trade", symbol)
            return None
        if side == "sell" and stop_loss <= entry_price:
            logger.error("%s: stop_loss <= entry_price for SELL trade", symbol)
            return None

        params = RiskParams(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            quantity=round(quantity, 8),
            risk_amount=round(risk_amount, 4),
            capital_snapshot=balance,
        )
        logger.info(
            "%s risk params: qty=%.6f entry=%.4f SL=%.4f TP=%.4f risk=$%.2f",
            symbol, params.quantity, params.entry_price,
            params.stop_loss, params.take_profit, params.risk_amount,
        )
        return params

    def record_trade_open(self):
        """Call when a trade is successfully opened."""
        self._global.trades_taken += 1

    def record_trade_close(self, pnl: float):
        """Call when a trade is closed. pnl is signed USDT profit/loss."""
        self._global.realized_pnl += pnl
        if pnl < 0:
            self._global.consecutive_losses += 1
        else:
            self._global.consecutive_losses = 0

    def daily_summary(self) -> dict:
        return {
            "date": str(self._global.date),
            "trades_taken": self._global.trades_taken,
            "consecutive_losses": self._global.consecutive_losses,
            "realized_pnl": round(self._global.realized_pnl, 4),
            "drawdown_pct": round(self._global.drawdown_pct, 4),
        }

    # ── Internals ───────────────────────────────────────────────────────

    def _maybe_reset_daily(self, balance: float):
        today = date.today()
        if self._global.date != today:
            logger.info("New trading day — resetting daily stats")
            self._global.reset(balance)
