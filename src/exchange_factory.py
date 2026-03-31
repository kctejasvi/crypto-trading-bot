"""
Exchange Factory: Creates and configures ccxt exchange instances.
Spot trading → Binance
Options trading → Deribit
"""

import logging
import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)


def create_exchange(config: dict) -> ccxt.Exchange:
    """Build the Binance spot exchange."""
    exc_cfg = config["exchange"]
    paper = config.get("paper_trading", True)
    live_confirmed = config.get("live_trading_confirmed", False)

    exchange: ccxt.Exchange = ccxt.binance(
        {
            "apiKey": exc_cfg["api_key"],
            "secret": exc_cfg["api_secret"],
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )

    use_testnet = exc_cfg.get("testnet", True) or paper
    if use_testnet:
        if hasattr(exchange, "set_sandbox_mode"):
            exchange.set_sandbox_mode(True)
            logger.info("Binance spot exchange: SANDBOX/TESTNET mode")
        else:
            logger.warning("Binance does not support sandbox — paper mode simulates locally")
    else:
        if not live_confirmed:
            raise ValueError("LIVE trading requires live_trading_confirmed=true in config.yaml")
        logger.warning("Binance exchange: LIVE mode — real funds at risk!")

    return exchange


def create_deribit_exchange(config: dict) -> ccxt.Exchange:
    """
    Build the Deribit exchange for options trading.
    Deribit testnet is at test.deribit.com — enabled when paper_trading=true.
    """
    deribit_cfg = config.get("deribit", {})
    paper = config.get("paper_trading", True)
    live_confirmed = config.get("live_trading_confirmed", False)

    exchange: ccxt.Exchange = ccxt.deribit(
        {
            "apiKey": deribit_cfg.get("api_key", ""),
            "secret": deribit_cfg.get("api_secret", ""),
            "enableRateLimit": True,
        }
    )

    if paper:
        # Deribit testnet uses a different URL
        exchange.set_sandbox_mode(True)
        logger.info("Deribit options exchange: TESTNET mode (test.deribit.com)")
    else:
        if not live_confirmed:
            raise ValueError("LIVE options trading requires live_trading_confirmed=true")
        logger.warning("Deribit exchange: LIVE mode — real funds at risk!")

    return exchange
