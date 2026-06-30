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
    short_signals = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
    for sym, df in stock_dfs.items():
        for i in range(10, len(df)):
            sliced = df.iloc[:i+1]
            c_se = evaluator._evaluate_conditions(buy_set_short, StrategyEvaluationContext("buy", sliced, sliced, i+1))
            if c_se and all(r.get("fired") for r in c_se):
                short_signals[sym][i] = True
                
    capital = 100000.0
    buying_power = capital * 5.0
    
    # Best configuration parameters
    filter_type = "vwap"
    thresh = 0.012
    exit_rule = "ema9"
    
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
                trades.append({
                    "symbol": sym, "direction": "SHORT", "entry": ep, "exit": cp, "qty": qty, 
                    "reason": "EOD_TIME", "entry_time": pos["entry_time"], "exit_time": ts
                })
                syms_to_close.append(sym)
                continue
                
            # Stop Loss (0.5%)
            sl_price = ep * 1.005
            if hp >= sl_price:
                exit_price = max(sl_price, op)
                trades.append({
                    "symbol": sym, "direction": "SHORT", "entry": ep, "exit": exit_price, "qty": qty, 
                    "reason": "STOP_LOSS", "entry_time": pos["entry_time"], "exit_time": ts
                })
                syms_to_close.append(sym)
                continue
                
            # Profit Target (2.5%)
            tp_price = ep * 0.975
            if lp <= tp_price:
                exit_price = min(tp_price, op)
                trades.append({
                    "symbol": sym, "direction": "SHORT", "entry": ep, "exit": exit_price, "qty": qty, 
                    "reason": "PARTIAL_PROFIT", "entry_time": pos["entry_time"], "exit_time": ts
                })
                syms_to_close.append(sym)
                continue
                
            # RSI exit (17.0)
            if ts > pos["entry_time"] and idx >= 1:
                prev_rsi = df["rsi_16"].iloc[idx-1]
                if prev_rsi <= 17.0:
                    trades.append({
                        "symbol": sym, "direction": "SHORT", "entry": ep, "exit": op, "qty": qty, 
                        "reason": "RSI_OVERSOLD", "entry_time": pos["entry_time"], "exit_time": ts
                    })
                    syms_to_close.append(sym)
                    continue
                    
            # Savior exits
            if ts > pos["entry_time"] and idx >= 1:
                if exit_rule == "ema9":
                    c1 = df["close"].iloc[idx-1]
                    ema1 = df["ema9"].iloc[idx-1]
                    if c1 > ema1:
                        trades.append({
                            "symbol": sym, "direction": "SHORT", "entry": ep, "exit": op, "qty": qty, 
                            "reason": "SAVIOR_EMA9", "entry_time": pos["entry_time"], "exit_time": ts
                        })
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
                if filter_type == "vwap":
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
                        
    # Write ledger to file
    out_file_path = r"c:\Extra Programs\Files\AlcoSoft_Financial_Services\research\best_short_opt_ledger.txt"
    with open(out_file_path, "w") as out:
        out.write("EXHAUSTIVE TRADE LEDGER (SHORT ENGINE OPTIMIZED)\n")
        out.write("=" * 115 + "\n")
        out.write(f"{'SYMBOL':<10} | {'DIR':<6} | {'ENTRY TIME':<20} | {'EXIT TIME':<20} | {'QTY':<5} | {'ENTRY':<8} | {'EXIT':<8} | {'REASON':<15} | {'NET PNL':<10}\n")
        out.write("-" * 115 + "\n")
        
        gross_pnl = 0.0
        stt_tax = 0.0
        wins = 0
        sl_hits = 0
        
        for t in trades:
            pnl_gross = (t["entry"] - t["exit"]) * t["qty"]
            stt = t["exit"] * t["qty"] * 0.00035
            pnl_net = pnl_gross - stt
            
            gross_pnl += pnl_gross
            stt_tax += stt
            
            if pnl_net > 0:
                wins += 1
            if t["reason"] == "STOP_LOSS":
                sl_hits += 1
                
            entry_time_str = t["entry_time"].strftime("%Y-%m-%d %H:%M")
            exit_time_str = t["exit_time"].strftime("%Y-%m-%d %H:%M")
            out.write(f"{t['symbol']:<10} | {t['direction']:<6} | {entry_time_str:<20} | {exit_time_str:<20} | {t['qty']:<5} | {t['entry']:<8.2f} | {t['exit']:<8.2f} | {t['reason']:<15} | {pnl_net:>8.2f}\n")
            
        net_pnl = gross_pnl - stt_tax
        net_return = net_pnl / capital * 100
        win_rate = (wins / len(trades) * 100) if trades else 0.0
        
        out.write("=" * 115 + "\n")
        out.write(f"Total Trades: {len(trades)}\n")
        out.write(f"Win Rate: {win_rate:.2f}%\n")
        out.write(f"Gross PnL: Rs.{gross_pnl:.2f}\n")
        out.write(f"STT Tax: Rs.{stt_tax:.2f}\n")
        out.write(f"Net PnL: Rs.{net_pnl:.2f}\n")
        out.write(f"Net Return: {net_return:.2f}%\n")
        out.write(f"STOP_LOSS Hits: {sl_hits}\n")
        
    print(f"Written output to {out_file_path}")

if __name__ == "__main__":
    main()
