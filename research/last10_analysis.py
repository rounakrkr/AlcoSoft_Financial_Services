import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

from research.verify_dual_engine_enterprise import MarketRegimeAnalyzer, LongEngineConfig, ShortEngineConfig
from screener.morning_screener import NIFTY_50

RESEARCH_DIR = os.path.dirname(os.path.abspath(__file__))

def load_and_analyze(cache_path, label):
    with open(cache_path, "rb") as f:
        data = pickle.load(f)
    dfs = {sym: df for sym, df in data["stock_dfs"].items() if sym in NIFTY_50}
    
    # Normalize tz
    for sym, df in dfs.items():
        if df.index.tzinfo is not None:
            df.index = df.index.tz_localize(None)
    
    regime = MarketRegimeAnalyzer(dfs)
    long_config  = LongEngineConfig()
    short_config = ShortEngineConfig()
    
    bull_days = regime.get_bull_days(long_config)
    bear_days = regime.get_bear_days(short_config)
    
    # Last 10 trading days
    last10 = sorted(regime.trading_dates)[-10:]
    
    print(f"\n{'='*65}")
    print(f"  {label} — Last 10 Trading Days")
    print(f"  Gap >=+1.0% for Bull | Gap <=-0.6% for Bear | Breadth >= 40%")
    print(f"{'='*65}")
    print(f"  {'Date':<14} {'Bull%':>6} {'Bear%':>6} {'Type':<14} {'Top Gap'}")
    print(f"  {'-'*60}")
    
    for d in last10:
        gaps = [(s, g) for (date, s), g in regime.all_daily_gaps.items() if date == d]
        total = len(gaps)
        if total == 0:
            continue
        
        bull_q = sum(1 for _, g in gaps if g >= long_config.market_gap_threshold)
        bear_q = sum(1 for _, g in gaps if g <= short_config.market_gap_threshold)
        bull_ratio = bull_q / total * 100
        bear_ratio = bear_q / total * 100
        
        is_bull = d in bull_days
        is_bear = d in bear_days
        
        if is_bull and is_bear:
            day_type = "BULL + BEAR"
        elif is_bull:
            day_type = "BULL DAY"
        elif is_bear:
            day_type = "BEAR DAY"
        else:
            day_type = "NEUTRAL"
        
        # Top gap of the day
        gaps_sorted = sorted(gaps, key=lambda x: x[1], reverse=True)
        top_gap = f"{gaps_sorted[0][0]}: {gaps_sorted[0][1]*100:+.2f}%" if gaps_sorted else ""
        
        print(f"  {str(d):<14} {bull_ratio:>5.1f}% {bear_ratio:>6.1f}% {day_type:<14} {top_gap}")
    
    print(f"\n  Total Bull Days (full history): {len(bull_days)}")
    print(f"  Total Bear Days (full history): {len(bear_days)}")
    return bull_days, bear_days

load_and_analyze(os.path.join(RESEARCH_DIR, "data_cache.pkl"), "UPSTOX")
load_and_analyze(os.path.join(RESEARCH_DIR, "yfinance_cache.pkl"), "YFINANCE")
