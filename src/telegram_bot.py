"""
Telegram Integration: Sends trade alerts, daily summaries, and handles /report command.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning("python-telegram-bot not installed — Telegram alerts disabled")


class TelegramNotifier:
    """
    Sends async notifications via Telegram Bot API.
    Gracefully disabled if the library is missing or token/chat_id are not set.
    """

    def __init__(self, config: dict):
        tg_cfg = config.get("telegram", {})
        self.enabled = tg_cfg.get("enabled", False) and TELEGRAM_AVAILABLE
        self.bot_token: str = tg_cfg.get("bot_token", "")
        self.chat_id: str = str(tg_cfg.get("chat_id", ""))
        self._bot: Optional["Bot"] = None

        self._db_file: str = config.get("logging", {}).get("db_file", "logs/trades.db")
        self._last_update_id: int = 0
        self._expanded_scan_cb = None      # registered by TradingBot
        self._aggressive_strategy = None   # registered by TradingBot
        self._aggressive_execute_cb = None # registered by TradingBot

        if self.enabled:
            if not self.bot_token or self.bot_token == "YOUR_TELEGRAM_BOT_TOKEN":
                logger.warning("Telegram enabled but bot_token not set — disabling")
                self.enabled = False
            elif not self.chat_id or self.chat_id == "YOUR_TELEGRAM_CHAT_ID":
                logger.warning("Telegram enabled but chat_id not set — disabling")
                self.enabled = False
            else:
                self._bot = Bot(token=self.bot_token)
                logger.info("Telegram notifier initialized (chat_id=%s)", self.chat_id)

    def register_expanded_scan(self, callback):
        """Register the TradingBot._expanded_scan coroutine so /expand can call it."""
        self._expanded_scan_cb = callback

    def register_aggressive_strategy(self, strategy, execute_cb=None):
        """Register AggressiveStrategy instance so /aggressive can toggle it."""
        self._aggressive_strategy = strategy
        self._aggressive_execute_cb = execute_cb

    # ── Public API ──────────────────────────────────────────────────────

    async def send_trade_entry(self, trade: dict):
        mode = "PAPER" if trade.get("paper") else "LIVE"
        msg = (
            f"🟢 *TRADE ENTRY [{mode}]*\n"
            f"Symbol: `{trade['symbol']}`\n"
            f"Side: `{trade['side'].upper()}`\n"
            f"Entry: `{trade['entry_price']:.4f}`\n"
            f"SL: `{trade['stop_loss']:.4f}`\n"
            f"TP: `{trade['take_profit']:.4f}`\n"
            f"Qty: `{trade['quantity']:.6f}`\n"
            f"Risk: `${trade['risk_amount']:.2f}`"
        )
        await self._send(msg)

    async def send_trade_exit(self, result: dict):
        pnl = result.get("pnl", 0.0)
        emoji = "✅" if pnl >= 0 else "🔴"
        mode = "PAPER" if result.get("paper") else "LIVE"
        msg = (
            f"{emoji} *TRADE EXIT [{mode}]*\n"
            f"Symbol: `{result['symbol']}`\n"
            f"Reason: `{result.get('reason', 'MANUAL')}`\n"
            f"Entry: `{result['entry_price']:.4f}`\n"
            f"Exit: `{result['exit_price']:.4f}`\n"
            f"PnL: `{pnl:+.4f} USDT`"
        )
        await self._send(msg)

    async def send_sl_hit(self, symbol: str, sl_price: float, pnl: float):
        msg = (
            f"🛑 *STOP LOSS HIT*\n"
            f"Symbol: `{symbol}`\n"
            f"SL Price: `{sl_price:.4f}`\n"
            f"PnL: `{pnl:+.4f} USDT`"
        )
        await self._send(msg)

    async def send_tp_hit(self, symbol: str, tp_price: float, pnl: float):
        msg = (
            f"🎯 *TAKE PROFIT HIT*\n"
            f"Symbol: `{symbol}`\n"
            f"TP Price: `{tp_price:.4f}`\n"
            f"PnL: `{pnl:+.4f} USDT`"
        )
        await self._send(msg)

    async def send_daily_summary(self, summary: dict):
        dd = summary.get("drawdown_pct", 0.0)
        pnl = summary.get("realized_pnl", 0.0)
        emoji = "📈" if pnl >= 0 else "📉"
        msg = (
            f"{emoji} *DAILY SUMMARY*\n"
            f"Date: `{summary['date']}`\n"
            f"Trades: `{summary['trades_taken']}`\n"
            f"PnL: `{pnl:+.4f} USDT`\n"
            f"Drawdown: `{dd:.2f}%`\n"
            f"Consecutive Losses: `{summary['consecutive_losses']}`"
        )
        await self._send(msg)

    async def send_risk_alert(self, reason: str):
        msg = f"⚠️ *RISK ALERT*\n{reason}"
        await self._send(msg)

    # ── Command Listener ────────────────────────────────────────────────

    async def start_command_listener(self):
        """Poll for incoming Telegram messages and handle commands."""
        if not self.enabled or self._bot is None:
            return
        logger.info("Telegram command listener started")
        while True:
            try:
                updates = await self._bot.get_updates(
                    offset=self._last_update_id + 1,
                    timeout=10,
                    allowed_updates=["message"],
                )
                for update in updates:
                    self._last_update_id = update.update_id
                    msg = update.message
                    if not msg or not msg.text:
                        continue
                    if str(msg.chat.id) != self.chat_id:
                        continue
                    text = msg.text.strip().lower()
                    if text in ("/report", "report"):
                        await self._send_report()
                    elif text in ("/status", "status"):
                        await self._send_status()
                    elif text in ("/expand", "expand"):
                        await self._run_expanded_scan()
                    elif text.startswith("/aggressive") or text.startswith("aggressive"):
                        await self._handle_aggressive(text)
                    elif text in ("/help", "help"):
                        await self._send(
                            "📋 *Available Commands*\n\n"
                            "/report — Full performance report\n"
                            "/status — Bot status & uptime\n"
                            "/expand — Run expanded scan now (top 20 coins)\n"
                            "/aggressive on — Enable high-risk strategy\n"
                            "/aggressive off — Disable high-risk strategy\n"
                            "/aggressive — Show current mode\n"
                            "/help — Show this menu"
                        )
            except Exception as e:
                logger.debug("Command listener error: %s", e)
            await asyncio.sleep(2)

    async def _handle_aggressive(self, text: str):
        if self._aggressive_strategy is None:
            await self._send("⚠️ Aggressive strategy not available.")
            return

        ag = self._aggressive_strategy
        parts = text.strip().split()
        arg = parts[1].lower() if len(parts) > 1 else ""

        if arg == "on":
            ag.enable()
            await self._send(
                "⚡ *AGGRESSIVE MODE — ON*\n\n"
                "*Strategy 1 — Mean Reversion:*\n"
                "  BUY when RSI < 28 (oversold bounce)\n"
                "  SELL when RSI > 72 (overbought fade)\n\n"
                "*Strategy 2 — Aggressive Trend:*\n"
                "  BUY: EMA up + RSI 45–75 + vol > 1.0x\n"
                "  SELL: EMA down + RSI 15–40 + vol > 1.0x\n\n"
                "*Risk:* 2% capital | SL 0.5x ATR | TP 1.5x ATR\n\n"
                "🔍 Scanning market now..."
            )
            if self._aggressive_execute_cb:
                try:
                    await self._aggressive_execute_cb()
                except Exception as e:
                    logger.error("Aggressive execute error: %s", e)
                    await self._send(f"⚠️ Execution error: `{e}`")
        elif arg == "off":
            ag.disable()
            await self._send(
                "✅ *AGGRESSIVE MODE — OFF*\n\n"
                "Back to normal strategy:\n"
                "  EMA20 > EMA50 + RSI 55–70 + vol > 1.5x\n"
                "  Capital risk: 1% | SL: 1x ATR | TP: 2x ATR"
            )
        else:
            status = "ON ⚡" if ag.enabled else "OFF ✅"
            await self._send(
                f"*Aggressive Mode: {status}*\n\n"
                f"Commands:\n"
                f"`/aggressive on` — enable high-risk strategy\n"
                f"`/aggressive off` — back to normal"
            )

    async def _run_expanded_scan(self):
        if self._expanded_scan_cb is None:
            await self._send("⚠️ Expanded scan not available yet — bot still initialising.")
            return
        await self._send("🔍 *Expanded scan started...*\nScanning top 20 coins on Binance + Deribit. Will execute best signal found.")
        try:
            await self._expanded_scan_cb()
        except Exception as e:
            logger.error("Expanded scan error from Telegram command: %s", e)
            await self._send(f"⚠️ Expanded scan error: `{e}`")

    async def _send_report(self):
        """Generate and send performance report from SQLite DB."""
        try:
            db_path = Path(self._db_file)
            if not db_path.exists():
                await self._send("📊 No trades recorded yet.")
                return

            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM trades")
            total = cur.fetchone()[0]

            if total == 0:
                conn.close()
                await self._send("📊 No trades recorded yet — bot is scanning for signals.")
                return

            cur.execute("SELECT COUNT(*) FROM trades WHERE pnl > 0")
            wins = cur.fetchone()[0]
            losses = total - wins

            cur.execute("SELECT COALESCE(SUM(pnl),0) FROM trades")
            total_pnl = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(AVG(pnl),0) FROM trades WHERE pnl > 0")
            avg_win = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(AVG(pnl),0) FROM trades WHERE pnl <= 0")
            avg_loss = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(MAX(pnl),0) FROM trades")
            best = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(MIN(pnl),0) FROM trades")
            worst = cur.fetchone()[0]

            # Per symbol
            cur.execute("""
                SELECT symbol,
                       COUNT(*) as trades,
                       SUM(pnl) as pnl,
                       ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr
                FROM trades GROUP BY symbol ORDER BY pnl DESC
            """)
            symbols = cur.fetchall()
            conn.close()

            win_rate = (wins / total * 100) if total > 0 else 0
            gross_win = avg_win * wins if wins else 0
            gross_loss = abs(avg_loss * losses) if losses else 1
            pf = round(gross_win / gross_loss, 2) if gross_loss else 0

            pnl_emoji = "📈" if total_pnl >= 0 else "📉"
            now_ist = datetime.now(timezone.utc).strftime("%d %b %Y %I:%M %p") + " IST"

            sym_lines = "\n".join(
                f"  `{s[0]}` — {s[1]} trades | PnL: `{s[2]:+.2f}` | WR: `{s[3]}%`"
                for s in symbols
            )

            msg = (
                f"{pnl_emoji} *BOT PERFORMANCE REPORT*\n"
                f"_{now_ist}_\n\n"
                f"*Overall*\n"
                f"Trades: `{total}` (W:`{wins}` / L:`{losses}`)\n"
                f"Win Rate: `{win_rate:.1f}%`\n"
                f"Total PnL: `{total_pnl:+.2f} USDT`\n"
                f"Avg Win: `{avg_win:+.2f}` | Avg Loss: `{avg_loss:+.2f}`\n"
                f"Best: `{best:+.2f}` | Worst: `{worst:+.2f}`\n"
                f"Profit Factor: `{pf}`\n\n"
                f"*Per Symbol*\n{sym_lines}"
            )
            await self._send(msg)
        except Exception as e:
            logger.error("Report generation error: %s", e)
            await self._send("⚠️ Error generating report.")

    async def _send_status(self):
        now_ist = datetime.now(timezone.utc).strftime("%d %b %Y %I:%M %p") + " IST"
        await self._send(
            f"✅ *BOT STATUS*\n"
            f"_As of {now_ist}_\n\n"
            f"Status: `Running`\n"
            f"Mode: `Paper Trading`\n"
            f"Exchange: `Binance Testnet`\n"
            f"Options: `Deribit Testnet`\n"
            f"Scan Interval: `60 seconds`"
        )

    # ── Internals ───────────────────────────────────────────────────────

    async def _send(self, text: str):
        if not self.enabled or self._bot is None:
            return
        try:
            await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
