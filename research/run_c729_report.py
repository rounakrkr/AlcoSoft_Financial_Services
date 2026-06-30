import sys
import os
import pickle
import pandas as pd
from collections import defaultdict
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sweep_midcap50_engine import (
    SystemConfig, 
    LongEngineConfig, 
    ShortEngineConfig, 
    MarketRegimeAnalyzer, 
    IndicatorPreprocessor,
    SignalGenerator,
    LongEngineExecutor,
    ShortEngineExecutor
)

def load_cache():
    cache_path = os.path.join(os.path.dirname(__file__), "midcap50_historical_cache.pkl")
    with open(cache_path, "rb") as f:
        data = pickle.load(f)
    return data["stock_dfs"]

def filter_data_from_2024(stock_dfs):
    filtered_dfs = {}
    for sym, df in stock_dfs.items():
        if len(df) > 0:
            if df.index.tz is None:
                start_date = pd.to_datetime('2024-01-01')
            else:
                start_date = pd.to_datetime('2024-01-01').tz_localize(df.index.tz)
            filtered_df = df[df.index >= start_date].copy()
            if len(filtered_df) > 0:
                filtered_dfs[sym] = filtered_df
    return filtered_dfs

def main():
    stock_dfs = load_cache()
    stock_dfs = filter_data_from_2024(stock_dfs)
    
    IndicatorPreprocessor.enrich_data(stock_dfs)
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
    signal_gen = SignalGenerator(stock_dfs)
    signal_gen.precompute_signals()
    
    sys_config = SystemConfig(capital=100000.0, margin=5.0)
    
    long_config = LongEngineConfig(
        market_breadth_requirement=0.35,
        market_gap_threshold=0.007,
        exclude_gap_threshold=-0.008,
        rsi_exit_threshold=72.0,
        profit_target_pct=0.015,
        partial_booking_fraction=0.25,
        stop_loss_pct=0.008,
        dyn_exit_type="EMA21",
        reentry_cap=1,
        min_hold_time=15
    )
    
    short_config = ShortEngineConfig(
        market_breadth_requirement=0.40,
        market_gap_threshold=-0.006,
        target_gap_threshold=-0.015,
        rsi_exit_threshold=30.0,
        profit_target_pct=0.025,
        partial_booking_fraction=1.00,
        stop_loss_pct=0.005
    )
    
    long_ts_with_signals = set()
    long_signals_by_ts = defaultdict(list)
    for sym, bool_list in signal_gen.long_signals.items():
        df = stock_dfs[sym]
        for idx, is_signal in enumerate(bool_list):
            if is_signal:
                ts = df.index[idx]
                long_ts_with_signals.add(ts)
                long_signals_by_ts[ts].append(sym)

    short_ts_with_signals = set()
    short_signals_by_ts = defaultdict(list)
    for sym, bool_list in signal_gen.short_signals.items():
        df = stock_dfs[sym]
        for idx, is_signal in enumerate(bool_list):
            if is_signal:
                ts = df.index[idx]
                short_ts_with_signals.add(ts)
                short_signals_by_ts[ts].append(sym)

    long_executor = LongEngineExecutor(
        sys_config=sys_config,
        long_config=long_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        signals=signal_gen.long_signals,
        ts_with_signals=long_ts_with_signals,
        signals_by_ts=long_signals_by_ts
    )
    long_trades = long_executor.execute()
    
    short_executor = ShortEngineExecutor(
        sys_config=sys_config,
        short_config=short_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        signals=signal_gen.short_signals,
        ts_with_signals=short_ts_with_signals,
        signals_by_ts=short_signals_by_ts
    )
    short_trades = short_executor.execute()
    
    all_trades = long_trades + short_trades
    
    monthly_stats = defaultdict(lambda: {
        "Gross PnL": 0.0,
        "Net PnL": 0.0,
        "Wins": 0,
        "Total Trades": 0,
        "STT Impact": 0.0,
        "Long Trades": 0,
        "Short Trades": 0,
        "Long PnL": 0.0,
        "Short PnL": 0.0,
        "Exit Reasons": defaultdict(int)
    })
    
    for t in all_trades:
        m = t.exit_time.strftime("%Y-%m")
        st = monthly_stats[m]
        
        gross = t.pnl_gross
        stt = t.stt_tax
        net = t.pnl_net
        
        st["Gross PnL"] += gross
        st["STT Impact"] += stt
        st["Net PnL"] += net
        st["Total Trades"] += 1
        if net > 0:
            st["Wins"] += 1
            
        if t.direction == "LONG":
            st["Long Trades"] += 1
            st["Long PnL"] += net
        else:
            st["Short Trades"] += 1
            st["Short PnL"] += net
            
        st["Exit Reasons"][t.exit_reason] += 1
        
    brain_dir = r"C:\Users\RounakKR\.gemini\antigravity\brain\00f3c219-35fc-4cc5-a5fa-92c75c861e1e"
    artifact_path = os.path.join(brain_dir, "c729_monthly_backtest_report.md")
    
    with open(artifact_path, "w", encoding="utf-8") as f:
        f.write("# AlcoSoft Master Config C729_579 - Monthly Backtest Report (2024-Present)\n\n")
        f.write("## Overview\n")
        f.write("- **Config**: C729_579\n")
        f.write("- **Capital**: Rs. 100,000 (5x Margin)\n")
        f.write("- **Universe**: Nifty Midcap 50\n\n")
        
        for m in sorted(monthly_stats.keys()):
            st = monthly_stats[m]
            win_rate = (st["Wins"] / st["Total Trades"] * 100) if st["Total Trades"] > 0 else 0
            
            f.write(f"### Month: {m}\n")
            f.write(f"- **Gross PnL**: Rs. {st['Gross PnL']:.2f}\n")
            f.write(f"- **STT Impact**: Rs. {st['STT Impact']:.2f}\n")
            f.write(f"- **Net PnL**: Rs. {st['Net PnL']:.2f}\n")
            f.write(f"- **Win Rate**: {win_rate:.2f}%\n")
            f.write(f"- **Total Trades**: {st['Total Trades']}\n")
            
            f.write(f"\n#### Breakdown by Direction\n")
            f.write(f"- **Long Trades**: {st['Long Trades']} (Net PnL: Rs. {st['Long PnL']:.2f})\n")
            f.write(f"- **Short Trades**: {st['Short Trades']} (Net PnL: Rs. {st['Short PnL']:.2f})\n")
            
            f.write(f"\n#### Breakdown by Exit Reason\n")
            for reason, count in sorted(st["Exit Reasons"].items()):
                f.write(f"- **{reason}**: {count}\n")
                
            f.write("\n---\n\n")
            
    print("Backtest complete and report generated at:", artifact_path)

if __name__ == "__main__":
    main()
