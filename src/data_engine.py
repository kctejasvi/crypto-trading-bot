"""
Data Engine: Fetches and maintains rolling OHLCV data from the exchange.
"""

import asyncio
import logging
from typing import Dict, Optional
from datetime import datetime, timezone

import ccxt.async_support as ccxt
import pandas as pd

logger = logging.getLogger(__name__)


class DataEngine:
    """
    Fetches OHLCV candles from the exchange and maintains a rolling
    in-memory dataset (last N candles) per symbol.
    """

    def __init__(self, exchange: ccxt.Exchange, config: dict):
        self.exchange = exchange
        self.timeframe = config["trading"]["timeframe"]
        self.limit = config["trading"]["candles_limit"]
        self.symbols = config["trading"]["symbols"]
        # rolling candle store: symbol -> DataFrame
        self._data: Dict[str, pd.DataFrame] = {}

    async def fetch(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        Fetch latest OHLCV candles for a symbol and update the rolling store.
        Returns the updated DataFrame or None on failure.
        """
        try:
            raw = await self.exchange.fetch_ohlcv(
                symbol, timeframe=self.timeframe, limit=self.limit
            )
            if not raw or len(raw) < 60:
                logger.warning("%s: insufficient candle data (%d rows)", symbol, len(raw) if raw else 0)
                return None

            df = pd.DataFrame(
                raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("timestamp", inplace=True)
            df = df.astype(float)

            # Drop the last (in-progress) candle to avoid acting on incomplete data
            df = df.iloc[:-1]

            self._data[symbol] = df
            logger.debug("%s: fetched %d candles, latest: %s", symbol, len(df), df.index[-1])
            return df

        except ccxt.NetworkError as e:
            logger.error("%s: network error fetching OHLCV: %s", symbol, e)
        except ccxt.ExchangeError as e:
            logger.error("%s: exchange error fetching OHLCV: %s", symbol, e)
        except Exception as e:
            logger.exception("%s: unexpected error fetching OHLCV: %s", symbol, e)
        return None

    async def fetch_all(self) -> Dict[str, pd.DataFrame]:
        """Fetch data for all configured symbols concurrently."""
        tasks = [self.fetch(symbol) for symbol in self.symbols]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return {
            sym: df
            for sym, df in zip(self.symbols, results)
            if df is not None
        }

    def get(self, symbol: str) -> Optional[pd.DataFrame]:
        """Return cached data for a symbol."""
        return self._data.get(symbol)

    def latest_price(self, symbol: str) -> Optional[float]:
        """Return the most recent close price for a symbol."""
        df = self._data.get(symbol)
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
        return None
