# TASKS.md — Crypto Trading Bot

## Status Key
- [x] Completed
- [-] In progress
- [ ] Pending

---

## Completed

### Core Infrastructure
- [x] Project structure: `src/` package, `config.yaml`, `requirements.txt`, `run.py`, `setup.sh`
- [x] Config loader with safety validation (paper/live flags)
- [x] Colorized logging to stdout + rotating file (`logs/bot.log`)
- [x] Async main loop with SIGINT/SIGTERM graceful shutdown
- [x] Binance exchange factory (sandbox/testnet support)
- [x] Deribit exchange factory (test.deribit.com support)

### Data & Indicators
- [x] OHLCV data engine — rolling 200 candles, drops in-progress candle
- [x] Indicator engine — EMA20/50, RSI-14, ATR-14, volume avg, 20c high/low
- [x] Market scanner — top N USDT pairs by 24h volume, refreshes hourly

### Spot Trading (Binance)
- [x] Momentum breakout strategy (EMA + RSI + volume + breakout)
- [x] Risk engine — 1% capital sizing, SL/TP, daily limits, drawdown guard
- [x] Execution engine — market orders, retry on network error, partial fill detection
- [x] Trade manager — open trade tracking, trailing stop (activates at 1× ATR gain)

### Options Trading (Deribit)
- [x] Options chain fetcher — Deribit testnet, YYMMDD symbol parsing, DTE filter (08:00 UTC expiry)
- [x] Volatility squeeze strategy — BB width percentile + ATR compression
- [x] Straddle builder — ATM same-strike both legs
- [x] Strangle builder — OTM call + OTM put at configurable % from spot
- [x] Options execution engine — both legs, paper simulation, retry logic
- [x] Options position sizing — min contract (0.1 BTC / 1 ETH), 50% balance hard cap
- [x] Options manager — tracks position, checks TP/SL/time-decay/max-hold exits
- [x] Deribit chain fetch: two-step (`fetch_markets` for list + `fetch_tickers` for prices)
- [x] `ccxt_symbol` on `OptionContract` / `OpenOptionsTrade` for correct API routing vs display

### Signal Ranking
- [x] SignalRanker module — scores spot (0–100) and options (0–100)
- [x] Parallel signal gathering (Binance + Deribit in same cycle via asyncio.gather)
- [x] Confidence gates: HIGH ≥ 75 | MEDIUM ≥ 50 | LOW = skip
- [x] Main cycle unified: manage trades → gather all signals → rank → execute best

### Persistence & Alerting
- [x] Trade logger — CSV + SQLite (both spot and options trades)
- [x] Analytics CLI — today/date/range/all-time/live dashboard with color output
- [x] Telegram notifier — entry, exit, SL/TP hit, daily summary, risk alerts

---

## In Progress / Immediate
- [ ] **SECURITY**: Rotate Deribit testnet credentials — `api_key: 1y2gZFIt` was shared in chat history; generate new keys at test.deribit.com
- [ ] Live Binance API key integration (currently testnet only)

---

## Pending / Future

### Robustness
- [ ] Reconnect logic if exchange WebSocket drops mid-session
- [ ] Health check endpoint (HTTP) so external monitor can ping the bot
- [ ] Duplicate-order guard (idempotency key before placing)

### Strategy Enhancements
- [ ] Multi-timeframe confirmation (15m trend + 5m entry)
- [ ] Regime filter — suppress trading during high-correlation macro events
- [ ] Greeks-aware option selection (target delta 0.45–0.55 for straddle legs)
- [ ] IV rank / IV percentile filter — only enter straddle when IV rank < 30

### Risk
- [ ] Per-symbol position limits (max 2 open trades on same coin)
- [ ] Correlation check — avoid entering BTC and ETH options simultaneously
- [ ] Real balance fetch wired to risk engine (currently paper uses $10,000 static)

### Analytics
- [ ] Equity curve plot (matplotlib/ASCII chart)
- [ ] Options-specific analytics (premium paid, IV at entry, Greeks at exit)
- [ ] Export to Google Sheets or Notion

### Operations
- [ ] Docker container + docker-compose for easy deployment
- [ ] Systemd service file for auto-restart on crash
- [ ] Secrets management via `.env` or AWS Secrets Manager (remove API keys from config.yaml)
