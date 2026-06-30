"""
APPLES-TO-APPLES COMPARISON (FIXED INDICATOR WARMUP)
====================================================
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import logging
import warnings
warnings.filterwarnings("ignore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("Comparison")

from research.verify_dual_engine_enterprise import (
    SystemConfig, LongEngineConfig, ShortEngineConfig,
    IndicatorPreprocessor, MarketRegimeAnalyzer,
    SignalGenerator, LongEngineExecutor, ShortEngineExecutor,
    ReportingEngine
)
from screener.morning_screener import NIFTY_50

RESEARCH_DIR = os.path.dirname(os.path.abspath(__file__))
YF_CACHE_PATH  = os.path.join(RESEARCH_DIR, "yfinance_cache.pkl")
UPX_CACHE_PATH = os.path.join(RESEARCH_DIR, "data_cache.pkl")

# ============================================================
# STEP 1: Load caches without truncating raw data!
# ============================================================
with open(YF_CACHE_PATH, "rb") as f:
    yf_data = pickle.load(f)
yf_dfs_raw = {sym: df for sym, df in yf_data["stock_dfs"].items() if sym in NIFTY_50}

with open(UPX_CACHE_PATH, "rb") as f:
    upx_data = pickle.load(f)
upx_dfs_raw = {sym: df for sym, df in upx_data["stock_dfs"].items() if sym in NIFTY_50}

# Find common symbols
common_syms = set(yf_dfs_raw.keys()) & set(upx_dfs_raw.keys())

yf_dfs  = {s: yf_dfs_raw[s].copy()  for s in common_syms}
upx_dfs = {s: upx_dfs_raw[s].copy() for s in common_syms}

# Determine common execution range based on YFinance data limits
yf_start = max(df.index.min() for df in yf_dfs.values())
yf_end   = min(df.index.max() for df in yf_dfs.values())

if yf_start.tzinfo is not None: yf_start = yf_start.tz_localize(None)
if yf_end.tzinfo is not None: yf_end = yf_end.tz_localize(None)

# We will start execution from the first available date + 15 days in Yfinance to allow for indicator warmup
# Wait, Yfinance data only has 60 days. If we skip 15 days, we only test 45 days.
# But yesterday, the yfinance cache ALREADY had indicators calculated when it was built!
# Let's just use the start_date 2026-03-24 for execution filtering.

execute_start_date = "2026-03-24"
execute_end_date = "2026-06-19"

def run_engine(stock_dfs, label):
    # Fix timezones
    for sym, df in stock_dfs.items():
        if df.index.tzinfo is not None:
            df.index = df.index.tz_localize(None)
            
    sys_config   = SystemConfig()
    long_config  = LongEngineConfig()
    short_config = ShortEngineConfig()
    
    # We DO NOT truncate stock_dfs here. We pass the full history to IndicatorPreprocessor 
    # so that EMA, RSI, and Daily Gaps calculate correctly!
    IndicatorPreprocessor.enrich_data(stock_dfs)
    regime = MarketRegimeAnalyzer(stock_dfs)
    
    sig_gen = SignalGenerator(stock_dfs)
    sig_gen.precompute_signals(start_date=execute_start_date)
    
    long_exec = LongEngineExecutor(
        sys_config=sys_config, long_config=long_config,
        stock_dfs=stock_dfs, regime_analyzer=regime,
        signals=sig_gen.long_signals
    )
    long_trades = long_exec.execute(start_date=execute_start_date, end_date=execute_end_date)
    
    short_exec = ShortEngineExecutor(
        sys_config=sys_config, short_config=short_config,
        stock_dfs=stock_dfs, regime_analyzer=regime,
        signals=sig_gen.short_signals
    )
    short_trades = short_exec.execute(start_date=execute_start_date, end_date=execute_end_date)
    
    out_file = os.path.join(RESEARCH_DIR, f"{label.lower().replace(' ', '_')}_report.md")
    ReportingEngine.print_tearsheet(long_trades, short_trades, sys_config.capital, stock_dfs, out_path=out_file)
    return long_trades, short_trades

yf_long,  yf_short  = run_engine(yf_dfs,  "yfinance")
upx_long, upx_short = run_engine(upx_dfs, "upstox")

# Summary
def summarize(long_trades, short_trades, label):
    all_trades = long_trades + short_trades
    net_pnl  = sum(t.pnl_net for t in all_trades)
    print(f"{label}: Net Return = {net_pnl/100000*100:.2f}% | Total Trades = {len(all_trades)}")

print("\n\n" + "★"*60)
summarize(yf_long, yf_short, "YFinance")
summarize(upx_long, upx_short, "Upstox")
print("★"*60)
