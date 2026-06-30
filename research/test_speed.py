import sys
import os
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sweep_midcap50_engine import load_cache, IndicatorPreprocessor, MarketRegimeAnalyzer, SignalGenerator, run_backtest, SystemConfig, LongEngineConfig, ShortEngineConfig

def main():
    print("Loading cache...")
    stock_dfs = load_cache()
    print("Enriching...")
    IndicatorPreprocessor.enrich_data(stock_dfs)
    print("Regime...")
    regime = MarketRegimeAnalyzer(stock_dfs)
    print("Signals...")
    signal_gen = SignalGenerator(stock_dfs)
    signal_gen.precompute_signals()
    
    print("Precalculating signal timestamps...")
    long_ts_with_signals = set()
    long_signals_by_ts = {}
    for sym, sigs in signal_gen.long_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                long_ts_with_signals.add(ts)
                long_signals_by_ts.setdefault(ts, []).append(sym)
                
    short_ts_with_signals = set()
    short_signals_by_ts = {}
    for sym, sigs in signal_gen.short_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                short_ts_with_signals.add(ts)
                short_signals_by_ts.setdefault(ts, []).append(sym)
                
    sys_cfg = SystemConfig()
    long_cfg = LongEngineConfig(dyn_exit_type="DISABLE")
    short_cfg = ShortEngineConfig()
    
    t0 = time.time()
    print("Running cold backtest...")
    long_trades, short_trades, metrics = run_backtest(
        sys_config=sys_cfg,
        long_config=long_cfg,
        short_config=short_cfg,
        stock_dfs=stock_dfs,
        regime_analyzer=regime,
        long_signals=signal_gen.long_signals,
        short_signals=signal_gen.short_signals,
        long_ts_with_signals=long_ts_with_signals,
        long_signals_by_ts=long_signals_by_ts,
        short_ts_with_signals=short_ts_with_signals,
        short_signals_by_ts=short_signals_by_ts,
        verbose=False
    )
    t1 = time.time()
    print(f"Cold backtest took: {t1 - t0:.6f} seconds")
    print(f"Metrics: {metrics}")

    t2 = time.time()
    print("Running hot backtest...")
    long_trades, short_trades, metrics = run_backtest(
        sys_config=sys_cfg,
        long_config=long_cfg,
        short_config=short_cfg,
        stock_dfs=stock_dfs,
        regime_analyzer=regime,
        long_signals=signal_gen.long_signals,
        short_signals=signal_gen.short_signals,
        long_ts_with_signals=long_ts_with_signals,
        long_signals_by_ts=long_signals_by_ts,
        short_ts_with_signals=short_ts_with_signals,
        short_signals_by_ts=short_signals_by_ts,
        verbose=False
    )
    t3 = time.time()
    print(f"Hot backtest took: {t3 - t2:.6f} seconds")
    print(f"Metrics: {metrics}")

if __name__ == "__main__":
    main()
