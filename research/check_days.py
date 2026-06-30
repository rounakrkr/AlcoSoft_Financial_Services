import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from screener.morning_screener import NIFTY_50
from research.verify_dual_engine_enterprise import load_cache, SignalGenerator, MarketRegimeAnalyzer, LongEngineConfig, ShortEngineConfig

stock_dfs = load_cache()
stock_dfs = {sym: df for sym, df in stock_dfs.items() if sym in NIFTY_50}

regime = MarketRegimeAnalyzer(stock_dfs)
long_config = LongEngineConfig()
short_config = ShortEngineConfig()

bull_days = regime.get_bull_days(long_config)
bear_days = regime.get_bear_days(short_config)

print(f"\nTotal Trading Days: {len(regime.trading_dates)}")
print(f"Total Bull Days in entire history: {len(bull_days)}")
print(f"Total Bear Days in entire history: {len(bear_days)}\n")

# Let's check June 12th and 15th specifically
target_dates = [pd.to_datetime("2026-06-12").date(), pd.to_datetime("2026-06-15").date()]

for d in target_dates:
    if d in regime.trading_dates:
        gaps = [g for (date, s), g in regime.all_daily_gaps.items() if date == d]
        qualified = sum(1 for g in gaps if g >= long_config.market_gap_threshold)
        ratio = qualified / len(gaps) if gaps else 0
        print(f"Date: {d} | Total Stocks: {len(gaps)} | Qualified Gaps (>= {long_config.market_gap_threshold*100}%): {qualified} | Ratio: {ratio*100:.1f}%")
        print(f"  Is Bull Day? {'YES' if d in bull_days else 'NO'}")
        
        # Print top 5 gaps
        gaps_with_syms = [(s, g) for (date, s), g in regime.all_daily_gaps.items() if date == d]
        gaps_with_syms.sort(key=lambda x: x[1], reverse=True)
        print("  Top 5 gaps:")
        for sym, gap in gaps_with_syms[:5]:
            print(f"    {sym}: {gap*100:.2f}%")
    else:
        print(f"Date: {d} is NOT in trading dates.")
