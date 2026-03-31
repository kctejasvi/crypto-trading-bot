"""
Signal Ranker: Scores and ranks all opportunities from Binance spot and
Deribit options, returning the highest-probability trade to execute.

Scoring model (0–100):

SPOT (Binance momentum):
  - Trend strength:    EMA separation %          (0–25 pts)
  - Breakout margin:   how far above 20c high    (0–20 pts)
  - Volume surge:      vol / avg_vol ratio        (0–20 pts)
  - RSI position:      closeness to 60 (BUY)     (0–20 pts)
  - ATR size:          volatility room            (0–15 pts)

OPTIONS (Deribit straddle/strangle):
  - Squeeze depth:     BB width percentile        (0–30 pts)
  - Compression:       ATR ratio below average    (0–25 pts)
  - Premium value:     IV vs move_needed ratio    (0–20 pts)
  - DTE quality:       2–5d sweet spot            (0–15 pts)
  - Volume declining:  pre-breakout quiet         (0–10 pts)
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from .indicator_engine import Indicators
from .options_chain import StraddlePair
from .options_strategy import OptionsSignalResult, OptionsSignal
from .strategy_engine import SignalResult, Signal

logger = logging.getLogger(__name__)


class TradeType(Enum):
    SPOT    = "SPOT"
    OPTIONS = "OPTIONS"


@dataclass
class RankedOpportunity:
    trade_type: TradeType
    symbol: str
    score: float                          # 0–100
    score_breakdown: dict                 # component scores
    confidence: str                       # "HIGH" / "MEDIUM" / "LOW"

    # Spot fields (populated when trade_type == SPOT)
    spot_signal: Optional[SignalResult] = None
    indicators: Optional[Indicators] = None

    # Options fields (populated when trade_type == OPTIONS)
    options_signal: Optional[OptionsSignalResult] = None
    options_pair: Optional[StraddlePair] = None

    @property
    def label(self) -> str:
        if self.trade_type == TradeType.SPOT:
            side = self.spot_signal.signal.value if self.spot_signal else "?"
            return f"SPOT {side} {self.symbol}"
        else:
            strategy = self.options_pair.strategy.upper() if self.options_pair else "OPTIONS"
            return f"OPTIONS {strategy} {self.symbol}"


class SignalRanker:
    """
    Aggregates spot and options signals, scores them, and returns
    the ranked list so the main loop can act on the best opportunity.
    """

    CONFIDENCE_HIGH   = 75.0
    CONFIDENCE_MEDIUM = 50.0

    def rank(
        self,
        spot_signals: List[SignalResult],
        options_signals: List[tuple],   # list of (OptionsSignalResult, StraddlePair | None)
    ) -> List[RankedOpportunity]:
        """
        Score all valid signals and return sorted list (highest score first).
        Filters out NONE signals and unpaired options.
        """
        opportunities: List[RankedOpportunity] = []

        # ── Score spot signals ───────────────────────────────────────────
        for sig in spot_signals:
            if sig.signal == Signal.NONE or sig.indicators is None:
                continue
            score, breakdown = self._score_spot(sig)
            opportunities.append(RankedOpportunity(
                trade_type=TradeType.SPOT,
                symbol=sig.symbol,
                score=score,
                score_breakdown=breakdown,
                confidence=self._confidence(score),
                spot_signal=sig,
                indicators=sig.indicators,
            ))

        # ── Score options signals ────────────────────────────────────────
        for opt_sig, pair in options_signals:
            if opt_sig.signal == OptionsSignal.NONE or pair is None:
                continue
            score, breakdown = self._score_options(opt_sig, pair)
            opportunities.append(RankedOpportunity(
                trade_type=TradeType.OPTIONS,
                symbol=opt_sig.symbol,
                score=score,
                score_breakdown=breakdown,
                confidence=self._confidence(score),
                options_signal=opt_sig,
                options_pair=pair,
            ))

        # Sort highest score first
        opportunities.sort(key=lambda o: o.score, reverse=True)

        if opportunities:
            self._log_ranking(opportunities)

        return opportunities

    # ── Spot scoring ─────────────────────────────────────────────────────

    def _score_spot(self, sig: SignalResult) -> tuple:
        ind = sig.indicators
        breakdown = {}

        # 1. Trend strength: EMA separation as % of price (max 25 pts)
        ema_sep_pct = abs(ind.ema_fast - ind.ema_slow) / ind.close * 100
        breakdown["trend"] = min(25.0, ema_sep_pct * 25)   # 1% sep = full score

        # 2. Breakout margin: how far close is above/below the 20c level (max 20 pts)
        if sig.signal == Signal.BUY:
            margin_pct = (ind.close - ind.high_20) / ind.close * 100
        else:
            margin_pct = (ind.low_20 - ind.close) / ind.close * 100
        breakdown["breakout"] = min(20.0, max(0.0, margin_pct * 400))  # 0.05% = full

        # 3. Volume surge: vol_ratio above 1.5x threshold (max 20 pts)
        vol_excess = max(0.0, ind.volume_ratio - 1.5)
        breakdown["volume"] = min(20.0, vol_excess * 20)   # +1.0x = full score

        # 4. RSI quality: closeness to 60 for BUY / 37.5 for SELL (max 20 pts)
        if sig.signal == Signal.BUY:
            rsi_distance = abs(ind.rsi - 60.0)
        else:
            rsi_distance = abs(ind.rsi - 37.5)
        breakdown["rsi"] = max(0.0, 20.0 - rsi_distance * 1.33)  # 15 pts away = 0

        # 5. ATR as % of price — more volatility = more profit potential (max 15 pts)
        atr_pct = ind.atr / ind.close * 100
        breakdown["atr"] = min(15.0, atr_pct * 7.5)  # 2% ATR = full

        total = sum(breakdown.values())
        return round(total, 2), breakdown

    # ── Options scoring ──────────────────────────────────────────────────

    def _score_options(self, sig: OptionsSignalResult, pair: StraddlePair) -> tuple:
        breakdown = {}

        # 1. Squeeze depth: lower BB percentile = tighter squeeze (max 30 pts)
        # 20th pct → 30 pts, 35th pct → 0 pts
        squeeze_score = max(0.0, (35.0 - sig.bb_width_percentile) / 35.0 * 30)
        breakdown["squeeze"] = round(squeeze_score, 2)

        # 2. ATR compression: ratio below 1.0 means calmer than average (max 25 pts)
        # ratio=0.5 → 25 pts, ratio=0.85 → 0 pts
        compression = max(0.0, (0.85 - sig.atr_ratio) / 0.85 * 25)
        breakdown["atr_compression"] = round(compression, 2)

        # 3. Premium value: IV vs move_needed — lower move needed relative to IV = better
        # if IV implies a 5% move but we only need 2%, that's great (max 20 pts)
        implied_move_pct = pair.call.iv / 100 * (pair.call.days_to_expiry / 365) ** 0.5 * 100
        if pair.move_needed_pct > 0 and implied_move_pct > 0:
            ratio = implied_move_pct / max(pair.move_needed_pct, 0.01)
            breakdown["premium_value"] = round(min(20.0, ratio * 5), 2)
        else:
            breakdown["premium_value"] = 5.0

        # 4. DTE quality: 2–5 day window is ideal (max 15 pts)
        dte = pair.call.days_to_expiry
        if 2.0 <= dte <= 5.0:
            breakdown["dte"] = 15.0
        elif 1.0 <= dte < 2.0:
            breakdown["dte"] = round(10.0 * (dte - 1.0), 2)
        elif 5.0 < dte <= 7.0:
            breakdown["dte"] = round(15.0 - (dte - 5.0) * 3.75, 2)
        else:
            breakdown["dte"] = 0.0

        # 5. Pre-breakout quiet: low volume confirms accumulation (max 10 pts)
        vol_quiet = max(0.0, 1.0 - sig.volume_ratio)
        breakdown["vol_quiet"] = round(min(10.0, vol_quiet * 20), 2)

        total = sum(breakdown.values())
        return round(total, 2), breakdown

    # ── Helpers ──────────────────────────────────────────────────────────

    def _confidence(self, score: float) -> str:
        if score >= self.CONFIDENCE_HIGH:
            return "HIGH"
        if score >= self.CONFIDENCE_MEDIUM:
            return "MEDIUM"
        return "LOW"

    def _log_ranking(self, ranked: List[RankedOpportunity]):
        logger.info("─── Signal Ranking (%d opportunities) ───", len(ranked))
        for i, opp in enumerate(ranked, 1):
            logger.info(
                "  #%d  %-35s  score=%.1f  confidence=%s  breakdown=%s",
                i, opp.label, opp.score, opp.confidence,
                {k: round(v, 1) for k, v in opp.score_breakdown.items()},
            )
        best = ranked[0]
        logger.warning(
            "  → BEST: %s  score=%.1f  [%s]",
            best.label, best.score, best.confidence,
        )
