import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pickle
import time
import pandas as pd
from research.build_cache import load_cache
from research.strategy_sets_opt import load_strategy_sets
from research.strategy_opt import StrategySetEvaluator, StrategyEvaluationContext, CONDITION_REGISTRY

print("Loading cache...")
stock_dfs = load_cache()
from screener.morning_screener import NIFTY_50
stock_dfs = {sym: df for sym, df in stock_dfs.items() if sym in NIFTY_50}

print(f"Loaded {len(stock_dfs)} symbols.")
for sym, df in list(stock_dfs.items())[:3]:
    print(f"Symbol {sym}: shape={df.shape}, date range={df.index.min()} to {df.index.max()}")

# Load strategy sets
config = load_strategy_sets()
buy_set_long = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

sym = list(stock_dfs.keys())[0]
df = stock_dfs[sym]
target_start = pd.to_datetime("2026-01-01").date()
future_dates = df.index.date >= target_start
loop_start = max(10, future_dates.argmax()) if future_dates.any() else 10

print(f"Profiling {sym} from index {loop_start} to {len(df)} (total iterations: {len(df) - loop_start})...")
t0 = time.time()
count = 0
for i in range(loop_start, len(df)):
    start_idx = max(0, i-100)
    sliced = df.iloc[start_idx:i+1]
    
    # Evaluate Long
    c_le = evaluator._evaluate_conditions(
        buy_set_long, 
        StrategyEvaluationContext("buy", sliced, sliced, len(sliced))
    )
    count += 1
    if count >= 100:
        break
t1 = time.time()
print(f"Time for 100 iterations: {t1 - t0:.4f} seconds.")
print(f"Estimated time for full loop of {len(df) - loop_start} iterations: {(t1 - t0) * (len(df) - loop_start) / 100:.2f} seconds.")
