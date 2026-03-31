"""Run expanded scan once and print results."""
import asyncio, yaml, sys
sys.path.insert(0, ".")

async def main():
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    from src.exchange_factory import create_exchange, create_deribit_exchange
    from src.market_scanner import MarketScanner
    from src.data_engine import DataEngine
    from src.indicator_engine import IndicatorEngine
    from src.strategy_engine import StrategyEngine, Signal
    from src.options_strategy import OptionsStrategyEngine, OptionsSignal
    from src.options_chain import OptionsChain
    from src.signal_ranker import SignalRanker
    from src.delta_scanner import DeltaScanner

    exchange = create_exchange(config)
    deribit  = create_deribit_exchange(config)
    scanner  = MarketScanner(exchange, config)
    data     = DataEngine(exchange, config)
    ind_eng  = IndicatorEngine(config)
    strat    = StrategyEngine(config)
    opts_strat = OptionsStrategyEngine(config)
    opts_chain = OptionsChain(deribit, config)
    ranker   = SignalRanker()
    delta    = DeltaScanner(config)

    print("\n🔍 EXPANDED SCAN — Running now...\n")

    # 1. Top 20 Binance
    expanded = await scanner.get_expanded_symbols(top_n=20)
    all_syms = list(set(expanded + ["BTC/USDT", "ETH/USDT"]))
    data.symbols = all_syms
    data_map = await data.fetch_all()
    print(f"✅ Fetched data for {len(data_map)} symbols: {', '.join(data_map.keys())}\n")

    # 2. Spot signals
    spot_signals = []
    for sym in expanded:
        df = data_map.get(sym)
        if df is None: continue
        ind = ind_eng.calculate(sym, df)
        if ind is None: continue
        sig = strat.evaluate(ind)
        spot_signals.append(sig)

    # 3. Options signals
    options_candidates = []
    for sym in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
        df = data_map.get(sym)
        if df is None: continue
        sig = opts_strat.evaluate(sym, df)
        if sig.signal != OptionsSignal.NONE:
            spot = float(df["close"].iloc[-1])
            chosen = "straddle" if sig.signal == OptionsSignal.ENTER_STRADDLE else "strangle"
            underlying = sym.replace("/USDT", "")
            try:
                pair = await opts_chain.get_straddle(underlying, spot, chosen)
            except Exception:
                pair = None
            options_candidates.append((sig, pair))
        else:
            options_candidates.append((sig, None))

    # 4. Rank Binance + Deribit
    ranked = ranker.rank(spot_signals, options_candidates)
    print("━━━ BINANCE / DERIBIT SIGNALS ━━━")
    if ranked:
        for i, opp in enumerate(ranked[:5], 1):
            print(f"  {i}. {opp.label:<30} score={opp.score:>6.1f}  conf={opp.confidence}")
    else:
        print("  No signals above threshold")

    # 5. Delta Exchange
    print("\n━━━ DELTA EXCHANGE ━━━")
    delta_opps = await delta.scan()
    if delta_opps:
        for i, opp in enumerate(delta_opps[:5], 1):
            print(f"  {i}. {opp.label:<35} score={opp.score:>6.1f}")
            print(f"     {opp.reason}")
    else:
        print("  No opportunities found")

    # 6. Overall best
    print("\n━━━ BEST OPPORTUNITY ACROSS ALL MARKETS ━━━")
    all_scores = []
    if ranked:
        all_scores.append(("Binance/Deribit", ranked[0].label, ranked[0].score, ranked[0].confidence))
    if delta_opps:
        all_scores.append(("Delta Exchange", delta_opps[0].label, delta_opps[0].score, "MANUAL"))
    if all_scores:
        all_scores.sort(key=lambda x: x[2], reverse=True)
        for src, label, score, conf in all_scores:
            print(f"  [{src}] {label}  score={score:.1f}  action={conf}")
    else:
        print("  No opportunities found across any market")

    await exchange.close()
    await deribit.close()

asyncio.run(main())
