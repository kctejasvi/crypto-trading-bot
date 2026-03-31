"""
Market Scanner: Discovers the top N USDT pairs by 24h volume on Binance.
Refreshes on a configurable interval so the bot adapts to shifting market activity.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)

# Stablecoins and wrapped tokens to exclude from consideration
_EXCLUDE = {
    "USDT/USDT", "BUSD/USDT", "USDC/USDT", "TUSD/USDT", "FDUSD/USDT",
    "DAI/USDT",  "USDP/USDT", "WBTC/USDT", "WETH/USDT", "WBNB/USDT",
    "BETH/USDT", "BBTC/USDT", "PAXB/USDT", "EUR/USDT",  "GBP/USDT",
}


class MarketScanner:
    """
    Fetches all USDT spot tickers from the exchange and returns the top N
    by 24-hour quote volume (i.e., USDT traded).

    Caches results and only re-fetches after `refresh_interval` seconds.
    """

    def __init__(self, exchange: ccxt.Exchange, config: dict):
        self.exchange = exchange
        scanner_cfg = config.get("scanner", {})
        self.top_n: int = scanner_cfg.get("top_n", 5)
        self.refresh_interval: int = scanner_cfg.get("refresh_interval_seconds", 3600)
        self.min_volume_usdt: float = scanner_cfg.get("min_24h_volume_usdt", 50_000_000)
        self.rank_by: str = scanner_cfg.get("rank_by", "volume")  # "volume" or "change"

        self._symbols: List[str] = []
        self._last_refresh: Optional[datetime] = None

    # ── Public API ──────────────────────────────────────────────────────

    async def get_symbols(self) -> List[str]:
        """
        Return the current top-N symbol list, refreshing from exchange if stale.
        Falls back to previously cached list on error.
        """
        if self._needs_refresh():
            await self._refresh()
        return list(self._symbols)

    def cached_symbols(self) -> List[str]:
        return list(self._symbols)

    async def get_expanded_symbols(self, top_n: int = 20) -> List[str]:
        """
        Force-fetch the top `top_n` symbols regardless of cache.
        Used for the 1-hour idle expanded scan.
        """
        logger.info("MarketScanner: EXPANDED scan — fetching top %d USDT pairs...", top_n)
        try:
            tickers = await self.exchange.fetch_tickers()
        except Exception as e:
            logger.error("MarketScanner expanded scan error: %s", e)
            return self._symbols or []

        candidates = []
        for symbol, ticker in tickers.items():
            if not symbol.endswith("/USDT") or symbol in _EXCLUDE:
                continue
            quote_vol   = float(ticker.get("quoteVolume") or 0)
            change_pct  = float(ticker.get("percentage") or 0)
            last_price  = float(ticker.get("last") or 0)
            if quote_vol < 20_000_000 or last_price <= 0:   # lower threshold for expanded scan
                continue
            candidates.append({"symbol": symbol, "volume": quote_vol, "change_pct": change_pct})

        if not candidates:
            return self._symbols or []

        candidates.sort(key=lambda x: x["volume"], reverse=True)
        symbols = [c["symbol"] for c in candidates[:top_n]]
        logger.warning(
            "MarketScanner: expanded list → %s",
            ", ".join(symbols)
        )
        return symbols

    def last_refresh_time(self) -> Optional[str]:
        if self._last_refresh:
            return self._last_refresh.strftime("%Y-%m-%d %H:%M:%S UTC")
        return None

    # ── Internals ───────────────────────────────────────────────────────

    def _needs_refresh(self) -> bool:
        if not self._symbols or self._last_refresh is None:
            return True
        elapsed = (datetime.now(timezone.utc) - self._last_refresh).total_seconds()
        return elapsed >= self.refresh_interval

    async def _refresh(self):
        logger.info(
            "MarketScanner: fetching tickers to find top %d USDT pairs (rank_by=%s)...",
            self.top_n, self.rank_by,
        )
        try:
            tickers = await self.exchange.fetch_tickers()
        except ccxt.NetworkError as e:
            logger.error("MarketScanner: network error fetching tickers: %s", e)
            return
        except ccxt.ExchangeError as e:
            logger.error("MarketScanner: exchange error fetching tickers: %s", e)
            return
        except Exception as e:
            logger.exception("MarketScanner: unexpected error: %s", e)
            return

        candidates = []
        for symbol, ticker in tickers.items():
            # Only USDT spot pairs
            if not symbol.endswith("/USDT"):
                continue
            if symbol in _EXCLUDE:
                continue

            quote_vol = ticker.get("quoteVolume") or 0.0
            change_pct = ticker.get("percentage") or 0.0
            last_price = ticker.get("last") or 0.0

            # Filter out very low volume and zero/negative prices
            if quote_vol < self.min_volume_usdt or last_price <= 0:
                continue

            candidates.append({
                "symbol": symbol,
                "volume": float(quote_vol),
                "change_pct": float(change_pct),
                "price": float(last_price),
            })

        if not candidates:
            logger.warning("MarketScanner: no valid candidates found — keeping previous list")
            return

        # Rank
        if self.rank_by == "change":
            candidates.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
        else:
            candidates.sort(key=lambda x: x["volume"], reverse=True)

        top = candidates[: self.top_n]
        self._symbols = [c["symbol"] for c in top]
        self._last_refresh = datetime.now(timezone.utc)

        logger.warning(
            "MarketScanner: top %d symbols selected → %s",
            self.top_n,
            ", ".join(
                f"{c['symbol']} (vol=${c['volume']/1e6:.0f}M, {c['change_pct']:+.1f}%)"
                for c in top
            ),
        )
