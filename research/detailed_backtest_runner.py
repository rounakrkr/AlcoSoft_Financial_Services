import sys
import os
import pandas as pd
import datetime

sys.path.insert(0, r"C:\Extra Programs\Files\AlcoSoft_Financial_Services\research")
import sweep_midcap50_engine as engine

def main():
    stock_dfs = engine.load_cache()
    if not stock_dfs:
        print("Failed to load cache")
        return

    engine.IndicatorPreprocessor.enrich_data(stock_dfs)
    regime_analyzer = engine.MarketRegimeAnalyzer(stock_dfs)
    
    signal_gen = engine.SignalGenerator(stock_dfs)
    signal_gen.precompute_signals("VARIANT_D")
    
    short_ts_with_signals = set()
    short_signals_by_ts = {}
    for sym, sigs in signal_gen.short_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                short_ts_with_signals.add(ts)
                short_signals_by_ts.setdefault(ts, []).append(sym)
                
    long_ts_with_signals = set()
    long_signals_by_ts = {}
    for sym, sigs in signal_gen.long_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                long_ts_with_signals.add(ts)
                long_signals_by_ts.setdefault(ts, []).append(sym)

    sys_cfg = engine.SystemConfig(
        capital=100000.0,
        margin=5.0,
        max_open_positions=1
    )
    long_cfg = engine.LongEngineConfig(
        market_gap_threshold=0.008,
        market_breadth_requirement=0.35,
        exclude_gap_threshold=-0.008,
        rsi_exit_threshold=72.0,
        profit_target_pct=0.015,
        partial_booking_fraction=0.25,
        stop_loss_pct=0.008,
        dyn_exit_type="EMA21",
        dyn_exit_hold_time=0,
        reentry_cap=1,
        min_hold_time=15
    )
    short_cfg = engine.ShortEngineConfig(
        market_gap_threshold=-0.006,
        market_breadth_requirement=0.40,
        target_gap_threshold=-0.015,
        rsi_exit_threshold=30.0,
        profit_target_pct=0.025,
        partial_booking_fraction=1.00,
        stop_loss_pct=0.005,
        disable_shorts=False,
        savior_exit=False
    )

    long_trades, short_trades, _ = engine.run_backtest(
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

    all_trades = long_trades + short_trades
    all_trades.sort(key=lambda x: x.entry_time)

    # Filtering from Jan 2024
    filtered_trades = [t for t in all_trades if t.entry_time.year >= 2024]

    # Month by month aggregation
    results_by_month = {}
    for t in filtered_trades:
        m = t.entry_time.strftime("%Y-%m")
        if m not in results_by_month:
            results_by_month[m] = {
                "pnl_gross": 0.0,
                "stt": 0.0,
                "pnl_net": 0.0,
                "wins": 0,
                "total": 0,
                "long_pnl": 0.0,
                "short_pnl": 0.0,
                "reasons": {}
            }
        r = results_by_month[m]
        r["total"] += 1
        r["pnl_gross"] += t.pnl_gross
        r["stt"] += t.stt_tax
        r["pnl_net"] += t.pnl_net
        if t.pnl_net > 0:
            r["wins"] += 1
            
        if t.direction == "LONG":
            r["long_pnl"] += t.pnl_net
        else:
            r["short_pnl"] += t.pnl_net
            
        r["reasons"][t.exit_reason] = r["reasons"].get(t.exit_reason, 0.0) + t.pnl_net

    report_lines = []
    report_lines.append("# Detailed Monthly Backtest Report (Jan 2024 - Present)")
    report_lines.append("")
    report_lines.append("## Setup: C729_579 Configuration")
    report_lines.append("- Capital: Rs. 100,000 + 5x Intraday Margin (Total: 500,000)")
    report_lines.append("- Universe: Nifty Midcap 50")
    report_lines.append("- Long Engine: `VARIANT_D` (EMA21 Dynamic Exit, Hold >= 15m)")
    report_lines.append("- Short Engine: `SHORT_STREAK_MOMENTUM_BREAKDOWN` (RSI Exit <= 30.0)")
    report_lines.append("")
    report_lines.append("---")
    report_lines.append("")

    total_pnl = 0.0
    for m in sorted(results_by_month.keys()):
        r = results_by_month[m]
        total_pnl += r["pnl_net"]
        wr = (r["wins"] / r["total"]) * 100 if r["total"] > 0 else 0
        report_lines.append(f"### Month: {m}")
        report_lines.append(f"- **Total Trades:** {r['total']}")
        report_lines.append(f"- **Win Rate:** {wr:.2f}%")
        report_lines.append(f"- **Gross PnL:** Rs. {r['pnl_gross']:,.2f}")
        report_lines.append(f"- **STT Impact:** Rs. {r['stt']:,.2f}")
        report_lines.append(f"- **Net PnL:** Rs. {r['pnl_net']:,.2f}")
        report_lines.append("")
        report_lines.append("#### PnL Breakdown by Direction:")
        report_lines.append(f"- Long Trades PnL: Rs. {r['long_pnl']:,.2f}")
        report_lines.append(f"- Short Trades PnL: Rs. {r['short_pnl']:,.2f}")
        report_lines.append("")
        report_lines.append("#### PnL Breakdown by Exit Reason:")
        for reason, p in sorted(r["reasons"].items(), key=lambda x: x[1], reverse=True):
            report_lines.append(f"- `{reason}`: Rs. {p:,.2f}")
        report_lines.append("")
        report_lines.append("---")
        report_lines.append("")
        
    report_lines.append(f"### **Overall Net PnL (Since Jan 2024): Rs. {total_pnl:,.2f}**")
    
    report_text = "\n".join(report_lines)
    
    with open(r"C:\Extra Programs\Files\AlcoSoft_Financial_Services\research\detailed_monthly_backtest_report_data.md", "w") as f:
        f.write(report_text)
    print("Report generated and saved to detailed_monthly_backtest_report_data.md")

if __name__ == '__main__':
    main()
