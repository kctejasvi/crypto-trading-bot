"""
Delta Scanner: Fetches market data from Delta Exchange India (public API, no auth needed)
and scores ETH/BTC futures + options opportunities for the expanded scan.

Returns scored dicts compatible with the signal ranker's display/alert format.
"""

import asyncio
import logging
from typing import List, Optional
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

DELTA_BASE = "https://api.india.delta.exchange"

# Symbols to scan on Delta Exchange
DELTA_FUTURES = ["ETHUSD", "BTCUSD"]
DELTA_OPTIONS_UNDERLYING = ["ETH", "BTC"]


class DeltaOpportunity:
    """A scored trading opportunity from Delta Exchange."""

    def __init__(self, symbol: str, market: str, score: float,
                 reason: str, price: float, iv: Optional[float] = None):
        self.symbol   = symbol
        self.market   = market       # "FUTURES" or "OPTIONS"
        self.score    = score        # 0–100
        self.reason   = reason
        self.price    = price
        self.iv       = iv
        self.exchange = "Delta Exchange India"
        self.label    = f"DELTA:{market}:{symbol}"


class DeltaScanner:
    """
    Scans Delta Exchange India public market data and scores opportunities.

    Scoring model:
      Futures:
        - Trend momentum (mark vs 1h open)    0–30 pts
        - Volume 24h (relative activity)      0–25 pts
        - Price volatility (high-low range)   0–25 pts
        - Funding rate signal                 0–20 pts

      Options:
        - IV in target range (50–90%)         0–35 pts
        - Volume activity                     0–30 pts
        - OI (open interest)                  0–20 pts
        - DTE quality                         0–15 pts
    """

    def __init__(self, config: dict):
        self._iv_min: float = 50.0
        self._iv_max: float = 90.0
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get(self, path: str, params: dict = None) -> Optional[dict]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    DELTA_BASE + path,
                    params=params or {},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except asyncio.TimeoutError:
            logger.warning("Delta API timeout: %s", path)
        except Exception as e:
            logger.warning("Delta API error %s: %s", path, e)
        return None

    async def scan(self) -> List[DeltaOpportunity]:
        """Run full Delta Exchange scan. Returns scored opportunities."""
        futures_task = self._scan_futures()
        options_task = self._scan_options()
        futures_opps, options_opps = await asyncio.gather(
            futures_task, options_task, return_exceptions=True
        )

        results = []
        if isinstance(futures_opps, list):
            results.extend(futures_opps)
        if isinstance(options_opps, list):
            results.extend(options_opps)

        results.sort(key=lambda x: x.score, reverse=True)
        return results

    async def _scan_futures(self) -> List[DeltaOpportunity]:
        data = await self._get("/v2/tickers", {"contract_types": "perpetual_futures"})
        if not data or not data.get("success"):
            return []

        opps = []
        for ticker in data.get("result", []):
            sym = ticker.get("symbol", "")
            if sym not in DELTA_FUTURES:
                continue

            mark_price = float(ticker.get("mark_price") or 0)
            spot_price = float(ticker.get("spot_price") or mark_price)
            vol_24h    = float(ticker.get("volume") or 0)
            high_24h   = float(ticker.get("high") or mark_price)
            low_24h    = float(ticker.get("low") or mark_price)
            funding    = float(ticker.get("funding_rate") or 0)

            if mark_price <= 0:
                continue

            # Score: momentum
            move_pct = abs((mark_price - spot_price) / spot_price * 100) if spot_price else 0
            trend_score = min(30, move_pct * 10)

            # Score: volume (normalised — ETH ~5k-50k contracts)
            vol_norm = min(25, (vol_24h / 10000) * 25)

            # Score: volatility range
            range_pct = ((high_24h - low_24h) / low_24h * 100) if low_24h else 0
            vol_score = min(25, range_pct * 5)

            # Score: funding rate signal (extreme funding = mean reversion opportunity)
            funding_abs = abs(funding) * 100
            funding_score = min(20, funding_abs * 200)

            total = trend_score + vol_norm + vol_score + funding_score

            direction = "LONG" if mark_price > spot_price else "SHORT"
            reason = (
                f"mark={mark_price:.2f} spot={spot_price:.2f} "
                f"range={range_pct:.1f}% vol24h={vol_24h:.0f} funding={funding:.4f}"
            )
            opps.append(DeltaOpportunity(
                symbol=sym, market="FUTURES", score=round(total, 1),
                reason=f"{direction} {reason}", price=mark_price
            ))

        return opps

    async def _scan_options(self) -> List[DeltaOpportunity]:
        data = await self._get("/v2/tickers", {
            "contract_types": "call_options,put_options"
        })
        if not data or not data.get("success"):
            return []

        # Group by underlying + expiry to find straddle setups
        groups: dict = {}
        for ticker in data.get("result", []):
            underlying = ticker.get("underlying_asset_symbol", "")
            if underlying not in DELTA_OPTIONS_UNDERLYING:
                continue

            iv = float(ticker.get("implied_volatility") or 0) * 100
            if iv <= 0:
                continue

            expiry = ticker.get("settlement_time", "")[:10]
            key = f"{underlying}-{expiry}"
            if key not in groups:
                groups[key] = {"ivs": [], "volumes": [], "ois": [], "expiry": expiry, "underlying": underlying}
            groups[key]["ivs"].append(iv)
            groups[key]["volumes"].append(float(ticker.get("volume") or 0))
            groups[key]["ois"].append(float(ticker.get("open_interest") or 0))

        opps = []
        now = datetime.now(timezone.utc)
        for key, g in groups.items():
            avg_iv  = sum(g["ivs"]) / len(g["ivs"]) if g["ivs"] else 0
            avg_vol = sum(g["volumes"]) / len(g["volumes"]) if g["volumes"] else 0
            avg_oi  = sum(g["ois"]) / len(g["ois"]) if g["ois"] else 0

            # IV score: sweet spot 50–90%
            if self._iv_min <= avg_iv <= self._iv_max:
                iv_score = 35
            elif avg_iv < self._iv_min:
                iv_score = max(0, 35 - (self._iv_min - avg_iv) * 0.5)
            else:
                iv_score = max(0, 35 - (avg_iv - self._iv_max) * 0.3)

            # Volume score
            vol_score = min(30, (avg_vol / 100) * 30)

            # OI score
            oi_score = min(20, (avg_oi / 500) * 20)

            # DTE score
            try:
                expiry_dt = datetime.fromisoformat(g["expiry"])
                dte = max(0, (expiry_dt - now.replace(tzinfo=None)).days)
                dte_score = 15 if 2 <= dte <= 7 else max(0, 15 - abs(dte - 4) * 2)
            except Exception:
                dte_score = 0

            total = iv_score + vol_score + oi_score + dte_score
            if total < 20:
                continue

            opps.append(DeltaOpportunity(
                symbol=f"{g['underlying']}-{g['expiry']}",
                market="OPTIONS",
                score=round(total, 1),
                reason=f"avg_iv={avg_iv:.1f}% vol={avg_vol:.0f} oi={avg_oi:.0f}",
                price=0,
                iv=avg_iv
            ))

        return opps
