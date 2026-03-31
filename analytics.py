#!/usr/bin/env python3
"""
Analytics CLI for the crypto trading bot.

Usage:
    python analytics.py                  # today's summary
    python analytics.py --date 2026-03-31
    python analytics.py --range 7        # last 7 days
    python analytics.py --all            # all-time stats
    python analytics.py --live           # live dashboard, refreshes every 10s
    python analytics.py --trades         # list all closed trades
    python analytics.py --open           # show currently open positions (paper)
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────────
DB_PATH = Path("logs/trades.db")
CSV_PATH = Path("logs/trades.csv")


# ── DB helpers ──────────────────────────────────────────────────────────────────

def get_conn():
    if not DB_PATH.exists():
        print(f"No database found at {DB_PATH}. Has the bot run yet?")
        sys.exit(1)
    return sqlite3.connect(str(DB_PATH))


def query(sql: str, params: tuple = ()) -> list:
    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ── Formatting helpers ───────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
BLUE   = "\033[94m"


def color(text, c):
    return f"{c}{text}{RESET}"


def pnl_color(val: float) -> str:
    if val > 0:
        return color(f"+{val:.4f}", GREEN)
    elif val < 0:
        return color(f"{val:.4f}", RED)
    return f"{val:.4f}"


def pct_color(val: float) -> str:
    if val > 0:
        return color(f"+{val:.2f}%", GREEN)
    elif val < 0:
        return color(f"{val:.2f}%", RED)
    return f"{val:.2f}%"


def bar(wins: int, total: int, width: int = 20) -> str:
    if total == 0:
        return "[" + "-" * width + "]"
    filled = round(wins / total * width)
    return "[" + color("█" * filled, GREEN) + color("░" * (width - filled), DIM) + "]"


def hr(width: int = 60):
    print(color("─" * width, DIM))


def header(title: str):
    print()
    print(color("═" * 60, CYAN))
    print(color(f"  {title}", BOLD + CYAN))
    print(color("═" * 60, CYAN))


# ── Analytics builders ───────────────────────────────────────────────────────────

def stats_for_rows(rows) -> dict:
    """Compute aggregate stats from a list of trade rows."""
    if not rows:
        return {}

    pnls = [r["pnl"] for r in rows if r["pnl"] is not None]
    wins  = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total  = len(pnls)

    win_rate = len(wins) / total * 100 if total else 0
    avg_win  = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    max_win  = max(wins) if wins else 0
    max_loss = min(losses) if losses else 0

    # Max consecutive losses
    max_consec_loss = 0
    cur = 0
    for p in pnls:
        if p < 0:
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0

    symbols = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in symbols:
            symbols[sym] = {"trades": 0, "pnl": 0.0, "wins": 0}
        symbols[sym]["trades"] += 1
        symbols[sym]["pnl"] += r["pnl"] or 0
        if (r["pnl"] or 0) > 0:
            symbols[sym]["wins"] += 1

    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_pnl": sum(pnls),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_win": max_win,
        "max_loss": max_loss,
        "profit_factor": profit_factor,
        "max_consec_loss": max_consec_loss,
        "symbols": symbols,
    }


def print_stats(s: dict, title: str = ""):
    if not s:
        print(color("  No trades found.", DIM))
        return

    if title:
        header(title)

    total_pnl = s["total_pnl"]
    pf = s["profit_factor"]
    pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"

    print(f"  {'Trades':20s} {color(str(s['total']), BOLD)}")
    print(f"  {'Win / Loss':20s} {color(str(s['wins']), GREEN)} / {color(str(s['losses']), RED)}")
    print(f"  {'Win Rate':20s} {bar(s['wins'], s['total'])}  {pct_color(s['win_rate'])}")
    hr()
    print(f"  {'Total PnL':20s} {pnl_color(total_pnl)} USDT")
    avg_win_str  = f"+{s['avg_win']:.4f}"
    avg_loss_str = f"{s['avg_loss']:.4f}"
    max_win_str  = f"+{s['max_win']:.4f}"
    max_loss_str = f"{s['max_loss']:.4f}"
    print(f"  {'Avg Win':20s} {color(avg_win_str, GREEN)} USDT")
    print(f"  {'Avg Loss':20s} {color(avg_loss_str, RED)} USDT")
    print(f"  {'Best Trade':20s} {color(max_win_str, GREEN)} USDT")
    print(f"  {'Worst Trade':20s} {color(max_loss_str, RED)} USDT")
    hr()
    print(f"  {'Profit Factor':20s} {color(pf_str, YELLOW)}")
    print(f"  {'Max Consec. Losses':20s} {color(str(s['max_consec_loss']), RED)}")

    if s.get("symbols"):
        print()
        print(color("  Per Symbol:", BOLD))
        for sym, d in s["symbols"].items():
            wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            print(
                f"    {sym:12s}  trades={d['trades']:3d}  "
                f"pnl={pnl_color(d['pnl'])} USDT  "
                f"win={wr:.0f}%"
            )
    print()


def print_trade_list(rows, title="Trades"):
    header(title)
    if not rows:
        print(color("  No trades found.", DIM))
        print()
        return

    fmt = "  {:<5} {:<12} {:<6} {:>12} {:>12} {:>10} {:<14} {}"
    print(color(fmt.format("ID", "Symbol", "Side", "Entry", "Exit", "PnL", "Reason", "Closed At"), BOLD))
    hr()
    for r in rows:
        pnl = r["pnl"] or 0.0
        pnl_str = pnl_color(pnl)
        mode = color("[P]", DIM) if r["paper"] else color("[L]", YELLOW)
        print(fmt.format(
            r["id"] or "-",
            r["symbol"],
            r["side"].upper(),
            f"{r['entry_price']:.4f}",
            f"{r['exit_price']:.4f}" if r["exit_price"] else "-",
            pnl_str,
            r["reason"] or "-",
            (r["closed_at"] or "")[:19],
        ) + f" {mode}")
    print()


# ── Views ────────────────────────────────────────────────────────────────────────

def view_today():
    today = date.today().isoformat()
    rows = query(
        "SELECT * FROM trades WHERE DATE(closed_at) = ? ORDER BY closed_at",
        (today,),
    )
    s = stats_for_rows(rows)
    print_stats(s, f"Today's Performance  ({today})")
    print_trade_list(rows, "Today's Trades")


def view_date(date_str: str):
    rows = query(
        "SELECT * FROM trades WHERE DATE(closed_at) = ? ORDER BY closed_at",
        (date_str,),
    )
    s = stats_for_rows(rows)
    print_stats(s, f"Performance  ({date_str})")
    print_trade_list(rows, f"Trades on {date_str}")


def view_range(days: int):
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = query(
        "SELECT * FROM trades WHERE DATE(closed_at) >= ? ORDER BY closed_at",
        (since,),
    )
    s = stats_for_rows(rows)
    print_stats(s, f"Last {days} Days  (since {since})")

    # Daily breakdown
    header("Daily Breakdown")
    daily: dict = {}
    for r in rows:
        d = (r["closed_at"] or "")[:10]
        if d not in daily:
            daily[d] = []
        daily[d].append(r)

    fmt = "  {:<12} {:>7} {:>7} {:>14}  {}"
    print(color(fmt.format("Date", "Trades", "Win%", "PnL (USDT)", "Bar"), BOLD))
    hr()
    for d in sorted(daily.keys()):
        dr = daily[d]
        pnls = [r["pnl"] or 0 for r in dr]
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = sum(pnls)
        wr = wins / len(dr) * 100 if dr else 0
        print(fmt.format(
            d,
            len(dr),
            f"{wr:.0f}%",
            pnl_color(total_pnl),
            bar(wins, len(dr), 15),
        ))
    print()


def view_all():
    rows = query("SELECT * FROM trades ORDER BY closed_at")
    s = stats_for_rows(rows)
    print_stats(s, "All-Time Performance")

    # Equity curve (cumulative PnL)
    header("Equity Curve (Cumulative PnL)")
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rows:
        cumulative += r["pnl"] or 0
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    print(f"  {'Final Cumulative PnL':25s} {pnl_color(cumulative)} USDT")
    print(f"  {'Peak PnL':25s} {color(f'+{peak:.4f}', GREEN)} USDT")
    print(f"  {'Max Drawdown':25s} {color(f'-{max_dd:.4f}', RED)} USDT")
    print()


def view_trades():
    rows = query("SELECT * FROM trades ORDER BY closed_at DESC LIMIT 50")
    print_trade_list(rows, "Last 50 Closed Trades")


def view_live(interval: int = 10):
    """Auto-refresh dashboard."""
    try:
        while True:
            os.system("clear")
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            print(color(f"  Live Dashboard — refreshes every {interval}s  [{now}]", BOLD + BLUE))
            print(color("  Press Ctrl+C to exit", DIM))

            view_today()

            # Last 7 days summary row
            since = (date.today() - timedelta(days=6)).isoformat()
            rows_7 = query(
                "SELECT * FROM trades WHERE DATE(closed_at) >= ?", (since,)
            )
            s7 = stats_for_rows(rows_7)
            if s7:
                header("7-Day Summary")
                print(f"  Trades: {s7['total']}  "
                      f"Win Rate: {pct_color(s7['win_rate'])}  "
                      f"PnL: {pnl_color(s7['total_pnl'])} USDT  "
                      "PF: " + color(("∞" if s7["profit_factor"] == float("inf") else f"{s7['profit_factor']:.2f}"), YELLOW))
                print()

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nDashboard closed.")


# ── CLI ──────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trading Bot Analytics")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date",   metavar="YYYY-MM-DD", help="Stats for a specific date")
    group.add_argument("--range",  type=int, metavar="N", help="Stats for last N days")
    group.add_argument("--all",    action="store_true",   help="All-time stats")
    group.add_argument("--trades", action="store_true",   help="List last 50 closed trades")
    group.add_argument("--live",   action="store_true",   help="Live auto-refresh dashboard")
    parser.add_argument("--interval", type=int, default=10, help="Live refresh interval (seconds)")

    args = parser.parse_args()

    if args.date:
        view_date(args.date)
    elif args.range:
        view_range(args.range)
    elif args.all:
        view_all()
    elif args.trades:
        view_trades()
    elif args.live:
        view_live(args.interval)
    else:
        view_today()


if __name__ == "__main__":
    main()
