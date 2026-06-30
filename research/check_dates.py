import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from screener.morning_screener import NIFTY_50
from research.verify_dual_engine_enterprise import load_cache, SignalGenerator, RegimeClassifier, DualEngineConfig

stock_dfs = load_cache()
stock_dfs = {sym: df for sym, df in stock_dfs.items() if sym in NIFTY_50}

signal_gen = SignalGenerator(stock_dfs)
daily_gaps = signal_gen.compute_daily_gaps()

regime = RegimeClassifier(daily_gaps)
config = DualEngineConfig()

bull_days = regime.get_bull_days(config.long_engine)
bear_days = regime.get_bear_days(config.short_engine)

target_start = pd.to_datetime("2026-03-20").date()

recent_bulls = sorted([d for d in bull_days if d >= target_start])
recent_bears = sorted([d for d in bear_days if d >= target_start])

print(f"BULL DAYS ({len(recent_bulls)}):")
for d in recent_bulls: print("  ", d)
print(f"BEAR DAYS ({len(recent_bears)}):")
for d in recent_bears: print("  ", d)
