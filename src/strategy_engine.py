"""
Strategy Engine: Evaluates buy/sell signals using momentum breakout logic.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .indicator_engine import Indicators

logger = logging.getLogger(__name__)


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    NONE = "NONE"


@dataclass
class SignalResult:
    signal: Signal
    symbol: str
    reason: str
    indicators: Indicators  # always populated — used by SignalRanker


class StrategyEngine:
    """
    Momentum Breakout Strategy:

    BUY when ALL of:
      - EMA20 > EMA50 (uptrend)
      - Close > 20-candle high (breakout)
      - Volume > 1.5x average (confirmation)
      - RSI between 55 and 70 (momentum, not overbought)

    SELL when ALL of:
      - EMA20 < EMA50 (downtrend)
      - Close < 20-candle low (breakdown)
      - Volume > 1.5x average (confirmation)
      - RSI between 30 and 45 (momentum, not oversold)
    """

    def __init__(self, config: dict):
        ind = config["indicators"]
        self.rsi_buy_low = ind["rsi_buy_low"]
        self.rsi_buy_high = ind["rsi_buy_high"]
        self.rsi_sell_low = ind["rsi_sell_low"]
        self.rsi_sell_high = ind["rsi_sell_high"]
        self.volume_multiplier = ind["volume_multiplier"]

    def evaluate(self, ind: Indicators) -> SignalResult:
        """Evaluate indicators and return a signal."""
        # ── BUY CONDITIONS ──────────────────────────────────────────────
        trend_up = ind.ema_fast > ind.ema_slow
        breakout_up = ind.close > ind.high_20
        volume_ok_buy = ind.volume_ratio >= self.volume_multiplier
        rsi_ok_buy = self.rsi_buy_low <= ind.rsi <= self.rsi_buy_high

        if trend_up and breakout_up and volume_ok_buy and rsi_ok_buy:
            reason = (
                f"BUY: EMA{ind.ema_fast:.2f}>EMA{ind.ema_slow:.2f} "
                f"close={ind.close:.4f}>high20={ind.high_20:.4f} "
                f"vol_ratio={ind.volume_ratio:.2f} rsi={ind.rsi:.1f}"
            )
            logger.info("%s SIGNAL %s", ind.symbol, reason)
            return SignalResult(Signal.BUY, ind.symbol, reason, ind)

        # ── SELL CONDITIONS ─────────────────────────────────────────────
        trend_down = ind.ema_fast < ind.ema_slow
        breakdown = ind.close < ind.low_20
        volume_ok_sell = ind.volume_ratio >= self.volume_multiplier
        rsi_ok_sell = self.rsi_sell_low <= ind.rsi <= self.rsi_sell_high

        if trend_down and breakdown and volume_ok_sell and rsi_ok_sell:
            reason = (
                f"SELL: EMA{ind.ema_fast:.2f}<EMA{ind.ema_slow:.2f} "
                f"close={ind.close:.4f}<low20={ind.low_20:.4f} "
                f"vol_ratio={ind.volume_ratio:.2f} rsi={ind.rsi:.1f}"
            )
            logger.info("%s SIGNAL %s", ind.symbol, reason)
            return SignalResult(Signal.SELL, ind.symbol, reason, ind)

        # Log why signal was not triggered (debug level)
        self._log_no_signal(ind, trend_up, breakout_up, volume_ok_buy, rsi_ok_buy)
        return SignalResult(Signal.NONE, ind.symbol, "No signal", ind)

    def _log_no_signal(
        self, ind: Indicators,
        trend_up: bool, breakout_up: bool, vol_ok: bool, rsi_ok: bool
    ):
        reasons = []
        if not trend_up:
            reasons.append(f"EMA{ind.ema_fast:.2f}<EMA{ind.ema_slow:.2f}")
        if not breakout_up:
            reasons.append(f"close={ind.close:.4f}<=high20={ind.high_20:.4f}")
        if not vol_ok:
            reasons.append(f"vol_ratio={ind.volume_ratio:.2f}<{self.volume_multiplier}")
        if not rsi_ok:
            reasons.append(f"rsi={ind.rsi:.1f} outside [{self.rsi_buy_low},{self.rsi_buy_high}]")
        logger.debug("%s no signal: %s", ind.symbol, "; ".join(reasons))
