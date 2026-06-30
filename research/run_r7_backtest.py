import sys
import os
import pandas as pd
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sweep_midcap50_engine import (
    load_cache,
    IndicatorPreprocessor,
    MarketRegimeAnalyzer,
    SignalGenerator,
    SystemConfig,
    LongEngineConfig,
    ShortEngineConfig,
    run_backtest
)

def default_serializer(obj):
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    return str(obj)

def main():
    print("Loading data...")
    stock_dfs = load_cache()
    
    print("Filtering data for Jan 2024 onwards...")
    for sym in list(stock_dfs.keys()):
        df = stock_dfs[sym]
        stock_dfs[sym] = df[df.index >= '2024-01-01']
        if stock_dfs[sym].empty:
            del stock_dfs[sym]

    print("Enriching data...")
    IndicatorPreprocessor.enrich_data(stock_dfs)
    
    print("Analyzing market regime...")
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
    
    print("Generating Signals for VARIANT_D...")
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
                
    # R7_COMB_486 Long parameters
    long_params = {
        "max_open_positions": 1,
        "stop_loss_pct": 0.0100,
        "profit_target_pct": 0.0250,
        "rsi_exit_threshold": 78.0,
        "market_gap_threshold": 0.0070,
        "market_breadth_requirement": 0.35,
        "partial_booking_fraction": 0.25,
        "dyn_exit_type": "EMA50",
        "dyn_exit_hold_time": 0,
        "reentry_cap": 9999,
        "min_hold_time": 20,
        "min_price": 0.0,
        "min_expected_pnl": 0.0,
        "entry_variant": "VARIANT_D"
    }
    
    # R7_COMB_486 Short parameters
    short_params = {
        "market_gap_threshold": -0.006,
        "market_breadth_requirement": 0.40,
        "target_gap_threshold": -0.0150,
        "rsi_exit_threshold": 17.0,
        "profit_target_pct": 0.0250,
        "partial_booking_fraction": 1.0,
        "stop_loss_pct": 0.0050,
        "disable_shorts": False,
        "savior_exit": False
    }

    signal_gen.precompute_signals(long_params["entry_variant"])
    long_ts_with_signals = set()
    long_signals_by_ts = {}
    for sym, sigs in signal_gen.long_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                long_ts_with_signals.add(ts)
                long_signals_by_ts.setdefault(ts, []).append(sym)

    # Capital: 100000, Margin: 5
    sys_cfg = SystemConfig(
        capital=100000.0,
        margin=5.0,
        max_open_positions=long_params["max_open_positions"]
    )
    long_cfg = LongEngineConfig(
        market_gap_threshold=long_params["market_gap_threshold"],
        market_breadth_requirement=long_params["market_breadth_requirement"],
        exclude_gap_threshold=-0.008,
        rsi_exit_threshold=long_params["rsi_exit_threshold"],
        profit_target_pct=long_params["profit_target_pct"],
        partial_booking_fraction=long_params["partial_booking_fraction"],
        stop_loss_pct=long_params["stop_loss_pct"],
        dyn_exit_type=long_params["dyn_exit_type"],
        dyn_exit_hold_time=long_params["dyn_exit_hold_time"],
        reentry_cap=long_params["reentry_cap"],
        min_hold_time=long_params["min_hold_time"],
        min_price=long_params["min_price"],
        min_expected_pnl=long_params["min_expected_pnl"]
    )
    short_cfg = ShortEngineConfig(
        market_gap_threshold=short_params["market_gap_threshold"],
        market_breadth_requirement=short_params["market_breadth_requirement"],
        target_gap_threshold=short_params["target_gap_threshold"],
        rsi_exit_threshold=short_params["rsi_exit_threshold"],
        profit_target_pct=short_params["profit_target_pct"],
        partial_booking_fraction=short_params["partial_booking_fraction"],
        stop_loss_pct=short_params["stop_loss_pct"],
        disable_shorts=short_params["disable_shorts"],
        savior_exit=short_params["savior_exit"]
    )

    print("Running backtest for R7_COMB_486 (Jan 2024+)...")
    long_trades, short_trades, metrics = run_backtest(
        sys_config=sys_cfg,
        long_config=long_cfg,
        short_config=short_cfg,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        long_signals=signal_gen.long_signals,
        short_signals=signal_gen.short_signals,
        long_ts_with_signals=long_ts_with_signals,
        long_signals_by_ts=long_signals_by_ts,
        short_ts_with_signals=short_ts_with_signals,
        short_signals_by_ts=short_signals_by_ts,
        verbose=False
    )

    print("\n--- RESULTS ---")
    print(f"Total Trades: {metrics['Total Trades']}")
    print(f"Win Rate: {metrics['Win Rate']}")
    print(f"Net Return: {metrics['Net Return']}")
    print(f"Expectancy: {metrics['Expectancy']}")

    output_data = {
        "metrics": metrics,
        "long_trades": long_trades,
        "short_trades": short_trades
    }

    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw_r7_data.json")
    with open(json_path, "w") as f:
        json.dump(output_data, f, default=default_serializer, indent=2)

    sync_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_sync.txt")
    with open(sync_file, "w") as f:
        f.write("DATA_READY\n")

    print(f"Saved results to {json_path}")
    print(f"Updated {sync_file}")

if __name__ == "__main__":
    main()
