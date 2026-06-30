import sys
import os
import pandas as pd
from datetime import datetime

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

def main():
    print("Loading data...")
    stock_dfs = load_cache()
    
    print("Enriching data...")
    IndicatorPreprocessor.enrich_data(stock_dfs)
    
    print("Analyzing market regime...")
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
    
    print("Generating Signals...")
    signal_gen = SignalGenerator(stock_dfs)
    
    signal_gen.precompute_signals("BASELINE")
    short_ts_with_signals = set()
    short_signals_by_ts = {}
    for sym, sigs in signal_gen.short_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                short_ts_with_signals.add(ts)
                short_signals_by_ts.setdefault(ts, []).append(sym)
                
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

    print("Running backtest...")
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
    
    all_trades = []
    for t in long_trades + short_trades:
        all_trades.append({
            'symbol': t.symbol,
            'entry_time': t.entry_time,
            'exit_time': t.exit_time,
            'direction': t.direction,
            'exit_reason': t.exit_reason,
            'pnl_gross': t.pnl_gross,
            'stt_tax': t.stt_tax,
            'pnl_net': t.pnl_net
        })
        
    df = pd.DataFrame(all_trades)
    
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df = df[df['entry_time'] >= '2024-01-01'].copy()
    
    if df.empty:
        print("No trades found from Jan 2024 onwards!")
        sys.exit(1)
        
    df['YearMonth'] = df['entry_time'].dt.to_period('M')
    
    monthly_agg = df.groupby('YearMonth').agg(
        Gross_PnL=('pnl_gross', 'sum'),
        Net_PnL=('pnl_net', 'sum'),
        STT_Impact=('stt_tax', 'sum'),
        Total_Trades=('symbol', 'count'),
        Winning_Trades=('pnl_net', lambda x: (x > 0).sum())
    )
    monthly_agg['Win_Rate'] = (monthly_agg['Winning_Trades'] / monthly_agg['Total_Trades']) * 100
    
    dir_agg = df.groupby(['YearMonth', 'direction']).agg(
        Gross_PnL=('pnl_gross', 'sum'),
        Net_PnL=('pnl_net', 'sum'),
        STT_Impact=('stt_tax', 'sum'),
        Total_Trades=('symbol', 'count'),
        Winning_Trades=('pnl_net', lambda x: (x > 0).sum())
    )
    dir_agg['Win_Rate'] = (dir_agg['Winning_Trades'] / dir_agg['Total_Trades']) * 100
    
    exit_agg = df.groupby(['YearMonth', 'exit_reason']).agg(
        Gross_PnL=('pnl_gross', 'sum'),
        Net_PnL=('pnl_net', 'sum'),
        STT_Impact=('stt_tax', 'sum'),
        Total_Trades=('symbol', 'count'),
        Winning_Trades=('pnl_net', lambda x: (x > 0).sum())
    )
    exit_agg['Win_Rate'] = (exit_agg['Winning_Trades'] / exit_agg['Total_Trades']) * 100
    
    md = []
    md.append("# Detailed Monthly Backtest Report (Jan 2024 - Present)")
    md.append("Configuration: `R7_COMB_486`")
    md.append("Capital: Rs. 100,000 | Margin: 5x")
    md.append("")
    md.append("## Overall Monthly Breakdown")
    
    def fmt(val):
        return f"Rs. {val:,.2f}"
    
    md.append("| Month | Gross PnL | Net PnL | STT Impact | Win Rate | Total Trades |")
    md.append("|---|---|---|---|---|---|")
    for period, row in monthly_agg.iterrows():
        md.append(f"| {period} | {fmt(row['Gross_PnL'])} | {fmt(row['Net_PnL'])} | {fmt(row['STT_Impact'])} | {row['Win_Rate']:.1f}% | {row['Total_Trades']} |")
        
    md.append("")
    md.append("## Breakdown by Direction (Long vs Short)")
    md.append("| Month | Direction | Gross PnL | Net PnL | STT Impact | Win Rate | Total Trades |")
    md.append("|---|---|---|---|---|---|---|")
    for idx, row in dir_agg.iterrows():
        period, direction = idx
        md.append(f"| {period} | {direction} | {fmt(row['Gross_PnL'])} | {fmt(row['Net_PnL'])} | {fmt(row['STT_Impact'])} | {row['Win_Rate']:.1f}% | {row['Total_Trades']} |")
        
    md.append("")
    md.append("## Breakdown by Exit Reason")
    md.append("| Month | Exit Reason | Gross PnL | Net PnL | STT Impact | Win Rate | Total Trades |")
    md.append("|---|---|---|---|---|---|---|")
    for idx, row in exit_agg.iterrows():
        period, exit_reason = idx
        md.append(f"| {period} | {exit_reason} | {fmt(row['Gross_PnL'])} | {fmt(row['Net_PnL'])} | {fmt(row['STT_Impact'])} | {row['Win_Rate']:.1f}% | {row['Total_Trades']} |")
        
    artifact_path = r"C:\Users\RounakKR\.gemini\antigravity\brain\67da98a9-c536-4e34-b74b-a4d8fdb1fcf9\agent_original_monthly_backtest_report.md"
    os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
    with open(artifact_path, "w") as f:
        f.write("\n".join(md))
        
    print(f"Report saved to {artifact_path}")

if __name__ == '__main__':
    main()
