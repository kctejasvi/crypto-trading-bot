"""
Trade Logger: Persists trade records to CSV and SQLite.
"""

import csv
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_TRADE_FIELDS = [
    "id", "symbol", "side", "entry_price", "exit_price",
    "quantity", "pnl", "reason", "opened_at", "closed_at",
    "paper", "created_at",
]

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    entry_price REAL    NOT NULL,
    exit_price  REAL,
    quantity    REAL    NOT NULL,
    pnl         REAL,
    reason      TEXT,
    opened_at   TEXT,
    closed_at   TEXT,
    paper       INTEGER DEFAULT 1,
    created_at  TEXT    NOT NULL
)
"""


class TradeLogger:
    """
    Writes trade records to both CSV and SQLite.
    The CSV is a human-readable rolling log; SQLite supports queries.
    """

    def __init__(self, config: dict):
        log_cfg = config["logging"]
        self.csv_path = Path(log_cfg["trade_log_file"])
        self.db_path = Path(log_cfg["db_file"])
        self._ensure_dirs()
        self._init_db()
        self._init_csv()

    # ── Public API ──────────────────────────────────────────────────────

    def log_trade(self, trade_result: dict):
        """Persist a closed trade record."""
        record = self._build_record(trade_result)
        self._write_csv(record)
        self._write_db(record)
        logger.info(
            "TRADE LOG: %s %s pnl=%.4f reason=%s",
            record["symbol"], record["side"],
            record.get("pnl", 0.0), record.get("reason", ""),
        )

    def log_signal(self, symbol: str, signal: str, reason: str):
        """Log a strategy signal (not a trade) at INFO level."""
        logger.info("SIGNAL [%s] %s — %s", symbol, signal, reason)

    def query_daily_pnl(self, date_str: Optional[str] = None) -> float:
        """Return total realized PnL for a given date (YYYY-MM-DD), default today."""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE DATE(closed_at) = ?",
                (date_str,),
            ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()

    def query_trade_count_today(self) -> int:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE DATE(opened_at) = ?",
                (date_str,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    # ── Internals ───────────────────────────────────────────────────────

    def _ensure_dirs(self):
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()
        finally:
            conn.close()

    def _init_csv(self):
        if not self.csv_path.exists():
            with open(self.csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_TRADE_FIELDS)
                writer.writeheader()

    def _build_record(self, trade_result: dict) -> dict:
        return {
            "id": None,
            "symbol": trade_result.get("symbol", ""),
            "side": trade_result.get("side", ""),
            "entry_price": trade_result.get("entry_price", 0.0),
            "exit_price": trade_result.get("exit_price", 0.0),
            "quantity": trade_result.get("quantity", 0.0),
            "pnl": trade_result.get("pnl", 0.0),
            "reason": trade_result.get("reason", ""),
            "opened_at": trade_result.get("opened_at", ""),
            "closed_at": trade_result.get("closed_at", ""),
            "paper": 1 if trade_result.get("paper", True) else 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _write_csv(self, record: dict):
        try:
            with open(self.csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_TRADE_FIELDS)
                writer.writerow(record)
        except Exception as e:
            logger.error("Failed to write trade to CSV: %s", e)

    def _write_db(self, record: dict):
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO trades
                   (symbol, side, entry_price, exit_price, quantity, pnl,
                    reason, opened_at, closed_at, paper, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["symbol"], record["side"],
                    record["entry_price"], record["exit_price"],
                    record["quantity"], record["pnl"],
                    record["reason"], record["opened_at"],
                    record["closed_at"], record["paper"],
                    record["created_at"],
                ),
            )
            conn.commit()
        except Exception as e:
            logger.error("Failed to write trade to SQLite: %s", e)
        finally:
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))
