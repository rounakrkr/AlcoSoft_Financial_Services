import sys
import os
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sweep_midcap50_engine import (
    load_cache,
    IndicatorPreprocessor,
    MarketRegimeAnalyzer,
    SignalGenerator,
    SystemConfig,
    LongEngineConfig,
    ShortEngineConfig,
    run_backtest,
    LongEngineExecutor,
    ShortEngineExecutor
)

def main():
    print("Loading data...")
    stock_dfs = load_cache()
    
    print("Enriching data...")
    IndicatorPreprocessor.enrich_data(stock_dfs)
    
    print("Analyzing market regime...")
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
    
    # 4. Generate Signals
    signal_gen = SignalGenerator(stock_dfs)
    
    # Precalculate Short Signals once
    short_ts_with_signals = set()
    short_signals_by_ts = {}
    signal_gen.precompute_signals("BASELINE")
    for sym, sigs in signal_gen.short_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                short_ts_with_signals.add(ts)
                short_signals_by_ts.setdefault(ts, []).append(sym)
                
    sys_cfg = SystemConfig(capital=100000.0, margin=5.0, max_open_positions=1)
    long_cfg = LongEngineConfig(stop_loss_pct=0.007, profit_target_pct=0.025, rsi_exit_threshold=78.0, market_gap_threshold=0.007, market_breadth_requirement=0.35, partial_booking_fraction=0.25, dyn_exit_type="DISABLE", dyn_exit_hold_time=0)
    short_cfg = ShortEngineConfig(disable_shorts=False)
    
    # Run 1: BASELINE in isolation
    signal_gen.precompute_signals("BASELINE")
    long_ts_with_signals_baseline = set()
    long_signals_by_ts_baseline = {}
    for sym, sigs in signal_gen.long_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                long_ts_with_signals_baseline.add(ts)
                long_signals_by_ts_baseline.setdefault(ts, []).append(sym)
                
    _, _, metrics_baseline_1 = run_backtest(
        sys_config=sys_cfg,
        long_config=long_cfg,
        short_config=short_cfg,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        long_signals=signal_gen.long_signals,
        short_signals=signal_gen.short_signals,
        long_ts_with_signals=long_ts_with_signals_baseline,
        long_signals_by_ts=long_signals_by_ts_baseline,
        short_ts_with_signals=short_ts_with_signals,
        short_signals_by_ts=short_signals_by_ts,
        verbose=False
    )
    
    # Run 2: VARIANT_E in isolation
    signal_gen.precompute_signals("VARIANT_E")
    long_ts_with_signals_var_e = set()
    long_signals_by_ts_var_e = {}
    for sym, sigs in signal_gen.long_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                long_ts_with_signals_var_e.add(ts)
                long_signals_by_ts_var_e.setdefault(ts, []).append(sym)
                
    _, _, metrics_var_e_1 = run_backtest(
        sys_config=sys_cfg,
        long_config=long_cfg,
        short_config=short_cfg,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        long_signals=signal_gen.long_signals,
        short_signals=signal_gen.short_signals,
        long_ts_with_signals=long_ts_with_signals_var_e,
        long_signals_by_ts=long_signals_by_ts_var_e,
        short_ts_with_signals=short_ts_with_signals,
        short_signals_by_ts=short_signals_by_ts,
        verbose=False
    )
    
    # Run 3: BASELINE again in loop
    signal_gen.precompute_signals("BASELINE")
    long_ts_with_signals_baseline_2 = set()
    long_signals_by_ts_baseline_2 = {}
    for sym, sigs in signal_gen.long_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                long_ts_with_signals_baseline_2.add(ts)
                long_signals_by_ts_baseline_2.setdefault(ts, []).append(sym)
                
    _, _, metrics_baseline_2 = run_backtest(
        sys_config=sys_cfg,
        long_config=long_cfg,
        short_config=short_cfg,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        long_signals=signal_gen.long_signals,
        short_signals=signal_gen.short_signals,
        long_ts_with_signals=long_ts_with_signals_baseline_2,
        long_signals_by_ts=long_signals_by_ts_baseline_2,
        short_ts_with_signals=short_ts_with_signals,
        short_signals_by_ts=short_signals_by_ts,
        verbose=False
    )
    
    print(f"BASELINE (Run 1): Net Return = {metrics_baseline_1['Net Return']}")
    print(f"VARIANT_E (Run 2): Net Return = {metrics_var_e_1['Net Return']}")
    print(f"BASELINE (Run 3): Net Return = {metrics_baseline_2['Net Return']}")
    
    assert metrics_baseline_1['Net Return'] != metrics_var_e_1['Net Return'], "Error: BASELINE and VARIANT_E returns are the same!"
    assert metrics_baseline_1['Net Return'] == metrics_baseline_2['Net Return'], "Error: BASELINE returns differ between Run 1 and Run 3 (cache contamination!)"
    
    print("SUCCESS: Caching bug is successfully verified as fixed!")

if __name__ == "__main__":
    main()
