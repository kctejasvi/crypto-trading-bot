"""
Indicator Engine: Calculates technical indicators on OHLCV DataFrames.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import ta

logger = logging.getLogger(__name__)


@dataclass
class Indicators:
    """Snapshot of indicator values for the latest completed candle."""
    symbol: str
    timestamp: pd.Timestamp
    close: float
    ema_fast: float
    ema_slow: float
    rsi: float
    atr: float
    volume: float
    volume_avg: float
    high_20: float   # 20-candle rolling high (excl. last candle)
    low_20: float    # 20-candle rolling low  (excl. last candle)

    @property
    def volume_ratio(self) -> float:
        return self.volume / self.volume_avg if self.volume_avg > 0 else 0.0


class IndicatorEngine:
    """
    Computes EMA-20, EMA-50, RSI-14, ATR-14, volume average,
    and the rolling 20-candle high/low breakout levels.
    """

    def __init__(self, config: dict):
        ind = config["indicators"]
        self.ema_fast_period = ind["ema_fast"]
        self.ema_slow_period = ind["ema_slow"]
        self.rsi_period = ind["rsi_period"]
        self.atr_period = ind["atr_period"]
        self.vol_avg_period = ind["volume_avg_period"]
        self.breakout_lookback = ind["breakout_lookback"]

    def calculate(self, symbol: str, df: pd.DataFrame) -> Optional[Indicators]:
        """
        Calculate all indicators on the provided DataFrame.
        Returns an Indicators snapshot or None if data is insufficient.
        """
        min_rows = max(
            self.ema_slow_period,
            self.rsi_period,
            self.atr_period,
            self.breakout_lookback,
        ) + 5

        if len(df) < min_rows:
            logger.warning("%s: not enough rows (%d) to calculate indicators", symbol, len(df))
            return None

        try:
            close = df["close"]
            high = df["high"]
            low = df["low"]
            volume = df["volume"]

            # EMA
            ema_fast = ta.trend.EMAIndicator(close=close, window=self.ema_fast_period).ema_indicator()
            ema_slow = ta.trend.EMAIndicator(close=close, window=self.ema_slow_period).ema_indicator()

            # RSI
            rsi = ta.momentum.RSIIndicator(close=close, window=self.rsi_period).rsi()

            # ATR
            atr = ta.volatility.AverageTrueRange(
                high=high, low=low, close=close, window=self.atr_period
            ).average_true_range()

            # Volume average (SMA of volume)
            vol_avg = volume.rolling(window=self.vol_avg_period).mean()

            # 20-candle breakout levels — shift by 1 so current candle is NOT included
            # This represents the highest high / lowest low of the previous 20 candles
            high_20 = high.shift(1).rolling(window=self.breakout_lookback).max()
            low_20 = low.shift(1).rolling(window=self.breakout_lookback).min()

            # Validate no NaN in final row
            last = df.index[-1]
            vals = {
                "ema_fast": ema_fast.iloc[-1],
                "ema_slow": ema_slow.iloc[-1],
                "rsi": rsi.iloc[-1],
                "atr": atr.iloc[-1],
                "vol_avg": vol_avg.iloc[-1],
                "high_20": high_20.iloc[-1],
                "low_20": low_20.iloc[-1],
            }
            if any(np.isnan(v) for v in vals.values()):
                logger.warning("%s: NaN in indicators at %s: %s", symbol, last, vals)
                return None

            return Indicators(
                symbol=symbol,
                timestamp=last,
                close=float(close.iloc[-1]),
                ema_fast=float(vals["ema_fast"]),
                ema_slow=float(vals["ema_slow"]),
                rsi=float(vals["rsi"]),
                atr=float(vals["atr"]),
                volume=float(volume.iloc[-1]),
                volume_avg=float(vals["vol_avg"]),
                high_20=float(vals["high_20"]),
                low_20=float(vals["low_20"]),
            )

        except Exception as e:
            logger.exception("%s: error calculating indicators: %s", symbol, e)
            return None
