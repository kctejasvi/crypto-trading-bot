#!/usr/bin/env python3
"""
Entry point for the crypto trading bot.

Usage:
    python run.py                      # paper trading (default)
    python run.py --config config.yaml # custom config path
    python run.py --live               # live trading (requires config confirmation)
"""

import argparse
import sys

def parse_args():
    parser = argparse.ArgumentParser(description="Crypto Trading Bot")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Enable live trading. config.yaml must also have "
            "paper_trading=false and live_trading_confirmed=true"
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.live:
        print(
            "\n" + "=" * 60 + "\n"
            "  LIVE TRADING FLAG DETECTED\n"
            "  Ensure config.yaml has:\n"
            "    paper_trading: false\n"
            "    live_trading_confirmed: true\n"
            "=" * 60 + "\n"
        )
        confirm = input("Type 'I UNDERSTAND' to continue with live trading: ")
        if confirm.strip() != "I UNDERSTAND":
            print("Aborted.")
            sys.exit(0)

    from src.main import main as bot_main
    bot_main(args.config)


if __name__ == "__main__":
    main()
