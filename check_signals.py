"""Quick diagnostic — shows current indicator values and why signals aren't firing."""
import asyncio, yaml, sys
sys.path.insert(0, ".")

async def main():
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    from src.exchange_factory import create_exchange
    from src.data_engine import DataEngine
    from src.indicator_engine import IndicatorEngine
    from src.strategy_engine import StrategyEngine
    from src.market_scanner import MarketScanner

    exchange = create_exchange(config)
    data     = DataEngine(exchange, config)
    ind_eng  = IndicatorEngine(config)
    strat    = StrategyEngine(config)
    scanner  = MarketScanner(exchange, config)

    symbols = await scanner.get_symbols() or config["trading"]["symbols"]
    print(f"\nScanning: {symbols}\n")
    print(f"{'Symbol':<12} {'Close':>10} {'EMA20':>10} {'EMA50':>10} {'RSI':>6} {'VolRatio':>9} {'High20':>10} | Conditions")
    print("-" * 105)

    for sym in symbols:
        df = await data.fetch(sym)
        if df is None or df.empty:
            print(f"{sym:<12} No data"); continue
        ind = ind_eng.calculate(sym, df)
        r   = strat.evaluate(ind)

        trend   = "✅" if ind.ema_fast > ind.ema_slow else "❌"
        brkout  = "✅" if ind.close > ind.high_20    else "❌"
        vol     = "✅" if ind.volume_ratio >= 1.5    else "❌"
        rsi_ok  = "✅" if 55 <= ind.rsi <= 70        else "❌"
        signal  = r.signal.value

        print(
            f"{sym:<12} {ind.close:>10.2f} {ind.ema_fast:>10.2f} {ind.ema_slow:>10.2f} "
            f"{ind.rsi:>6.1f} {ind.volume_ratio:>9.2f} {ind.high_20:>10.2f} | "
            f"Trend{trend} Break{brkout} Vol{vol} RSI{rsi_ok}  → {signal}"
        )

    await exchange.close()

asyncio.run(main())
