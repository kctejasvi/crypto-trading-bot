"""
Options Chain (Deribit): Fetches available option contracts and selects
the best strikes/expiry for straddle and strangle setups.

Deribit symbol format: BTC-4APR25-84000-C
Premiums are quoted in the underlying currency (BTC for BTC options).
We convert to USDT using the spot price for risk management.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)

SUPPORTED_UNDERLYINGS = {"BTC", "ETH"}


@dataclass
class OptionContract:
    symbol: str           # e.g. BTC-4APR25-84000-C (Deribit instrument name)
    ccxt_symbol: str      # ccxt unified symbol
    underlying: str       # BTC or ETH
    strike: float
    expiry: datetime
    option_type: str      # "C" or "P"
    bid: float            # in underlying (BTC/ETH)
    ask: float            # in underlying
    mark_price: float     # in underlying
    iv: float = 0.0       # implied volatility %
    delta: float = 0.0

    @property
    def mid_price(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.mark_price

    def mid_price_usdt(self, spot: float) -> float:
        return self.mid_price * spot

    @property
    def days_to_expiry(self) -> float:
        now = datetime.now(timezone.utc)
        expiry = self.expiry
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return max(0.0, (expiry - now).total_seconds() / 86400)


@dataclass
class StraddlePair:
    call: OptionContract
    put: OptionContract
    strike: float
    call_strike: float
    put_strike: float
    total_premium_underlying: float    # in BTC or ETH
    total_premium_usdt: float          # converted to USDT
    underlying_price: float
    strategy: str                      # "straddle" or "strangle"

    @property
    def breakeven_up(self) -> float:
        return self.call_strike + self.total_premium_underlying

    @property
    def breakeven_down(self) -> float:
        return self.put_strike - self.total_premium_underlying

    @property
    def move_needed_pct(self) -> float:
        return self.total_premium_underlying / self.underlying_price * 100


class OptionsChain:
    """
    Fetches Deribit option contracts and builds straddle/strangle pairs.
    """

    def __init__(self, exchange: ccxt.Exchange, config: dict):
        self.exchange = exchange
        opts_cfg = config.get("options", {})
        self.min_dte: int = opts_cfg.get("min_dte", 1)
        self.max_dte: int = opts_cfg.get("max_dte", 7)
        self.strangle_otm_pct: float = opts_cfg.get("strangle_otm_pct", 2.0)
        self.min_iv: float = opts_cfg.get("min_iv", 0.0)
        self.max_iv: float = opts_cfg.get("max_iv", 200.0)

    async def get_straddle(
        self, underlying: str, spot_price: float, strategy: str = "strangle"
    ) -> Optional[StraddlePair]:
        """
        Find the best straddle or strangle pair on Deribit for the given underlying.
        """
        underlying = underlying.replace("/USDT", "").replace("USDT", "").upper()
        if underlying not in SUPPORTED_UNDERLYINGS:
            logger.warning("Options not supported for %s on Deribit", underlying)
            return None

        calls, puts = await self._fetch_contracts(underlying, spot_price)
        if not calls or not puts:
            logger.warning("%s: no valid Deribit contracts found (DTE %d–%d)", underlying, self.min_dte, self.max_dte)
            return None

        if strategy == "straddle":
            return self._build_straddle(calls, puts, spot_price, underlying)
        else:
            return self._build_strangle(calls, puts, spot_price, underlying)

    # ── Contract fetching ────────────────────────────────────────────────

    async def _fetch_contracts(
        self, underlying: str, spot_price: float
    ) -> tuple:
        """Fetch all option markets for the underlying, filter by DTE."""
        try:
            markets = await self.exchange.fetch_markets({
                "currency": underlying,
                "kind": "option",
            })
        except Exception as e:
            logger.error("Deribit: failed to fetch option markets for %s: %s", underlying, e)
            return [], []

        calls, puts = [], []
        instrument_names = []

        for m in markets:
            # ccxt unified symbol: "BTC/USD:BTC-260401-84000-C"
            sym = m.get("symbol", "")
            info = m.get("info", {})

            # Strip the "BTC/USD:" prefix to get "BTC-260401-84000-C"
            instrument = sym.split(":")[-1] if ":" in sym else sym
            parts = instrument.split("-")
            if len(parts) != 4:
                continue

            und, expiry_str, strike_str, opt_type = parts
            if und.upper() != underlying:
                continue
            if opt_type not in ("C", "P"):
                continue

            try:
                strike = float(strike_str)
                expiry = self._parse_deribit_expiry(expiry_str)
            except Exception:
                continue

            dte = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
            if not (self.min_dte <= dte <= self.max_dte):
                continue

            instrument_names.append((sym, strike, expiry, opt_type, instrument))

        if not instrument_names:
            return [], []

        # Deribit: fetch all option tickers for the currency in one call
        tickers = {}
        try:
            tickers = await self.exchange.fetch_tickers(
                None, {"currency": underlying, "kind": "option"}
            )
            logger.debug("%s: fetched %d option tickers from Deribit", underlying, len(tickers))
        except Exception as e:
            logger.warning("Deribit: fetch_tickers failed for %s: %s", underlying, e)

        for sym, strike, expiry, opt_type, instrument in instrument_names:
            ticker = tickers.get(sym, {})
            info = ticker.get("info", {})

            bid        = float(ticker.get("bid")  or info.get("best_bid_price")  or 0)
            ask        = float(ticker.get("ask")  or info.get("best_ask_price")  or 0)
            mark_price = float(ticker.get("last") or info.get("mark_price") or 0)
            iv         = float(info.get("mark_iv") or 0)
            delta      = float(info.get("greeks", {}).get("delta") or 0)

            if mark_price <= 0 and bid <= 0:
                continue
            if not (self.min_iv <= iv <= self.max_iv) and iv > 0:
                continue

            contract = OptionContract(
                symbol=instrument,     # short name e.g. BTC-260401-84000-C
                ccxt_symbol=sym,       # full ccxt symbol for API calls
                underlying=underlying,
                strike=strike,
                expiry=expiry,
                option_type=opt_type,
                bid=bid,
                ask=ask,
                mark_price=mark_price,
                iv=iv,
                delta=delta,
            )

            if opt_type == "C":
                calls.append(contract)
            else:
                puts.append(contract)

        logger.info(
            "%s options chain: %d calls, %d puts (DTE %d–%d)",
            underlying, len(calls), len(puts), self.min_dte, self.max_dte,
        )
        return calls, puts

    # ── Strike selection ─────────────────────────────────────────────────

    def _build_straddle(
        self, calls: list, puts: list, spot: float, underlying: str
    ) -> Optional[StraddlePair]:
        """ATM straddle: same strike closest to spot for both legs."""
        call_strikes = {c.strike for c in calls}
        put_strikes  = {p.strike for p in puts}
        common = sorted(call_strikes & put_strikes)
        if not common:
            return None

        atm = min(common, key=lambda s: abs(s - spot))
        call = self._pick(calls, atm)
        put  = self._pick(puts,  atm)
        if not call or not put:
            return None

        total_prem = call.mid_price + put.mid_price
        total_usdt = total_prem * spot

        logger.info(
            "%s STRADDLE: strike=%.0f  call=%.6f BTC  put=%.6f BTC  "
            "total=%.6f BTC ($%.2f)  move_needed=±%.2f%%  DTE=%.1fd  IV_call=%.1f%%",
            underlying, atm,
            call.mid_price, put.mid_price, total_prem, total_usdt,
            total_prem / spot * 100, call.days_to_expiry, call.iv,
        )
        return StraddlePair(
            call=call, put=put,
            strike=atm, call_strike=atm, put_strike=atm,
            total_premium_underlying=total_prem,
            total_premium_usdt=total_usdt,
            underlying_price=spot,
            strategy="straddle",
        )

    def _build_strangle(
        self, calls: list, puts: list, spot: float, underlying: str
    ) -> Optional[StraddlePair]:
        """OTM strangle: call above spot, put below spot by otm_pct."""
        otm = self.strangle_otm_pct / 100.0
        target_call = spot * (1 + otm)
        target_put  = spot * (1 - otm)

        call_strikes = sorted([c.strike for c in calls if c.strike > spot])
        put_strikes  = sorted([p.strike for p in puts  if p.strike < spot], reverse=True)

        if not call_strikes or not put_strikes:
            logger.warning(
                "%s strangle: not enough OTM strikes (calls=%d puts=%d)",
                underlying, len(call_strikes), len(put_strikes),
            )
            return None

        call_strike = min(call_strikes, key=lambda s: abs(s - target_call))
        put_strike  = min(put_strikes,  key=lambda s: abs(s - target_put))

        call = self._pick(calls, call_strike)
        put  = self._pick(puts,  put_strike)
        if not call or not put:
            return None

        total_prem = call.mid_price + put.mid_price
        total_usdt = total_prem * spot

        logger.info(
            "%s STRANGLE: call_strike=%.0f@%.6f BTC  put_strike=%.0f@%.6f BTC  "
            "total=%.6f BTC ($%.2f)  move_needed call=+%.2f%% put=-%.2f%%  "
            "DTE=%.1fd  IV_call=%.1f%%  IV_put=%.1f%%",
            underlying,
            call_strike, call.mid_price, put_strike, put.mid_price,
            total_prem, total_usdt,
            (call_strike - spot) / spot * 100, (spot - put_strike) / spot * 100,
            call.days_to_expiry, call.iv, put.iv,
        )
        return StraddlePair(
            call=call, put=put,
            strike=(call_strike + put_strike) / 2,
            call_strike=call_strike, put_strike=put_strike,
            total_premium_underlying=total_prem,
            total_premium_usdt=total_usdt,
            underlying_price=spot,
            strategy="strangle",
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _pick(contracts: list, strike: float) -> Optional[OptionContract]:
        matches = [c for c in contracts if c.strike == strike]
        return matches[0] if matches else None

    @staticmethod
    def _parse_deribit_expiry(expiry_str: str) -> datetime:
        """
        Parse Deribit expiry string in YYMMDD format (e.g. '260401' = 2026-04-01).
        Deribit options expire at 08:00 UTC.
        """
        return datetime.strptime(expiry_str, "%y%m%d").replace(
            hour=8, minute=0, second=0, tzinfo=timezone.utc
        )
