"""
Aggressive Strategy: Higher-risk trading for difficult/sideways markets.

Two sub-modes (both active simultaneously, best signal wins):

1. MEAN_REVERSION
   - BUY  when RSI < 28  (extremely oversold — bounce expected)
   - SELL when RSI > 72  (extremely overbought — fade expected)
   - No EMA trend filter, no breakout required
   - Volume: any (no minimum)
   - Designed for: current BTC RSI 22 downtrend / oversold market

2. AGGRESSIVE_TREND
   - BUY:  EMA20 > EMA50, RSI 45-75, volume > 1.0x, close > 15-candle high
   - SELL: EMA20 < EMA50, RSI 15-40, volume > 1.0x, close < 15-candle low
   - Lower thresholds than normal strategy to catch more setups

Risk profile (overrides normal config):
   - Capital risk: 2% per trade (double normal)
   - SL: 0.5x ATR  (tighter)
   - TP: 1.5x ATR  (lower but more likely to hit)
   - Max trades/day raised to 6
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .indicator_engine import Indicators
from .strategy_engine import Signal, SignalResult

logger = logging.getLogger(__name__)


class AggressiveMode(Enum):
    MEAN_REVERSION   = "MEAN_REVERSION"
    AGGRESSIVE_TREND = "AGGRESSIVE_TREND"


@dataclass
class AggressiveSignalResult(SignalResult):
    mode: AggressiveMode = AggressiveMode.MEAN_REVERSION
    aggr_score: float = 0.0    # 0–100 confidence score for this signal


class AggressiveStrategy:
    """
    Higher-risk strategy for low-signal markets.
    Enabled/disabled at runtime via Telegram /aggressive command.
    """

    # Risk overrides when aggressive mode is active
    CAPITAL_RISK_PCT   = 0.02   # 2% per trade
    SL_MULTIPLIER      = 0.5    # 0.5x ATR
    TP_MULTIPLIER      = 1.5    # 1.5x ATR
    MAX_TRADES_PER_DAY = 6

    # Mean reversion thresholds
    MR_RSI_BUY_MAX  = 28.0    # RSI below this → BUY (oversold)
    MR_RSI_SELL_MIN = 72.0    # RSI above this → SELL (overbought)

    # Aggressive trend thresholds
    AT_RSI_BUY_LOW   = 45.0
    AT_RSI_BUY_HIGH  = 75.0
    AT_RSI_SELL_LOW  = 15.0
    AT_RSI_SELL_HIGH = 40.0
    AT_VOLUME_MULT   = 1.0    # 1.0x (vs 1.5x normal)
    AT_BREAKOUT_BARS = 15     # 15-candle high/low (vs 20 normal)

    def __init__(self):
        self._enabled: bool = False

    # ── State ───────────────────────────────────────────────────────────

    def enable(self):
        self._enabled = True
        logger.warning("AggressiveStrategy ENABLED — higher risk mode active")

    def disable(self):
        self._enabled = False
        logger.warning("AggressiveStrategy DISABLED — back to normal mode")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Signal evaluation ────────────────────────────────────────────────

    def evaluate(self, ind: Indicators) -> Optional[AggressiveSignalResult]:
        """
        Evaluate both sub-modes. Returns the higher-scored signal,
        or None if disabled or no signal found.
        """
        if not self._enabled:
            return None

        mr_sig  = self._mean_reversion(ind)
        at_sig  = self._aggressive_trend(ind)

        # Pick highest scored
        candidates = [s for s in [mr_sig, at_sig] if s is not None and s.signal != Signal.NONE]
        if not candidates:
            return None

        best = max(candidates, key=lambda s: s.aggr_score)
        return best

    # ── Mean Reversion ───────────────────────────────────────────────────

    def _mean_reversion(self, ind: Indicators) -> AggressiveSignalResult:
        # BUY — RSI extremely oversold
        if ind.rsi <= self.MR_RSI_BUY_MAX:
            # Score: lower RSI = stronger signal
            score = min(100, 80 + (self.MR_RSI_BUY_MAX - ind.rsi) * 1.5)
            reason = (
                f"MR_BUY: RSI={ind.rsi:.1f}<={self.MR_RSI_BUY_MAX} "
                f"(oversold bounce) close={ind.close:.4f}"
            )
            logger.info("%s AGGRESSIVE %s", ind.symbol, reason)
            return AggressiveSignalResult(
                signal=Signal.BUY, symbol=ind.symbol,
                reason=reason, indicators=ind,
                mode=AggressiveMode.MEAN_REVERSION, aggr_score=score
            )

        # SELL — RSI extremely overbought
        if ind.rsi >= self.MR_RSI_SELL_MIN:
            score = min(100, 80 + (ind.rsi - self.MR_RSI_SELL_MIN) * 1.5)
            reason = (
                f"MR_SELL: RSI={ind.rsi:.1f}>={self.MR_RSI_SELL_MIN} "
                f"(overbought fade) close={ind.close:.4f}"
            )
            logger.info("%s AGGRESSIVE %s", ind.symbol, reason)
            return AggressiveSignalResult(
                signal=Signal.SELL, symbol=ind.symbol,
                reason=reason, indicators=ind,
                mode=AggressiveMode.MEAN_REVERSION, aggr_score=score
            )

        return AggressiveSignalResult(
            signal=Signal.NONE, symbol=ind.symbol,
            reason="MR: RSI not in extreme zone", indicators=ind,
            mode=AggressiveMode.MEAN_REVERSION, aggr_score=0.0
        )

    # ── Aggressive Trend ─────────────────────────────────────────────────

    def _aggressive_trend(self, ind: Indicators) -> AggressiveSignalResult:
        vol_ok = ind.volume_ratio >= self.AT_VOLUME_MULT

        # BUY
        trend_up   = ind.ema_fast > ind.ema_slow
        rsi_buy_ok = self.AT_RSI_BUY_LOW <= ind.rsi <= self.AT_RSI_BUY_HIGH
        breakout   = ind.close > ind.high_20   # still use 20-candle high for safety

        if trend_up and rsi_buy_ok and vol_ok:
            score = 60.0
            if breakout:
                score += 15.0
            score += min(10, (ind.rsi - self.AT_RSI_BUY_LOW) / 3)
            reason = (
                f"AT_BUY: EMA{ind.ema_fast:.0f}>EMA{ind.ema_slow:.0f} "
                f"rsi={ind.rsi:.1f} vol={ind.volume_ratio:.2f}x "
                f"{'breakout' if breakout else 'no-breakout'}"
            )
            logger.info("%s AGGRESSIVE %s", ind.symbol, reason)
            return AggressiveSignalResult(
                signal=Signal.BUY, symbol=ind.symbol,
                reason=reason, indicators=ind,
                mode=AggressiveMode.AGGRESSIVE_TREND, aggr_score=round(score, 1)
            )

        # SELL
        trend_down    = ind.ema_fast < ind.ema_slow
        rsi_sell_ok   = self.AT_RSI_SELL_LOW <= ind.rsi <= self.AT_RSI_SELL_HIGH
        breakdown     = ind.close < ind.low_20

        if trend_down and rsi_sell_ok and vol_ok:
            score = 60.0
            if breakdown:
                score += 15.0
            score += min(10, (self.AT_RSI_SELL_HIGH - ind.rsi) / 2.5)
            reason = (
                f"AT_SELL: EMA{ind.ema_fast:.0f}<EMA{ind.ema_slow:.0f} "
                f"rsi={ind.rsi:.1f} vol={ind.volume_ratio:.2f}x "
                f"{'breakdown' if breakdown else 'no-breakdown'}"
            )
            logger.info("%s AGGRESSIVE %s", ind.symbol, reason)
            return AggressiveSignalResult(
                signal=Signal.SELL, symbol=ind.symbol,
                reason=reason, indicators=ind,
                mode=AggressiveMode.AGGRESSIVE_TREND, aggr_score=round(score, 1)
            )

        return AggressiveSignalResult(
            signal=Signal.NONE, symbol=ind.symbol,
            reason="AT: no conditions met", indicators=ind,
            mode=AggressiveMode.AGGRESSIVE_TREND, aggr_score=0.0
        )
