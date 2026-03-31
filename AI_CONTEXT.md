# AI_CONTEXT.md — Crypto Trading Bot

## Project Overview
A production-grade automated crypto trading bot that trades on **Binance** (spot)
and **Deribit** (options) simultaneously, selecting the highest-probability trade
each cycle using a unified signal ranker.

---

## Architecture

```
run.py                        Entry point — paper/live safety gate
src/
  main.py                     Async main loop — orchestrates all modules
  exchange_factory.py         Creates Binance (spot) + Deribit (options) exchanges
  market_scanner.py           Fetches top N USDT pairs by 24h volume from Binance
  data_engine.py              Fetches + caches OHLCV candles (rolling 200 candles)
  indicator_engine.py         EMA20/50, RSI-14, ATR-14, volume avg, breakout levels
  strategy_engine.py          Momentum breakout: EMA+RSI+volume+high/low breakout
  options_chain.py            Deribit options chain fetcher, strike/expiry selector
  options_strategy.py         Volatility squeeze detector: BB width + ATR compression
  signal_ranker.py            Scores all signals 0-100, picks highest probability
  risk_engine.py              Position sizing, SL/TP, daily limits, drawdown guard
  execution_engine.py         Binance spot order placement with retry logic
  options_execution.py        Deribit options order placement (both legs)
  trade_manager.py            Tracks open spot trades, trailing stop logic
  options_manager.py          Tracks open straddle/strangle, manages exits
  logger.py                   CSV + SQLite trade persistence
  telegram_bot.py             Telegram alerts for entries, exits, daily summary
analytics.py                  CLI analytics dashboard (today/range/all-time/live)
config.yaml                   All configuration — API keys, risk params, symbols
```

---

## Exchange Roles

| Exchange | Purpose | Mode |
|---|---|---|
| Binance | Spot momentum trades (BTC/USDT, ETH/USDT, top 5 scanner) | Testnet/sandbox |
| Deribit | Options straddle/strangle trades (BTC, ETH) | Testnet (test.deribit.com) |

---

## Trading Strategies

### 1. Spot — Momentum Breakout (Binance)
**Entry (BUY):**
- EMA20 > EMA50 (uptrend)
- Close > 20-candle high (breakout)
- Volume > 1.5× average (confirmation)
- RSI between 55–70 (momentum, not overbought)

**Exit:** SL = 1× ATR below entry | TP = 2× ATR above entry | Trailing stop activates at 1× ATR gain

### 2. Options — Volatility Squeeze (Deribit)
**Entry:** Straddle or Strangle when:
- Bollinger Band width in bottom 20th percentile (tight squeeze)
- ATR < 85% of its 50-period average (calm/compressed)
- Price hasn't already moved > 1.5% in last 5 candles

**Strategy selection:**
- BB width ≤ 20th pct → **Straddle** (ATM, same strike)
- BB width 20–35th pct → **Strangle** (OTM, different strikes, cheaper)

**Exit:** TP at 2× premium | SL at 0.5× premium | Force close < 4h DTE | Max hold 48h

**Deribit chain fetch (two-step):**
1. `fetch_markets({"currency": underlying, "kind": "option"})` — builds instrument list, applies DTE filter
2. `fetch_tickers(None, {"currency": underlying, "kind": "option"})` — fetches all ~1,038 live prices in one call

Options expire at **08:00 UTC** on expiry date; DTE is computed against that timestamp.
Min contract size: **0.1 BTC** (BTC options) / **1 ETH** (ETH options).
Hard cap: options position cost never exceeds **50% of balance** (absolute safety ceiling).

---

## Signal Ranker (signal_ranker.py)

Every 60-second cycle, all signals from both exchanges are scored and ranked:

**Spot score (0–100):**
- Trend strength (EMA separation %) — up to 25 pts
- Breakout margin (how far above 20c high) — up to 20 pts
- Volume surge (vol ratio above 1.5×) — up to 20 pts
- RSI quality (closeness to optimal 60) — up to 20 pts
- ATR size (volatility room) — up to 15 pts

**Options score (0–100):**
- Squeeze depth (BB width percentile) — up to 30 pts
- ATR compression — up to 25 pts
- Premium value (IV implied move vs move needed) — up to 20 pts
- DTE quality (2–5d is ideal) — up to 15 pts
- Volume quiet (pre-breakout accumulation) — up to 10 pts

**Confidence gates:** HIGH ≥ 75 | MEDIUM ≥ 50 | LOW < 50 (skipped)

---

## Risk Management

| Parameter | Value |
|---|---|
| Capital risk per spot trade | 1% of balance |
| SL distance | 1× ATR |
| TP distance | 2× ATR |
| Trailing stop activation | 1× ATR profit |
| Max trades per day | 3 |
| Max consecutive losses | 2 (then pause) |
| Daily drawdown limit | 3% |
| Max options premium per trade | 2% of balance |
| Options hard cap | 50% of balance (absolute ceiling) |
| Min Deribit contract size | 0.1 BTC (BTC options) / 1 ETH (ETH options) |
| Options TP | 2× premium paid |
| Options SL | 0.5× premium paid |
| Options force exit | < 4h DTE remaining |
| Options max hold | 48h |

---

## Key Config (config.yaml)

```yaml
paper_trading: true               # SAFE default — no real money
live_trading_confirmed: false     # must flip BOTH for live

scanner:
  top_n: 5                        # trade top 5 coins by volume
  rank_by: volume                 # or "change" for biggest movers
  refresh_interval_seconds: 3600  # rescan every hour

options:
  strategy: strangle              # straddle or strangle
  min_dte: 1 / max_dte: 7         # expiry window
  tp_multiplier: 2.0              # 100% gain target
  sl_multiplier: 0.5              # 50% loss stop
```

---

## Data Flow (each 60s cycle)

```
1. Market scanner   → top N symbols from Binance 24h volume
2. OHLCV fetch      → Binance (spot symbols) + reuse for options
3. Manage trades    → update SL/TP/trailing on all open positions
4. Risk gate check  → daily limits, drawdown, consecutive losses
5. Gather signals   → Binance momentum + Deribit squeeze IN PARALLEL
6. Rank signals     → score all 0-100, sort descending
7. Execute best     → highest score above MEDIUM threshold wins
8. Log + alert      → SQLite + CSV + Telegram
```

---

## Operational Commands

```bash
# Start (paper mode)
source venv/bin/activate && python3 run.py

# Background
nohup python3 run.py > logs/bot.log 2>&1 & echo $! > bot.pid

# Live logs
tail -f logs/bot.log

# Analytics
python3 analytics.py               # today
python3 analytics.py --range 7     # last 7 days
python3 analytics.py --all         # all-time
python3 analytics.py --live        # auto-refresh dashboard

# Stop
kill $(cat bot.pid)
```

---

## Safety Flags
- Default: `paper_trading: true` — simulates all orders, no real money
- Live requires: `paper_trading: false` + `live_trading_confirmed: true` + `--live` CLI flag + typing `"I UNDERSTAND"`
- Binance uses testnet sandbox; Deribit uses `test.deribit.com`
