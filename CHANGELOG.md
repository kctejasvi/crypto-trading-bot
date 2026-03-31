# CHANGELOG.md — Crypto Trading Bot

All notable changes to this project are documented here.

---

## [Unreleased] — 2026-03-31

### Added — Signal Ranker (unified cross-exchange ranking)
- `src/signal_ranker.py` — scores every Binance spot and Deribit options signal
  0–100 using weighted multi-factor models; ranks all opportunities each cycle
- Spot scoring: trend strength, breakout margin, volume surge, RSI quality, ATR size
- Options scoring: squeeze depth, ATR compression, premium value, DTE quality, vol quiet
- Confidence gates: HIGH ≥ 75 executes immediately, MEDIUM ≥ 50 executes, LOW skips
- Parallel signal gathering via `asyncio.gather` — Binance and Deribit evaluated simultaneously
- Main loop unified: manage open trades → gather all signals → rank → execute single best

### Added — Deribit Options Trading
- `src/options_chain.py` — fetches Deribit option chain, parses YYMMDD symbol format,
  filters by DTE window (1–7d), selects ATM straddle or OTM strangle strikes
- `src/options_strategy.py` — volatility squeeze detector using Bollinger Band width
  percentile + ATR compression ratio; fires STRADDLE (tight) or STRANGLE (moderate)
- `src/options_execution.py` — places both call and put legs on Deribit with retry;
  paper mode simulates fills at mid price; tracks `ccxt_symbol` for API calls
- `src/options_manager.py` — manages open straddle/strangle; exits on TP (2× premium),
  SL (0.5× premium), time decay (< 4h DTE), or max hold (48h)
- Fixed Deribit ticker fetch: two-step approach — `fetch_markets` builds the DTE-filtered
  instrument list, then `fetch_tickers(None, {"currency": "BTC", "kind": "option"})` fetches
  all ~1,038 BTC option instruments with live bid/ask/mark prices in one API call
- `OpenOptionsTrade` dataclass includes both `symbol` (short) and `ccxt_symbol` (full)
  to correctly route API order calls vs. log display
- Options position sizing: min contract 0.1 BTC (BTC) / 1 ETH (ETH); hard cap prevents
  spending more than 50% of balance regardless of calculated qty
- Deribit options expire at 08:00 UTC; `_parse_deribit_expiry` sets this explicitly when
  parsing YYMMDD expiry strings so DTE calculations are precise

### Added — Market Scanner
- `src/market_scanner.py` — fetches all USDT tickers from Binance, filters out
  stablecoins/wrapped tokens, ranks by 24h quote volume, selects top N
- Refreshes every hour; logs and Telegrams when watchlist changes
- Configurable: `top_n`, `rank_by` (volume/change), `min_24h_volume_usdt`

### Added — Deribit Exchange Factory
- `src/exchange_factory.py` extended with `create_deribit_exchange()` — connects to
  `test.deribit.com` when `paper_trading: true`, live Deribit when confirmed
- Both exchanges initialized at startup; Deribit closed cleanly on shutdown

### Added — Analytics CLI
- `analytics.py` — standalone CLI for trade performance analysis
- Views: today, specific date, last N days (with daily breakdown), all-time, trades list
- Live auto-refresh dashboard (`--live --interval N`)
- Color-coded output: green PnL, red losses, win-rate bar chart, equity curve

### Changed — Main Loop Architecture
- Cycle now: resolve symbols → fetch OHLCV → manage open trades → risk gate →
  gather signals (parallel) → rank → execute best
- Spot and options signals no longer execute independently — all go through ranker
- `_options_cycle` removed; replaced by `_gather_options_signals` + `_execute_options`
- Added `━━━ Cycle TIMESTAMP ━━━` header each scan for clear log readability

### Changed — config.yaml
- Added `deribit` section (api_key, api_secret)
- Added `scanner` section (top_n, rank_by, refresh_interval_seconds, min_24h_volume_usdt)
- Added `options` section (strategy, min/max_dte, strikes, TP/SL multipliers, exit rules)
- `trading.symbols` demoted to fallback — scanner takes precedence when enabled

---

## [0.2.0] — 2026-03-31 (earlier)

### Added
- `src/market_scanner.py` — dynamic top-5 symbol selection
- `src/options_chain.py` — initial Binance options chain (later replaced by Deribit)
- `src/options_strategy.py` — BB squeeze + ATR compression signal
- `src/options_execution.py` — dual-leg order placement
- `src/options_manager.py` — position tracking with TP/SL/time exits
- `src/telegram_bot.py` — trade entry/exit/SL/TP/daily summary alerts
- `src/exchange_factory.py` — Binance sandbox factory

### Fixed
- Options chain: switched from Binance (limited testnet support) to Deribit
- Symbol format parser: `BTC/USD:BTC-260401-84000-C` → strip `BTC/USD:` prefix
- Expiry parser: `%d%b%y` (4APR25) → `%y%m%d` (260401 = YYMMDD)
- `fetch_tickers` call: list of symbols → `(None, {"currency": "BTC", "kind": "option"})`
- `total_cost` → `total_cost_usdt` field name throughout options modules

---

## [0.1.0] — 2026-03-31 (initial build)

### Added
- `src/data_engine.py` — async OHLCV fetch, rolling 200-candle store, drops live candle
- `src/indicator_engine.py` — EMA20/50, RSI-14, ATR-14, vol avg, 20-candle high/low
- `src/strategy_engine.py` — momentum breakout: EMA cross + RSI + volume + high/low
- `src/risk_engine.py` — 1% capital sizing, ATR-based SL/TP, daily limits, DD guard
- `src/execution_engine.py` — Binance market orders, retry logic, partial fill detection
- `src/trade_manager.py` — open trade tracking, trailing stop (activates 1× ATR gain)
- `src/logger.py` — trade persistence to CSV + SQLite
- `src/main.py` — async event loop, SIGINT/SIGTERM handling, modular cycle
- `config.yaml` — full configuration file with inline documentation
- `run.py` — CLI entry with `--live` safety gate and `"I UNDERSTAND"` confirmation
- `setup.sh` — one-command environment setup (venv + pip install)
- `requirements.txt` — pinned dependencies (ccxt, pandas, numpy, ta, etc.)
