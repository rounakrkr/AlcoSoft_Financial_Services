import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import pandas as pd
from research.build_cache import load_cache
from research.verify_dual_engine_enterprise_opt import (
    SystemConfig,
    LongEngineConfig,
    ShortEngineConfig,
    IndicatorPreprocessor,
    MarketRegimeAnalyzer,
    SignalGenerator,
    LongEngineExecutor,
    ShortEngineExecutor
)

print("Loading cache...")
stock_dfs = load_cache()
from screener.morning_screener import NIFTY_50
stock_dfs = {sym: df for sym, df in stock_dfs.items() if sym in NIFTY_50}

print("Enriching and precomputing signals for ONE symbol to run quickly...")
# Let's filter to just 2 symbols for quick profiling
profile_syms = list(stock_dfs.keys())[:2]
stock_dfs_sub = {sym: stock_dfs[sym] for sym in profile_syms}
IndicatorPreprocessor.enrich_data(stock_dfs_sub)
regime_analyzer = MarketRegimeAnalyzer(stock_dfs_sub)
signal_gen = SignalGenerator(stock_dfs_sub)
signal_gen.precompute_signals(start_date="2026-01-01")

sys_config = SystemConfig()
long_config = LongEngineConfig()

long_executor = LongEngineExecutor(
    sys_config=sys_config,
    long_config=long_config,
    stock_dfs=stock_dfs_sub,
    regime_analyzer=regime_analyzer,
    signals=signal_gen.long_signals
)

print("Profiling execution...")
t0 = time.time()
for i in range(10):
    long_executor.execute(start_date="2026-01-01", end_date="2026-06-20")
t1 = time.time()
print(f"Time for 10 executions: {t1 - t0:.4f} seconds (average {(t1 - t0)/10:.4f} seconds per execution).")
