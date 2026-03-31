"""
Main Loop: Orchestrates the trading bot — fetch → indicators → strategy → risk → execute.
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .data_engine import DataEngine
from .exchange_factory import create_deribit_exchange, create_exchange
from .execution_engine import ExecutionEngine
from .indicator_engine import IndicatorEngine
from .logger import TradeLogger
from .market_scanner import MarketScanner
from .options_chain import OptionsChain
from .options_execution import OptionsExecutionEngine
from .options_manager import OptionsManager
from .options_strategy import OptionsSignal, OptionsStrategyEngine
from .risk_engine import RiskEngine
from .signal_ranker import RankedOpportunity, SignalRanker, TradeType
from .strategy_engine import Signal, StrategyEngine
from .aggressive_strategy import AggressiveStrategy
from .telegram_bot import TelegramNotifier
from .trade_manager import TradeManager

# ── Logging setup ───────────────────────────────────────────────────────────────


def setup_logging(config: dict):
    log_dir = Path(config["logging"]["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config["logging"].get("log_level", "INFO").upper(), logging.INFO)

    try:
        import colorlog
        handler = colorlog.StreamHandler()
        handler.setFormatter(
            colorlog.ColoredFormatter(
                "%(log_color)s%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    except ImportError:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    file_handler = logging.FileHandler(log_dir / "bot.log")
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    root.addHandler(file_handler)


# ── Bot class ───────────────────────────────────────────────────────────────────


class TradingBot:
    def __init__(self, config: dict):
        self.config = config
        self.running = False
        self.loop_interval = config["trading"]["loop_interval"]
        self._fallback_symbols = config["trading"]["symbols"]
        self._active_symbols: list = []
        self._last_daily_summary: Optional[str] = None
        self._scanner_enabled: bool = config.get("scanner", {}).get("enabled", False)

        exchange = create_exchange(config)
        deribit  = create_deribit_exchange(config)

        self.data = DataEngine(exchange, config)
        self.indicators = IndicatorEngine(config)
        self.strategy = StrategyEngine(config)
        self.risk = RiskEngine(config)
        self.execution = ExecutionEngine(exchange, config)
        self.trade_mgr = TradeManager(self.execution, self.risk, config)
        self.trade_logger = TradeLogger(config)
        self.telegram = TelegramNotifier(config)
        self.scanner = MarketScanner(exchange, config)

        # Options modules — Deribit exchange
        opts_cfg = config.get("options", {})
        self._options_enabled: bool = opts_cfg.get("enabled", False)
        self._options_symbols: list = opts_cfg.get("underlying_symbols", ["BTC/USDT", "ETH/USDT"])
        self.options_chain    = OptionsChain(deribit, config)
        self.options_strategy = OptionsStrategyEngine(config)
        self.options_exec     = OptionsExecutionEngine(deribit, config)
        self.options_mgr      = OptionsManager(self.options_exec, config)
        self.ranker           = SignalRanker()

        self._exchange = exchange
        self._deribit  = deribit
        self._last_trade_time: Optional[datetime] = datetime.now(timezone.utc)
        self._idle_expanded_hours: float = 1.0   # trigger expanded scan after 1hr no trade
        self.aggressive = AggressiveStrategy()

        log = logging.getLogger(__name__)
        mode = "PAPER" if config.get("paper_trading", True) else "LIVE"
        log.warning("=" * 60)
        log.warning("  Crypto Trading Bot starting in %s mode", mode)
        log.warning("  Market scanner: %s", "ENABLED" if self._scanner_enabled else "DISABLED")
        log.warning("  Options trading: %s", "ENABLED" if self._options_enabled else "DISABLED")
        if self._options_enabled:
            strategy_type = opts_cfg.get("strategy", "strangle").upper()
            log.warning("  Options strategy: %s", strategy_type)
        log.warning("=" * 60)

    # ── Main entry ──────────────────────────────────────────────────────

    async def run(self):
        self.running = True
        logger = logging.getLogger(__name__)
        self.telegram.register_expanded_scan(self._expanded_scan_trigger)
        self.telegram.register_aggressive_strategy(self.aggressive, self._aggressive_execute_trigger)
        asyncio.ensure_future(self.telegram.start_command_listener())
        try:
            while self.running:
                cycle_start = asyncio.get_event_loop().time()
                try:
                    await self._cycle()
                except Exception as e:
                    logger.exception("Unhandled error in main cycle: %s", e)

                elapsed = asyncio.get_event_loop().time() - cycle_start
                sleep_for = max(0.0, self.loop_interval - elapsed)
                logger.debug("Cycle took %.1fs — sleeping %.1fs", elapsed, sleep_for)
                await asyncio.sleep(sleep_for)
        finally:
            await self._shutdown()

    def stop(self):
        logging.getLogger(__name__).info("Stop signal received")
        self.running = False

    # ── Single scan cycle ───────────────────────────────────────────────

    async def _cycle(self):
        logger = logging.getLogger(__name__)
        now = datetime.now(timezone.utc)
        logger.info("━━━ Cycle %s ━━━", now.strftime("%Y-%m-%d %H:%M:%S UTC"))

        # 1. Resolve active symbols via market scanner
        symbols = await self._resolve_symbols()
        if not symbols:
            logger.warning("No symbols available — skipping cycle")
            return

        # 2. Fetch balance
        balance = await self.execution.get_balance()
        if balance <= 0:
            logger.warning("Balance unavailable — skipping cycle")
            return

        # 3. Daily summary
        await self._maybe_send_daily_summary()

        # 4. Fetch OHLCV for all symbols
        self.data.symbols = list(set(symbols + self._options_symbols))
        data_map = await self.data.fetch_all()
        if not data_map:
            logger.warning("No OHLCV data — skipping cycle")
            return

        # 5. Manage existing open trades (both spot and options)
        await self._manage_open_trades(symbols, data_map, balance)

        # 6. Check risk gates
        ok, reason = self.risk.can_trade(balance)
        if not ok:
            logger.info("Risk gate: %s — no new trades this cycle", reason)
            await self.telegram.send_risk_alert(reason)
            return

        # 7. Expanded scan if idle > 1 hour (checks all coins + Delta Exchange)
        if self._idle_hours() >= self._idle_expanded_hours:
            await self._expanded_scan(balance)
            return

        # 7. Gather all signals from BOTH Binance + Deribit in parallel
        spot_signals, options_candidates = await asyncio.gather(
            self._gather_spot_signals(symbols, data_map),
            self._gather_options_signals(data_map),
        )

        # 8. Rank everything — pick highest probability trade
        ranked = self.ranker.rank(spot_signals, options_candidates)
        if not ranked:
            logger.info("No tradeable signals this cycle")
            return

        best = ranked[0]
        if best.confidence == "LOW":
            logger.info("Best signal confidence is LOW (score=%.1f) — skipping", best.score)
            return

        logger.warning(
            "EXECUTING: %s  score=%.1f  confidence=%s",
            best.label, best.score, best.confidence,
        )

        # 9. Execute the best opportunity
        if best.trade_type == TradeType.SPOT:
            await self._execute_spot(best, balance, data_map)
        else:
            await self._execute_options(best, balance)
        self._last_trade_time = datetime.now(timezone.utc)

    # ── Manage existing trades ───────────────────────────────────────────

    async def _manage_open_trades(self, symbols: list, data_map: dict, balance: float):
        logger = logging.getLogger(__name__)

        # Spot trades
        for symbol in symbols:
            if not self.trade_mgr.has_open_trade(symbol):
                continue
            df = data_map.get(symbol)
            if df is None:
                continue
            price = float(df["close"].iloc[-1])
            ind = self.indicators.calculate(symbol, df)
            atr = ind.atr if ind else 0.0
            result = await self.trade_mgr.update(symbol, price, atr)
            if result:
                self.trade_logger.log_trade(result)
                await self.telegram.send_trade_exit(result)
                if result["reason"] == "SL_HIT":
                    await self.telegram.send_sl_hit(symbol, result["exit_price"], result["pnl"])
                elif result["reason"] == "TP_HIT":
                    await self.telegram.send_tp_hit(symbol, result["exit_price"], result["pnl"])

        # Options trades
        for symbol in self._options_symbols:
            if not self.options_mgr.has_open_trade(symbol):
                continue
            df = data_map.get(symbol)
            price = float(df["close"].iloc[-1]) if df is not None else 0.0
            result = await self.options_mgr.update(symbol, price)
            if result:
                self.trade_logger.log_trade(result)
                await self.telegram.send_trade_exit(result)

    # ── Signal gathering ─────────────────────────────────────────────────

    async def _gather_spot_signals(self, symbols: list, data_map: dict) -> list:
        """Evaluate spot momentum strategy (+ aggressive if enabled) on all symbols."""
        signals = []
        for symbol in symbols:
            if self.trade_mgr.has_open_trade(symbol):
                continue
            df = data_map.get(symbol)
            if df is None:
                continue
            ind = self.indicators.calculate(symbol, df)
            if ind is None:
                continue

            # Aggressive strategy overrides normal when enabled and finds a signal
            if self.aggressive.enabled:
                aggr_sig = self.aggressive.evaluate(ind)
                if aggr_sig is not None and aggr_sig.signal != Signal.NONE:
                    self.trade_logger.log_signal(symbol, aggr_sig.signal.value, aggr_sig.reason)
                    signals.append(aggr_sig)
                    continue   # skip normal strategy for this symbol

            sig = self.strategy.evaluate(ind)
            self.trade_logger.log_signal(symbol, sig.signal.value, sig.reason)
            signals.append(sig)
        return signals

    async def _gather_options_signals(self, data_map: dict) -> list:
        """Evaluate volatility squeeze on options symbols and fetch chains."""
        if not self._options_enabled:
            return []

        candidates = []
        tasks = []
        pending_signals = []

        for symbol in self._options_symbols:
            if self.options_mgr.has_open_trade(symbol):
                continue
            df = data_map.get(symbol)
            if df is None:
                continue
            sig = self.options_strategy.evaluate(symbol, df)
            if sig.signal == OptionsSignal.NONE:
                candidates.append((sig, None))
                continue

            spot = float(df["close"].iloc[-1])
            chosen = "straddle" if sig.signal == OptionsSignal.ENTER_STRADDLE else "strangle"
            underlying = symbol.replace("/USDT", "")
            tasks.append(self.options_chain.get_straddle(underlying, spot, chosen))
            pending_signals.append(sig)

        if tasks:
            pairs = await asyncio.gather(*tasks, return_exceptions=True)
            for sig, pair in zip(pending_signals, pairs):
                if isinstance(pair, Exception):
                    pair = None
                candidates.append((sig, pair))

        return candidates

    # ── Execution ────────────────────────────────────────────────────────

    async def _execute_spot(self, opp: RankedOpportunity, balance: float, data_map: dict):
        logger = logging.getLogger(__name__)
        symbol = opp.symbol
        ind = opp.indicators
        df = data_map.get(symbol)
        if df is None or ind is None:
            return

        side = "buy" if opp.spot_signal.signal == Signal.BUY else "sell"
        current_price = float(df["close"].iloc[-1])

        # Use aggressive risk params if aggressive mode is active
        if self.aggressive.enabled:
            from .aggressive_strategy import AggressiveStrategy as AS
            orig_risk_pct = self.risk.capital_risk_pct
            orig_sl       = self.risk.sl_multiplier
            orig_tp       = self.risk.tp_multiplier
            self.risk.capital_risk_pct = AS.CAPITAL_RISK_PCT
            self.risk.sl_multiplier    = AS.SL_MULTIPLIER
            self.risk.tp_multiplier    = AS.TP_MULTIPLIER

        risk_params = self.risk.calculate(
            symbol=symbol, side=side,
            entry_price=current_price, atr=ind.atr, balance=balance,
        )

        if self.aggressive.enabled:
            self.risk.capital_risk_pct = orig_risk_pct
            self.risk.sl_multiplier    = orig_sl
            self.risk.tp_multiplier    = orig_tp
        if risk_params is None:
            logger.warning("%s: risk params invalid — skipping", symbol)
            return

        order_result = await self.execution.open_position(risk_params)
        if order_result is None:
            logger.error("%s: spot order execution failed", symbol)
            return

        self.trade_mgr.open_trade(order_result, ind.atr)
        await self.telegram.send_trade_entry(order_result)

    async def _execute_options(self, opp: RankedOpportunity, balance: float):
        logger = logging.getLogger(__name__)
        pair = opp.options_pair
        if pair is None:
            return

        trade = await self.options_exec.enter_position(pair, balance)
        if trade is None:
            logger.error("%s: options entry failed", opp.symbol)
            return

        self.options_mgr.register(trade)
        await self.telegram.send_risk_alert(
            f"OPTIONS ENTRY [{trade.strategy.upper()}]  score={opp.score:.1f}\n"
            f"Symbol: {opp.symbol}\n"
            f"Call: {trade.call_strike:.0f} @ {trade.call_premium:.6f} {trade.underlying}\n"
            f"Put:  {trade.put_strike:.0f} @ {trade.put_premium:.6f} {trade.underlying}\n"
            f"Cost: ${trade.total_cost_usdt:.2f}  Move needed: ±{pair.move_needed_pct:.2f}%\n"
            f"TP @ 2x | SL @ 0.5x | Exit < 4h DTE"
        )

    async def _expanded_scan_trigger(self):
        """Called by Telegram /expand command — fetches balance and runs expanded scan."""
        balance = await self.execution.get_balance()
        if balance <= 0:
            await self.telegram.send_risk_alert("⚠️ Cannot run expanded scan — balance unavailable.")
            return
        await self._expanded_scan(balance)

    async def _aggressive_execute_trigger(self):
        """Called immediately when /aggressive on is sent — scans and executes right away."""
        logger = logging.getLogger(__name__)

        balance = await self.execution.get_balance()
        if balance <= 0:
            await self.telegram.send_risk_alert("⚠️ Balance unavailable — cannot execute.")
            return

        ok, reason = self.risk.can_trade(balance)
        if not ok:
            await self.telegram.send_risk_alert(f"⚠️ Risk gate blocked: {reason}")
            return

        # Scan top 20 symbols with aggressive strategy
        symbols = await self.scanner.get_expanded_symbols(top_n=20)
        self.data.symbols = list(set(symbols + self._options_symbols))
        data_map = await self.data.fetch_all()

        best_sig = None
        best_score = -1.0

        for symbol in symbols:
            if self.trade_mgr.has_open_trade(symbol):
                continue
            df = data_map.get(symbol)
            if df is None:
                continue
            ind = self.indicators.calculate(symbol, df)
            if ind is None:
                continue
            sig = self.aggressive.evaluate(ind)
            if sig is None or sig.signal == Signal.NONE:
                continue
            if sig.aggr_score > best_score:
                best_score = sig.aggr_score
                best_sig = sig

        if best_sig is None:
            await self.telegram.send_risk_alert(
                "⚡ *AGGRESSIVE SCAN COMPLETE*\n\n"
                "❌ No signal found across 20 coins.\n"
                "Aggressive mode is ON — will execute on next cycle when signal appears."
            )
            return

        logger.warning(
            "AGGRESSIVE EXECUTE: %s %s score=%.1f mode=%s",
            best_sig.signal.value, best_sig.symbol, best_sig.aggr_score, best_sig.mode.value
        )

        # Build a ranked opportunity from the aggressive signal
        from .signal_ranker import RankedOpportunity, TradeType
        from .aggressive_strategy import AggressiveStrategy as AS

        opp = RankedOpportunity(
            trade_type=TradeType.SPOT,
            symbol=best_sig.symbol,
            score=best_sig.aggr_score,
            score_breakdown={"aggressive": best_sig.aggr_score},
            confidence="HIGH" if best_sig.aggr_score >= 75 else "MEDIUM",
            spot_signal=best_sig,
            indicators=best_sig.indicators,
        )

        # Apply aggressive risk overrides
        orig_risk_pct = self.risk.capital_risk_pct
        orig_sl       = self.risk.sl_multiplier
        orig_tp       = self.risk.tp_multiplier
        self.risk.capital_risk_pct = AS.CAPITAL_RISK_PCT
        self.risk.sl_multiplier    = AS.SL_MULTIPLIER
        self.risk.tp_multiplier    = AS.TP_MULTIPLIER

        await self._execute_spot(opp, balance, data_map)

        self.risk.capital_risk_pct = orig_risk_pct
        self.risk.sl_multiplier    = orig_sl
        self.risk.tp_multiplier    = orig_tp
        self._last_trade_time = datetime.now(timezone.utc)

    def _idle_hours(self) -> float:
        """Hours elapsed since last trade was executed."""
        if self._last_trade_time is None:
            return 0.0
        return (datetime.now(timezone.utc) - self._last_trade_time).total_seconds() / 3600

    async def _expanded_scan(self, balance: float):
        """
        Triggered after 1hr with no trade.
        Scans top-20 Binance pairs + all Deribit option underlyings + Delta Exchange.
        Sends a Telegram alert with the best opportunity found across all markets.
        """
        logger = logging.getLogger(__name__)
        logger.warning("EXPANDED SCAN triggered — no trade for %.1fh", self._idle_hours())

        # 1. Binance top-20
        expanded_symbols = await self.scanner.get_expanded_symbols(top_n=20)
        self.data.symbols = list(set(expanded_symbols + self._options_symbols))
        data_map = await self.data.fetch_all()

        # 2. Score Binance spot signals on all 20 symbols
        spot_signals = await self._gather_spot_signals(expanded_symbols, data_map)

        # 3. Score Deribit options on all available symbols
        all_deribit_symbols = list(set(self._options_symbols + ["BTC/USDT", "ETH/USDT", "SOL/USDT"]))
        options_candidates = await self._gather_options_signals_for(all_deribit_symbols, data_map)

        # 4. Rank Binance + Deribit
        ranked = self.ranker.rank(spot_signals, options_candidates)

        # 5. Execute best available — ignore confidence threshold during expanded scan
        if ranked:
            best = ranked[0]
            logger.warning(
                "EXPANDED: executing best signal %s score=%.1f conf=%s",
                best.label, best.score, best.confidence
            )
            await self.telegram.send_risk_alert(
                f"🔍 *EXPANDED SCAN — {self._idle_hours():.1f}h No Trade*\n\n"
                f"Best: `{best.label}`\n"
                f"Score: `{best.score:.1f}` | Conf: `{best.confidence}`\n"
                f"✅ Executing on Binance now."
            )
            if best.trade_type == TradeType.SPOT:
                await self._execute_spot(best, balance, data_map)
            else:
                await self._execute_options(best, balance)
            self._last_trade_time = datetime.now(timezone.utc)
        else:
            logger.info("Expanded scan: no signals found on any of %d symbols", len(expanded_symbols))
            await self.telegram.send_risk_alert(
                f"🔍 *EXPANDED SCAN — {self._idle_hours():.1f}h No Trade*\n\n"
                f"Scanned {len(expanded_symbols)} Binance pairs + Deribit options.\n"
                f"❌ No executable signal found — market too quiet, waiting..."
            )

    async def _gather_options_signals_for(self, symbols: list, data_map: dict) -> list:
        """Options signal gathering for an arbitrary symbol list."""
        if not self._options_enabled:
            return []
        candidates = []
        tasks = []
        pending_signals = []
        for symbol in symbols:
            if self.options_mgr.has_open_trade(symbol):
                continue
            df = data_map.get(symbol)
            if df is None:
                continue
            sig = self.options_strategy.evaluate(symbol, df)
            if sig.signal == OptionsSignal.NONE:
                candidates.append((sig, None))
                continue
            spot = float(df["close"].iloc[-1])
            chosen = "straddle" if sig.signal == OptionsSignal.ENTER_STRADDLE else "strangle"
            underlying = symbol.replace("/USDT", "")
            tasks.append(self.options_chain.get_straddle(underlying, spot, chosen))
            pending_signals.append(sig)
        if tasks:
            pairs = await asyncio.gather(*tasks, return_exceptions=True)
            for sig, pair in zip(pending_signals, pairs):
                candidates.append((sig, None if isinstance(pair, Exception) else pair))
        return candidates

    async def _resolve_symbols(self) -> list:
        """Return active symbol list from scanner, or fallback to config symbols."""
        logger = logging.getLogger(__name__)
        if not self._scanner_enabled:
            return self._fallback_symbols

        symbols = await self.scanner.get_symbols()
        if not symbols:
            logger.warning("Scanner returned no symbols — using fallback: %s", self._fallback_symbols)
            return self._fallback_symbols

        # Log only when the list changes
        if symbols != self._active_symbols:
            logger.warning(
                "Active symbols updated → %s  (refreshed: %s)",
                ", ".join(symbols),
                self.scanner.last_refresh_time(),
            )
            await self.telegram.send_risk_alert(
                f"Market scanner updated watchlist:\n" + "\n".join(f"• {s}" for s in symbols)
            )
            self._active_symbols = symbols

        return symbols

    async def _maybe_send_daily_summary(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_daily_summary != today:
            summary = self.risk.daily_summary()
            await self.telegram.send_daily_summary(summary)
            self._last_daily_summary = today

    async def _shutdown(self):
        logger = logging.getLogger(__name__)
        logger.info("Shutting down — closing exchange connections...")
        for ex in (self._exchange, self._deribit):
            try:
                await ex.close()
            except Exception as e:
                logger.error("Error closing exchange: %s", e)
        logger.info("Bot stopped.")


# ── Config loader ───────────────────────────────────────────────────────────────


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    # Safety validation
    if not cfg.get("paper_trading", True):
        if not cfg.get("live_trading_confirmed", False):
            raise ValueError(
                "DANGER: paper_trading=false but live_trading_confirmed is not true.\n"
                "Set live_trading_confirmed=true in config.yaml to confirm live trading."
            )
        print("\n" + "!" * 60)
        print("  WARNING: LIVE TRADING MODE ACTIVE — REAL FUNDS AT RISK")
        print("!" * 60 + "\n")

    return cfg


# ── Entry point ─────────────────────────────────────────────────────────────────


async def _main(config_path: str):
    config = load_config(config_path)
    setup_logging(config)
    bot = TradingBot(config)

    loop = asyncio.get_running_loop()

    def _handle_signal(sig):
        logging.getLogger(__name__).info("Received signal %s", sig)
        bot.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    await bot.run()


def main(config_path: str = "config.yaml"):
    asyncio.run(_main(config_path))
