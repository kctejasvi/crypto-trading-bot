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
        self._live_status_cb = None        # registered by TradingBot — returns live state dict

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

    def register_live_status(self, cb):
        """Register callback that returns live bot state dict."""
        self._live_status_cb = cb

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
        """Generate live performance report from SQLite DB + current bot state."""
        try:
            now_ist = datetime.now(timezone.utc)
            now_str = (now_ist).strftime("%d %b %Y") + f" {(now_ist.hour + 5) % 12 or 12}:{(now_ist.minute + 30) % 60:02d} {'AM' if (now_ist.hour + 5) % 24 < 12 else 'PM'} IST"

            # ── Live state from bot ──────────────────────────────────────
            live = self._live_status_cb() if self._live_status_cb else {}
            aggr_on   = live.get("aggressive_on", False)
            last_cycle = live.get("last_cycle", "—")
            idle_hrs  = live.get("idle_hours", 0.0)
            open_trades = live.get("open_trades", [])
            live_indicators = live.get("indicators", {})  # {symbol: {rsi, ema_fast, ema_slow, close}}

            # ── DB stats ─────────────────────────────────────────────────
            db_path = Path(self._db_file)
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()

            # All-time
            cur.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0), COALESCE(MAX(pnl),0), COALESCE(MIN(pnl),0) FROM trades")
            total, total_pnl, best, worst = cur.fetchone()
            wins = 0
            avg_win = avg_loss = 0.0
            pf = 0.0
            if total > 0:
                cur.execute("SELECT COUNT(*) FROM trades WHERE pnl > 0")
                wins = cur.fetchone()[0]
                losses = total - wins
                cur.execute("SELECT COALESCE(AVG(pnl),0) FROM trades WHERE pnl > 0")
                avg_win = cur.fetchone()[0]
                cur.execute("SELECT COALESCE(AVG(pnl),0) FROM trades WHERE pnl <= 0")
                avg_loss = cur.fetchone()[0]
                gross_win  = avg_win * wins if wins else 0
                gross_loss = abs(avg_loss * losses) if losses else 1
                pf = round(gross_win / gross_loss, 2) if gross_loss else 0

            # Today
            cur.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM trades WHERE DATE(created_at)=?", (today_str,))
            today_trades, today_pnl = cur.fetchone()

            # Per symbol
            cur.execute("""SELECT symbol, COUNT(*), SUM(pnl),
                           ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1)
                           FROM trades GROUP BY symbol ORDER BY SUM(pnl) DESC""")
            sym_rows = cur.fetchall()

            # Last 3 trades
            cur.execute("SELECT symbol, side, entry_price, exit_price, pnl, reason, closed_at FROM trades ORDER BY id DESC LIMIT 3")
            last_trades = cur.fetchall()
            conn.close()

            win_rate = (wins / total * 100) if total > 0 else 0
            pnl_emoji = "📈" if total_pnl >= 0 else "📉"

            # ── Build message ─────────────────────────────────────────────
            lines = [
                f"{pnl_emoji} *LIVE BOT REPORT*",
                f"_{now_str}_",
                "",
                f"*🤖 Bot Status*",
                f"  Strategy: `{'⚡ AGGRESSIVE' if aggr_on else '✅ NORMAL'}`",
                f"  Last cycle: `{last_cycle}`",
                f"  Idle: `{idle_hrs:.1f}h` since last trade",
                f"  Open trades: `{len(open_trades)}`",
            ]

            # Live market snapshot
            if live_indicators:
                lines.append("")
                lines.append("*📡 Market Snapshot*")
                for sym, ind in live_indicators.items():
                    rsi = ind.get("rsi", 0)
                    close = ind.get("close", 0)
                    ema_fast = ind.get("ema_fast", 0)
                    ema_slow = ind.get("ema_slow", 0)
                    trend = "↑" if ema_fast > ema_slow else "↓"
                    rsi_tag = "🔴 OVS" if rsi < 30 else ("🟢 OVB" if rsi > 70 else "🟡 NEU")
                    lines.append(f"  `{sym}` ${close:,.2f} | RSI `{rsi:.1f}` {rsi_tag} | Trend `{trend}`")

            # Today
            lines += [
                "",
                f"*📅 Today*",
                f"  Trades: `{today_trades}` | PnL: `{today_pnl:+.2f} USDT`",
            ]

            # All-time
            lines += [
                "",
                f"*📊 All-Time*",
                f"  Trades: `{total}` (W:`{wins}` / L:`{total - wins}`)",
                f"  Win Rate: `{win_rate:.1f}%` | Profit Factor: `{pf}`",
                f"  Total PnL: `{total_pnl:+.2f} USDT`",
                f"  Avg Win: `{avg_win:+.2f}` | Avg Loss: `{avg_loss:+.2f}`",
                f"  Best: `{best:+.2f}` | Worst: `{worst:+.2f}`",
            ]

            # Per symbol
            if sym_rows:
                lines.append("")
                lines.append("*💹 Per Symbol*")
                for s in sym_rows:
                    lines.append(f"  `{s[0]}` {s[1]}T | `{s[2]:+.2f}` USDT | WR `{s[3]}%`")

            # Last trades
            if last_trades:
                lines.append("")
                lines.append("*🕐 Last 3 Trades*")
                for t in last_trades:
                    sym, side, ep, xp, pnl, reason, closed = t
                    e = "✅" if pnl > 0 else "🔴"
                    lines.append(f"  {e} `{sym}` {side.upper()} `{pnl:+.2f}` — {reason}")

            await self._send("\n".join(lines))

        except Exception as e:
            logger.error("Report generation error: %s", e)
            await self._send("⚠️ Error generating report.")

    async def _send_status(self):
        live = self._live_status_cb() if self._live_status_cb else {}
        aggr_on    = live.get("aggressive_on", False)
        last_cycle = live.get("last_cycle", "—")
        idle_hrs   = live.get("idle_hours", 0.0)
        open_trades = live.get("open_trades", [])
        now_ist = datetime.now(timezone.utc)
        now_str = f"{(now_ist.hour + 5) % 12 or 12}:{(now_ist.minute + 30) % 60:02d} {'AM' if (now_ist.hour+5)%24 < 12 else 'PM'} IST"
        await self._send(
            f"✅ *BOT STATUS* — _{now_str}_\n\n"
            f"Strategy: `{'⚡ AGGRESSIVE' if aggr_on else '✅ NORMAL'}`\n"
            f"Last cycle: `{last_cycle}`\n"
            f"Idle: `{idle_hrs:.1f}h` since last trade\n"
            f"Open trades: `{len(open_trades)}`\n"
            f"Mode: `Paper Trading`\n"
            f"Exchange: `Binance Testnet + Deribit Testnet`\n"
            f"Scan: every `60s`"
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
