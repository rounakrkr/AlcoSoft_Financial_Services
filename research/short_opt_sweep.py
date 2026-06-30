import sys
import os
import pickle
import pandas as pd
import numpy as np
import ta
import warnings
warnings.filterwarnings("ignore")

# Ensure we can load local modules
sys.path.insert(0, r"c:\Extra Programs\Files\AlcoSoft_Financial_Services")

from core.strategy_sets import load_strategy_sets
from core.strategy import StrategySetEvaluator, StrategyEvaluationContext, CONDITION_REGISTRY

def main():
    cache_path = r"c:\Extra Programs\Files\AlcoSoft_Financial_Services\research\data_cache.pkl"
    with open(cache_path, "rb") as f:
        data = pickle.load(f)
    stock_dfs = data["stock_dfs"]
    
    # Calculate indicators
    print("Enriching technical indicators...")
    for sym, df in stock_dfs.items():
        df["rsi_14"] = ta.momentum.rsi(df["close"], window=14).fillna(50.0)
        df["rsi_16"] = ta.momentum.rsi(df["close"], window=16).fillna(50.0)
        df["ema9"] = ta.trend.ema_indicator(df["close"], window=9).bfill()
        df["ema21"] = ta.trend.ema_indicator(df["close"], window=21).bfill()
        if "vwap" not in df.columns:
            typical_price = (df["high"] + df["low"] + df["close"]) / 3
            df["vwap"] = (typical_price * df["volume"]).groupby(df.index.date).cumsum() / df["volume"].groupby(df.index.date).cumsum()
            df["vwap"] = df["vwap"].bfill()
            
    # Build timeline
    timeline_set = set()
    for sym, df in stock_dfs.items():
        timeline_set.update(df.index)
    timeline = sorted(list(timeline_set))
    stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
    
    # Get Bear Days
    all_daily_gaps = {}
    for sym, df in stock_dfs.items():
        daily_first = df.groupby(df.index.date).first()
        daily_last = df.groupby(df.index.date).last()
        first_opens = daily_first["open"]
        last_closes = daily_last["close"].shift(1)
        gaps = (first_opens - last_closes) / last_closes
        for d, gap_val in gaps.items():
            if pd.notna(gap_val):
                all_daily_gaps[(d, sym)] = float(gap_val)
                
    trading_dates = sorted(list(set(d for d, s in all_daily_gaps.keys())))
    bear_days = set()
    for curr_d in trading_dates:
        gaps = [g for (d, s), g in all_daily_gaps.items() if d == curr_d]
        if gaps:
            qualified = sum(1 for g in gaps if g <= -0.006)
            if qualified / len(gaps) >= 0.40:
                bear_days.add(curr_d)
                
    # Evaluator
    config_obj = load_strategy_sets()
    buy_set_short = next((s for s in config_obj.buy_sets if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN"), None)
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
    
    # Precompute signals
    print("Precomputing Short breakdown signals...")
    short_signals = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
    for sym, df in stock_dfs.items():
        for i in range(10, len(df)):
            sliced = df.iloc[:i+1]
            c_se = evaluator._evaluate_conditions(buy_set_short, StrategyEvaluationContext("buy", sliced, sliced, i+1))
            if c_se and all(r.get("fired") for r in c_se):
                short_signals[sym][i] = True
                
    capital = 100000.0
    buying_power = capital * 5.0
    
    # Define sweep values
    entry_filter_types = ["none", "ema21", "ema9", "vwap"]
    thresholds = [0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014, 0.015, 0.020]
    
    savior_exits = ["none", "ema21", "ema9", "high2", "high1"]
    
    results = []
    
    # Build list of combinations
    combinations = []
    # 1. No entry filter combination
    for exit_rule in savior_exits:
        combinations.append(("none", 0.0, exit_rule))
        
    # 2. Entry filter combinations
    for filter_type in ["ema21", "ema9", "vwap"]:
        for thresh in thresholds:
            for exit_rule in savior_exits:
                combinations.append((filter_type, thresh, exit_rule))
                
    print(f"Sweeping {len(combinations)} parameter combinations...")
    
    for filter_type, thresh, exit_rule in combinations:
        trades = []
        open_positions = {}
        
        for ts in timeline:
            date_only = ts.date()
            is_bear_day = date_only in bear_days
            
            # Manage positions
            syms_to_close = []
            for sym, pos in open_positions.items():
                if ts not in stock_ts_map.get(sym, {}):
                    continue
                idx = stock_ts_map[sym][ts]
                df = stock_dfs[sym]
                cc = df.iloc[idx]
                cp, lp, op, hp = float(cc["close"]), float(cc["low"]), float(cc["open"]), float(cc["high"])
                ep = pos["entry_price"]
                qty = pos["quantity"]
                
                # EOD Exit
                if ts.hour == 15 and ts.minute >= 15:
                    trades.append({"symbol": sym, "direction": "SHORT", "entry": ep, "exit": cp, "qty": qty, "reason": "EOD"})
                    syms_to_close.append(sym)
                    continue
                    
                # Stop Loss (0.5%)
                sl_price = ep * 1.005
                if hp >= sl_price:
                    exit_price = max(sl_price, op)
                    trades.append({"symbol": sym, "direction": "SHORT", "entry": ep, "exit": exit_price, "qty": qty, "reason": "STOP_LOSS"})
                    syms_to_close.append(sym)
                    continue
                    
                # Profit Target (2.5%)
                tp_price = ep * 0.975
                if lp <= tp_price:
                    exit_price = min(tp_price, op)
                    trades.append({"symbol": sym, "direction": "SHORT", "entry": ep, "exit": exit_price, "qty": qty, "reason": "PROFIT_TARGET"})
                    syms_to_close.append(sym)
                    continue
                    
                # RSI exit (17.0)
                if ts > pos["entry_time"] and idx >= 1:
                    prev_rsi = df["rsi_16"].iloc[idx-1]
                    if prev_rsi <= 17.0:
                        trades.append({"symbol": sym, "direction": "SHORT", "entry": ep, "exit": op, "qty": qty, "reason": "RSI_OVERSOLD"})
                        syms_to_close.append(sym)
                        continue
                        
                # Savior exits
                if ts > pos["entry_time"] and idx >= 1:
                    if exit_rule == "ema21":
                        c1 = df["close"].iloc[idx-1]
                        ema1 = df["ema21"].iloc[idx-1]
                        if c1 > ema1:
                            trades.append({"symbol": sym, "direction": "SHORT", "entry": ep, "exit": op, "qty": qty, "reason": "SAVIOR_EMA21"})
                            syms_to_close.append(sym)
                            continue
                    elif exit_rule == "ema9":
                        c1 = df["close"].iloc[idx-1]
                        ema1 = df["ema9"].iloc[idx-1]
                        if c1 > ema1:
                            trades.append({"symbol": sym, "direction": "SHORT", "entry": ep, "exit": op, "qty": qty, "reason": "SAVIOR_EMA9"})
                            syms_to_close.append(sym)
                            continue
                    elif exit_rule == "high2":
                        c1 = df["close"].iloc[idx-1]
                        h2 = df["high"].iloc[idx-2] if idx >= 2 else float('inf')
                        if c1 > h2:
                            trades.append({"symbol": sym, "direction": "SHORT", "entry": ep, "exit": op, "qty": qty, "reason": "SAVIOR_HIGH2"})
                            syms_to_close.append(sym)
                            continue
                    elif exit_rule == "high1":
                        c1 = df["close"].iloc[idx-1]
                        h1 = df["high"].iloc[idx-1]
                        if c1 > h1:
                            trades.append({"symbol": sym, "direction": "SHORT", "entry": ep, "exit": op, "qty": qty, "reason": "SAVIOR_HIGH1"})
                            syms_to_close.append(sym)
                            continue
                            
            for sym in syms_to_close:
                if sym in open_positions:
                    del open_positions[sym]
                    
            # Scan for entries
            if is_bear_day and ts.hour < 15 and len(open_positions) < 1:
                for sym in stock_dfs:
                    if len(open_positions) >= 1:
                        break
                    if sym in open_positions:
                        continue
                    gap_pct = (all_daily_gaps.get((date_only, sym), 0.0))
                    if gap_pct > -0.008:
                        continue
                    idx = stock_ts_map[sym].get(ts, -1)
                    if idx == -1 or not short_signals[sym][idx]:
                        continue
                        
                    df = stock_dfs[sym]
                    
                    # Block entry if the short cover strategy triggers (close > previous high)
                    if idx >= 1 and df["close"].iloc[idx] > df["high"].iloc[idx-1]:
                        continue
                        
                    # Apply Entry Filters
                    close = df["close"].iloc[idx]
                    if filter_type == "ema21":
                        ema21 = df["ema21"].iloc[idx]
                        if close < ema21 * (1.0 - thresh):
                            continue
                    elif filter_type == "ema9":
                        ema9 = df["ema9"].iloc[idx]
                        if close < ema9 * (1.0 - thresh):
                            continue
                    elif filter_type == "vwap":
                        vwap = df["vwap"].iloc[idx]
                        if close < vwap * (1.0 - thresh):
                            continue
                            
                    if idx + 1 < len(df):
                        entry_price = float(df.iloc[idx+1]["open"])
                        entry_time = df.index[idx+1]
                        qty = int(buying_power // entry_price)
                        if qty > 0:
                            open_positions[sym] = {
                                "entry_time": entry_time,
                                "entry_price": entry_price,
                                "quantity": qty
                            }
                            
        # Calculate Stats
        gross_pnl = 0.0
        stt_tax = 0.0
        wins = 0
        sl_hits = 0
        for t in trades:
            pnl = (t["entry"] - t["exit"]) * t["qty"]
            gross_pnl += pnl
            stt = t["entry"] * t["qty"] * 0.00035
            stt_tax += stt
            net_pnl = pnl - stt
            if net_pnl > 0:
                wins += 1
            if t["reason"] == "STOP_LOSS":
                sl_hits += 1
                
        net_return = (gross_pnl - stt_tax) / capital * 100
        win_rate = (wins / len(trades) * 100) if trades else 0.0
        
        results.append({
            "filter_type": filter_type,
            "threshold": thresh,
            "exit_rule": exit_rule,
            "trades": len(trades),
            "win_rate": win_rate,
            "gross_pnl": gross_pnl,
            "stt": stt_tax,
            "net_return": net_return,
            "sl_hits": sl_hits
        })
        
    df_results = pd.DataFrame(results)
    
    # Save all results to CSV
    csv_path = r"c:\Extra Programs\Files\AlcoSoft_Financial_Services\research\short_opt_sweep_results.csv"
    df_results.to_csv(csv_path, index=False)
    print(f"Saved all results to {csv_path}")
    
    # Filter configurations that satisfy the constraint: sl_hits < 11
    constrained_df = df_results[df_results["sl_hits"] < 11]
    
    # Sort by Net Return descending
    sorted_df = constrained_df.sort_values(by="net_return", ascending=False)
    
    print("\nTop 20 Configurations satisfying SL hits < 11 (Sorted by Net Return %):")
    print(f"{'Filter Type':<12} | {'Threshold':<9} | {'Exit Rule':<10} | {'Trades':<6} | {'Win Rate':<8} | {'Net Return':<10} | {'SL Hits':<7}")
    print("-" * 75)
    for idx, row in sorted_df.head(20).iterrows():
        print(f"{row['filter_type']:<12} | {row['threshold']:<9.3f} | {row['exit_rule']:<10} | {int(row['trades']):<6d} | {row['win_rate']:7.2f}% | {row['net_return']:9.2f}% | {int(row['sl_hits']):<7d}")

if __name__ == "__main__":
    main()
