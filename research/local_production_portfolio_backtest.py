import os
import sys
import json
import glob
import pandas as pd
import numpy as np
from datetime import datetime

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.strategy import _build_indicators, CONDITION_REGISTRY, StrategySetEvaluator

def run_portfolio_backtest():
    with open(os.path.join(PROJECT_ROOT, "config", "trading_settings.json")) as f:
        settings = json.load(f)

    capital = 100000.0
    margin = 5
    buying_power = capital * margin
    max_pos = settings["strategy"].get("max_open_positions", 2)
    pos_size = buying_power / max_pos
    
    long_sl = settings["risk"]["long_stop_loss_percent"] / 100.0
    long_pt = settings["risk"]["long_profit_target_percent"] / 100.0
    long_rsi_exit = settings["risk"]["long_rsi_exit_threshold"]
    r7_min_hold = settings["risk"].get("r7_min_hold_candles", 20)
    long_partial_frac = settings["risk"].get("long_partial_profit_fraction", 0.25)
    
    short_sl = settings["risk"]["short_stop_loss_percent"] / 100.0
    short_pt = settings["risk"]["short_profit_target_percent"] / 100.0
    short_rsi_exit = settings["risk"]["short_rsi_exit_threshold"]
    short_partial_frac = settings["risk"].get("short_partial_profit_fraction", 0.0) # Assume 0 if not set
    
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

    hist_dir = os.path.join(PROJECT_ROOT, "data", "historical")
    from niftystocks import ns
    midcap50_syms = ns.get_nifty_midcap50()
    files = [os.path.join(hist_dir, s + "_5min.csv") for s in midcap50_syms if os.path.exists(os.path.join(hist_dir, s + "_5min.csv"))]
    
    print(f"Pre-calculating indicators for {len(files)} stocks...")
    
    dfs = {}
    all_timestamps = set()
    
    for file_path in files:
        symbol = os.path.basename(file_path).replace("_5min.csv", "")
        df = pd.read_csv(file_path, parse_dates=["timestamp"])
        df = df[df["timestamp"] >= "2024-01-01"].copy()
        if df.empty: continue
        
        df.rename(columns={"timestamp": "bucket"}, inplace=True)
        df = _build_indicators(df)
        df.set_index("bucket", inplace=True)
        dfs[symbol] = df
        all_timestamps.update(df.index.tolist())
        
    sorted_timestamps = sorted(list(all_timestamps))
    print(f"Total unique 5-min intervals: {len(sorted_timestamps)}")
    
    open_positions = []
    completed_trades = []
    
    class MockContext:
        def __init__(self, side, ind_df):
            self.side = side
            self.indicator_df = ind_df
            self.ws_count = 100
        def get_lookback(self): return 10
        
    print("Running chronological portfolio backtest...")
    
    for idx, ts in enumerate(sorted_timestamps):
        if idx % 5000 == 0:
            print(f"Processed {idx}/{len(sorted_timestamps)} timestamps...")
            
        # 1. Evaluate Exits for currently open positions
        surviving_positions = []
        for pos in open_positions:
            symbol = pos["symbol"]
            df = dfs.get(symbol)
            if df is None or ts not in df.index:
                surviving_positions.append(pos)
                continue
                
            row = df.loc[ts]
            close_p = row["close"]
            rsi_v = row["rsi"]
            ema50_v = row["ema50"]
            is_eod = (ts.hour == 15 and ts.minute >= 15)
            
            pos["hold_candles"] += 1
            entry_price = pos["entry_price"]
            exit_reason = None
            
            if pos["type"] == "LONG":
                if is_eod:
                    exit_reason = "EOD"
                elif close_p <= entry_price * (1 - long_sl):
                    exit_reason = "STOP_LOSS"
                elif not pos["partial_taken"] and close_p >= entry_price * (1 + long_pt):
                    pos["partial_taken"] = True
                    realized_pnl = (close_p - entry_price) * (pos["qty"] * long_partial_frac)
                    stt = close_p * (pos["qty"] * long_partial_frac) * 0.001
                    completed_trades.append({
                        "symbol": symbol, "month": ts.strftime("%Y-%m"), "type": "LONG",
                        "pnl": realized_pnl, "stt": stt, "exit_reason": "PARTIAL_PROFIT"
                    })
                    pos["qty"] *= (1 - long_partial_frac)
                elif rsi_v >= long_rsi_exit:
                    exit_reason = "RSI_EXIT"
                elif pos["hold_candles"] >= r7_min_hold and close_p < ema50_v:
                    exit_reason = "EMA50_EXIT"
            else: # SHORT
                if is_eod:
                    exit_reason = "EOD"
                elif close_p >= entry_price * (1 + short_sl):
                    exit_reason = "STOP_LOSS"
                elif close_p <= entry_price * (1 - short_pt):
                    exit_reason = "PROFIT_TARGET"
                elif rsi_v <= short_rsi_exit:
                    exit_reason = "RSI_EXIT"
                    
            if exit_reason:
                if pos["type"] == "LONG":
                    pnl = (close_p - entry_price) * pos["qty"]
                    stt = close_p * pos["qty"] * 0.001
                else:
                    pnl = (entry_price - close_p) * pos["qty"]
                    stt = entry_price * pos["qty"] * 0.00025
                    
                completed_trades.append({
                    "symbol": symbol, "month": ts.strftime("%Y-%m"), "type": pos["type"],
                    "pnl": pnl, "stt": stt, "exit_reason": exit_reason
                })
            else:
                surviving_positions.append(pos)
                
        open_positions = surviving_positions
        
        # 2. Evaluate Entries if we have capacity
        if len(open_positions) < max_pos and ts.hour < 15:
            # We need the past 20 candles for context
            # To be efficient, we only evaluate if there is a realistic chance
            # But here we actually use the production evaluator!
            
            # Simple heuristic: Only evaluate stocks that actually have data at this ts
            available_symbols = [sym for sym, d in dfs.items() if ts in d.index]
            for symbol in available_symbols:
                if len(open_positions) >= max_pos: break
                
                # Don't open if already holding
                if any(p["symbol"] == symbol for p in open_positions): continue
                
                df = dfs[symbol]
                idx_num = df.index.get_loc(ts)
                if isinstance(idx_num, slice) or isinstance(idx_num, np.ndarray):
                    idx_num = df.index.get_indexer([ts])[0]
                
                if idx_num < 20: continue
                
                trailing_df = df.iloc[idx_num-20 : idx_num+1]
                
                # Check Long
                ctx = MockContext("buy", trailing_df)
                if evaluator.evaluate("buy", ctx):
                    open_positions.append({
                        "symbol": symbol, "type": "LONG",
                        "entry_price": trailing_df["close"].iloc[-1],
                        "qty": pos_size / trailing_df["close"].iloc[-1],
                        "hold_candles": 0, "partial_taken": False
                    })
                    continue
                
                # Check Short
                ctx_sell = MockContext("sell", trailing_df)
                if evaluator.evaluate("sell", ctx_sell):
                    open_positions.append({
                        "symbol": symbol, "type": "SHORT",
                        "entry_price": trailing_df["close"].iloc[-1],
                        "qty": pos_size / trailing_df["close"].iloc[-1],
                        "hold_candles": 0, "partial_taken": False
                    })

    # Close any remaining at the end
    for pos in open_positions:
        symbol = pos["symbol"]
        df = dfs[symbol]
        last_ts = df.index[-1]
        close_p = df.iloc[-1]["close"]
        if pos["type"] == "LONG":
            pnl = (close_p - pos["entry_price"]) * pos["qty"]
            stt = close_p * pos["qty"] * 0.001
        else:
            pnl = (pos["entry_price"] - close_p) * pos["qty"]
            stt = pos["entry_price"] * pos["qty"] * 0.00025
        completed_trades.append({
            "symbol": symbol, "month": last_ts.strftime("%Y-%m"), "type": pos["type"],
            "pnl": pnl, "stt": stt, "exit_reason": "END_OF_TEST"
        })
        
    print(f"Test completed! Total trades generated: {len(completed_trades)}")
    
    # Generate Report
    df_trades = pd.DataFrame(completed_trades)
    if not df_trades.empty:
        summary = ["# STRICT Local System Portfolio Backtest (Jan 2024 - Present)\n"]
        summary.append(f"**Total Capital:** Rs. {capital}")
        summary.append(f"**Max Positions:** {max_pos}")
        summary.append(f"**Total Trades:** {len(df_trades)}\n")
        
        total_gross = df_trades["pnl"].sum()
        total_stt = df_trades["stt"].sum()
        total_net = total_gross - total_stt
        summary.append(f"**Total Net Return:** {(total_net / capital) * 100:.2f}%\n")
        
        summary.append("## Monthly Breakdown\n")
        summary.append("| Month | Total Trades | Win Rate | Gross PnL | STT | Net PnL |")
        summary.append("|-------|--------------|----------|-----------|-----|---------|")
        
        for month, group in df_trades.groupby("month"):
            gross = group["pnl"].sum()
            stt = group["stt"].sum()
            net = gross - stt
            wins = (group["pnl"] > 0).sum()
            total = len(group)
            wr = (wins / total * 100) if total > 0 else 0
            summary.append(f"| {month} | {total} | {wr:.1f}% | {gross:.2f} | {stt:.2f} | {net:.2f} |")
            
        summary.append("\n## Long vs Short Breakdown\n")
        summary.append("| Side | Trades | Net PnL | Win Rate |")
        summary.append("|------|--------|---------|----------|")
        for side, group in df_trades.groupby("type"):
            net = group["pnl"].sum() - group["stt"].sum()
            wr = (group["pnl"] > 0).sum() / len(group) * 100
            summary.append(f"| {side} | {len(group)} | {net:.2f} | {wr:.1f}% |")
            
        summary.append("\n## Exit Reason Breakdown\n")
        summary.append("| Reason | Trades | PnL |")
        summary.append("|--------|--------|-----|")
        for reason, group in df_trades.groupby("exit_reason"):
            summary.append(f"| {reason} | {len(group)} | {group['pnl'].sum():.2f} |")
            
        report_path = os.path.join(PROJECT_ROOT, "brain", "r7_strict_portfolio_report.md")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            f.write("\n".join(summary))
        print(f"Report written to {report_path}")

if __name__ == "__main__":
    run_portfolio_backtest()
