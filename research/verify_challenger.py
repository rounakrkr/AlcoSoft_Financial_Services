# CHALLENGER ADVERSARIAL VERIFICATION SCRIPT
import os
import sys
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.build_cache import load_cache
from research.verify_dual_engine_enterprise import (
    SystemConfig, LongEngineConfig, ShortEngineConfig,
    MarketRegimeAnalyzer, IndicatorPreprocessor, SignalGenerator,
    LongEngineExecutor, ShortEngineExecutor, ReportingEngine
)

def run_adversarial_checks():
    print("=== STARTING ADVERSARIAL CHECKS ===")
    
    # Load Cache
    stock_dfs = load_cache()
    
    # Check 1: Truncation & Indicator Memory Lookahead Bias
    # When IndicatorPreprocessor.enrich_data runs, it calculates indicators on the cached df.
    # The first 20 candles of the cached df will have NaN for ema21.
    # Then it does fillna(method="bfill").
    # Let's inspect the first 20 candles of a stock's newly computed ema21 and compare it with 
    # the cached ema21 (which was computed on the full raw dataset before truncation).
    
    sample_sym = list(stock_dfs.keys())[0]
    raw_df = stock_dfs[sample_sym].copy()
    
    # Recalculate indicators
    IndicatorPreprocessor.enrich_data(stock_dfs)
    
    recalc_df = stock_dfs[sample_sym]
    
    print(f"\n[Check 1] Recalculation Discrepancy on {sample_sym}:")
    diff_ema = recalc_df["ema21"] - raw_df["ema21"]
    max_diff_ema = diff_ema.abs().max()
    print(f"  Max difference in ema21 (recalculated vs cached): {max_diff_ema:.6f}")
    
    # Print first 25 rows comparing raw and recalc ema21
    print("  First 5 rows comparison:")
    for i in range(5):
        t = recalc_df.index[i]
        print(f"    {t} | Recalc EMA21: {recalc_df['ema21'].iloc[i]:.4f} | Cached EMA21: {raw_df['ema21'].iloc[i]:.4f} | Close: {recalc_df['close'].iloc[i]:.4f}")
        
    # Check 2: Lookahead in fillna(method="bfill")
    # Let's verify if any trades were entered or managed within the first 20 candles of any stock.
    # First, let's run the backtests and capture the trades.
    sys_config = SystemConfig()
    long_config = LongEngineConfig()
    short_config = ShortEngineConfig()
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
    signal_gen = SignalGenerator(stock_dfs)
    signal_gen.precompute_signals()
    
    long_executor = LongEngineExecutor(sys_config, long_config, stock_dfs, regime_analyzer, signal_gen.long_signals)
    long_trades = long_executor.execute()
    
    short_executor = ShortEngineExecutor(sys_config, short_config, stock_dfs, regime_analyzer, signal_gen.short_signals)
    short_trades = short_executor.execute()
    
    # For each trade, check if its entry_time or exit_time falls in the first 20 candles of the stock.
    lookahead_trades = []
    for t in long_trades:
        df = stock_dfs[t.symbol]
        entry_idx = df.index.get_loc(t.entry_time)
        exit_idx = df.index.get_loc(t.exit_time)
        if entry_idx < 20 or exit_idx < 20:
            lookahead_trades.append((t, entry_idx, exit_idx))
            
    print(f"\n[Check 2] Lookahead affected trades: {len(lookahead_trades)}")
    for t, en_idx, ex_idx in lookahead_trades:
        print(f"  Trade: {t.symbol} Long, Entry: {t.entry_time} (idx {en_idx}), Exit: {t.exit_time} (idx {ex_idx}), Reason: {t.exit_reason}")
        
    # Check 3: Check for simultaneous Entry and Exit execution on the same candle.
    # E.g., does entry candle's high/low trigger SL/TP?
    same_candle_exits = []
    for t in long_trades + short_trades:
        if t.entry_time == t.exit_time:
            same_candle_exits.append(t)
    print(f"\n[Check 3] Same candle exits (Entry Time == Exit Time): {len(same_candle_exits)}")
    for t in same_candle_exits:
        print(f"  {t.symbol} {t.direction} at {t.entry_time} exited on same candle for reason: {t.exit_reason}")
        
    # Check 4: Check if any trade has entry_time >= exit_time (invalid chronology)
    chronology_errors = []
    for t in long_trades + short_trades:
        if t.entry_time > t.exit_time:
            chronology_errors.append(t)
    print(f"\n[Check 4] Chronology Errors (Entry Time > Exit Time): {len(chronology_errors)}")
    for t in chronology_errors:
        print(f"  {t.symbol} {t.direction} entry {t.entry_time} > exit {t.exit_time}")
        
    # Check 5: Verify combined results
    print("\n[Check 5] Combined Portfolio Verification:")
    combined = long_trades + short_trades
    print(f"  Total Trades: {len(combined)}")
    metrics = ReportingEngine._calculate_metrics(combined, sys_config.capital)
    for k, v in metrics.items():
        print(f"    {k}: {str(v).replace('₹', 'Rs.')}")


    # Check 6: Portfolio overlap check
    # Check if a Long position and a Short position were open at the same time
    overlapping_periods = []
    for lt in long_trades:
        for st in short_trades:
            # Overlap condition: max(lt.entry, st.entry) < min(lt.exit, st.exit)
            overlap_start = max(lt.entry_time, st.entry_time)
            overlap_end = min(lt.exit_time, st.exit_time)
            if overlap_start < overlap_end:
                overlapping_periods.append((lt, st, overlap_start, overlap_end))
                
    print(f"\n[Check 6] Overlapping Long and Short Positions: {len(overlapping_periods)}")
    for lt, st, os_time, oe_time in overlapping_periods:
        print(f"  Overlap from {os_time} to {oe_time}:")
        print(f"    LONG: {lt.symbol} ({lt.entry_time} to {lt.exit_time})")
        print(f"    SHORT: {st.symbol} ({st.entry_time} to {st.exit_time})")

if __name__ == "__main__":
    run_adversarial_checks()

