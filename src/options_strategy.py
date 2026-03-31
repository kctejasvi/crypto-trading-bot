"""
Options Strategy: Detects volatility squeeze setups for straddle/strangle entry.

Entry logic — fire when ALL of:
  1. Bollinger Band width is contracting (squeeze) — low recent volatility
  2. ATR is below its own N-period average (calm market, compression)
  3. Volume is declining (pre-breakout accumulation)
  4. NOT already near a fresh extreme (avoid entering after the move has started)

The idea: enter BEFORE the big move, profit from expansion in either direction.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
import ta

logger = logging.getLogger(__name__)


class OptionsSignal(Enum):
    ENTER_STRADDLE  = "ENTER_STRADDLE"
    ENTER_STRANGLE  = "ENTER_STRANGLE"
    NONE            = "NONE"


@dataclass
class OptionsSignalResult:
    signal: OptionsSignal
    symbol: str
    reason: str
    bb_width: float
    bb_width_percentile: float   # where current width sits vs last N periods
    atr: float
    atr_ratio: float             # current ATR / avg ATR
    volume_ratio: float


class OptionsStrategyEngine:
    """
    Volatility squeeze detector.

    Straddle signal  → very tight squeeze (BB width in bottom 20th percentile)
    Strangle signal  → moderate squeeze (BB width in 20th–35th percentile) —
                       cheaper because the move might not be as explosive
    """

    def __init__(self, config: dict):
        opts_cfg = config.get("options", {})
        self.bb_period: int   = opts_cfg.get("bb_period", 20)
        self.bb_std: float    = opts_cfg.get("bb_std", 2.0)
        self.atr_period: int  = opts_cfg.get("atr_period", 14)
        self.atr_avg_period: int = opts_cfg.get("atr_avg_period", 50)
        self.vol_avg_period: int = opts_cfg.get("vol_avg_period", 20)
        self.lookback: int    = opts_cfg.get("squeeze_lookback", 50)

        # Percentile thresholds
        self.straddle_pct_threshold: float  = opts_cfg.get("straddle_bb_pct", 20.0)
        self.strangle_pct_threshold: float  = opts_cfg.get("strangle_bb_pct", 35.0)

        # Max move already happened — don't enter if price moved > this % recently
        self.max_recent_move_pct: float = opts_cfg.get("max_recent_move_pct", 1.5)

    def evaluate(self, symbol: str, df: pd.DataFrame) -> OptionsSignalResult:
        """Evaluate the dataframe for a squeeze entry signal."""
        min_rows = max(self.bb_period, self.atr_avg_period, self.lookback) + 5
        if len(df) < min_rows:
            return self._no_signal(symbol, f"insufficient data ({len(df)} rows)", 0, 0, 0, 0, 0)

        try:
            close  = df["close"]
            high   = df["high"]
            low    = df["low"]
            volume = df["volume"]

            # ── Bollinger Bands ──────────────────────────────────────────
            bb = ta.volatility.BollingerBands(close=close, window=self.bb_period, window_dev=self.bb_std)
            bb_upper = bb.bollinger_hband()
            bb_lower = bb.bollinger_lband()
            bb_mid   = bb.bollinger_mavg()

            bb_width = ((bb_upper - bb_lower) / bb_mid).dropna()
            if len(bb_width) < self.lookback:
                return self._no_signal(symbol, "insufficient BB history", 0, 0, 0, 0, 0)

            current_width = float(bb_width.iloc[-1])
            historical_widths = bb_width.iloc[-self.lookback:]
            width_percentile = float(
                np.sum(historical_widths <= current_width) / len(historical_widths) * 100
            )

            # ── ATR ratio ────────────────────────────────────────────────
            atr_series = ta.volatility.AverageTrueRange(
                high=high, low=low, close=close, window=self.atr_period
            ).average_true_range()
            atr_avg = atr_series.rolling(window=self.atr_avg_period).mean()

            current_atr = float(atr_series.iloc[-1])
            current_atr_avg = float(atr_avg.iloc[-1])
            atr_ratio = current_atr / current_atr_avg if current_atr_avg > 0 else 1.0

            # ── Volume ratio ─────────────────────────────────────────────
            vol_avg = float(volume.rolling(self.vol_avg_period).mean().iloc[-1])
            current_vol = float(volume.iloc[-1])
            vol_ratio = current_vol / vol_avg if vol_avg > 0 else 1.0

            # ── Recent price move (avoid entering mid-breakout) ───────────
            recent_high = float(high.iloc[-5:].max())
            recent_low  = float(low.iloc[-5:].min())
            current_close = float(close.iloc[-1])
            recent_move_pct = (recent_high - recent_low) / current_close * 100

            if recent_move_pct > self.max_recent_move_pct:
                return self._no_signal(
                    symbol,
                    f"move already underway ({recent_move_pct:.2f}% > {self.max_recent_move_pct}%)",
                    current_width, width_percentile, current_atr, atr_ratio, vol_ratio,
                )

            # ── NaN checks ───────────────────────────────────────────────
            if any(np.isnan(v) for v in [current_width, current_atr, atr_ratio, vol_ratio]):
                return self._no_signal(symbol, "NaN in indicators", 0, 0, 0, 0, 0)

            # ── Signal decision ──────────────────────────────────────────
            squeeze = atr_ratio < 0.85  # ATR below 85% of its average

            if width_percentile <= self.straddle_pct_threshold and squeeze:
                reason = (
                    f"STRADDLE squeeze: BB_width_pct={width_percentile:.1f}th "
                    f"atr_ratio={atr_ratio:.2f} vol_ratio={vol_ratio:.2f}"
                )
                logger.info("%s OPTIONS SIGNAL: %s", symbol, reason)
                return OptionsSignalResult(
                    OptionsSignal.ENTER_STRADDLE, symbol, reason,
                    current_width, width_percentile, current_atr, atr_ratio, vol_ratio,
                )

            if width_percentile <= self.strangle_pct_threshold and squeeze:
                reason = (
                    f"STRANGLE squeeze: BB_width_pct={width_percentile:.1f}th "
                    f"atr_ratio={atr_ratio:.2f} vol_ratio={vol_ratio:.2f}"
                )
                logger.info("%s OPTIONS SIGNAL: %s", symbol, reason)
                return OptionsSignalResult(
                    OptionsSignal.ENTER_STRANGLE, symbol, reason,
                    current_width, width_percentile, current_atr, atr_ratio, vol_ratio,
                )

            logger.debug(
                "%s no options signal: BB_pct=%.1f atr_ratio=%.2f",
                symbol, width_percentile, atr_ratio,
            )
            return self._no_signal(
                symbol,
                f"no squeeze: BB_pct={width_percentile:.1f} atr_ratio={atr_ratio:.2f}",
                current_width, width_percentile, current_atr, atr_ratio, vol_ratio,
            )

        except Exception as e:
            logger.exception("%s: error in options strategy: %s", symbol, e)
            return self._no_signal(symbol, f"error: {e}", 0, 0, 0, 0, 0)

    @staticmethod
    def _no_signal(symbol, reason, bw, bwp, atr, atr_r, vol_r) -> OptionsSignalResult:
        return OptionsSignalResult(
            OptionsSignal.NONE, symbol, reason, bw, bwp, atr, atr_r, vol_r
        )
