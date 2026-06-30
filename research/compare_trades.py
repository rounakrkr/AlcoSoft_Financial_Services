import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["STRATEGY_SETS_PATH"] = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "strategy_sets_opt.json")
)

import pandas as pd
from research.build_cache import load_cache
from research.verify_dual_engine_enterprise_opt import (
    SystemConfig,
    LongEngineConfig,
    ShortEngineConfig,
    IndicatorPreprocessor,
    MarketRegimeAnalyzer,
    SignalGenerator,
    LongEngineExecutor
)
from research.sweep_opt_enterprise import FastLongEngineExecutor

print("Loading cache...")
stock_dfs = load_cache()
from screener.morning_screener import NIFTY_50
stock_dfs = {sym: df for sym, df in stock_dfs.items() if sym in NIFTY_50}

IndicatorPreprocessor.enrich_data(stock_dfs)
regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
signal_gen = SignalGenerator(stock_dfs)
signal_gen.precompute_signals(start_date="2026-01-01")

sys_config = SystemConfig(max_open_positions=1)
long_config = LongEngineConfig()

orig_long = LongEngineExecutor(sys_config, long_config, stock_dfs, regime_analyzer, signal_gen.long_signals)
fast_long = FastLongEngineExecutor(sys_config, long_config, stock_dfs, regime_analyzer, signal_gen.long_signals)

trades_orig = orig_long.execute(start_date="2026-01-01", end_date="2026-06-20")
trades_fast = fast_long.execute(start_date="2026-01-01", end_date="2026-06-20")

print(f"Original Trades count: {len(trades_orig)}")
print(f"Fast Trades count: {len(trades_fast)}")

orig_set = set((t.symbol, t.entry_time, t.exit_time, t.exit_reason) for t in trades_orig)
fast_set = set((t.symbol, t.entry_time, t.exit_time, t.exit_reason) for t in trades_fast)

print("\n--- Trades in Original but not in Fast ---")
for t in sorted(orig_set - fast_set, key=lambda x: (x[0], x[1])):
    print(t)

print("\n--- Trades in Fast but not in Original ---")
for t in sorted(fast_set - orig_set, key=lambda x: (x[0], x[1])):
    print(t)
