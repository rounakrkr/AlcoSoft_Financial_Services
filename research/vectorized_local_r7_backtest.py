import os
import sys
import json
import glob
import pandas as pd
import numpy as np

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.strategy import _build_indicators

def backtest_vectorized():
    # Load settings
    with open(os.path.join(PROJECT_ROOT, "config", "trading_settings.json")) as f:
        settings = json.load(f)
        
    capital = 100000
    margin = 5
    buying_power = capital * margin
    max_pos = settings["strategy"].get("max_open_positions", 2)
    pos_size = buying_power / max_pos
    
    long_sl = settings["risk"]["long_stop_loss_percent"] / 100.0
    long_pt = settings["risk"]["long_profit_target_percent"] / 100.0
    long_rsi_exit = settings["risk"]["long_rsi_exit_threshold"]
    r7_min_hold = settings["risk"].get("r7_min_hold_candles", 20)
    long_partial_frac = settings["risk"].get("long_partial_profit_fraction", 0.25)
    
    hist_dir = os.path.join(PROJECT_ROOT, "data", "historical")
    files = glob.glob(os.path.join(hist_dir, "*_5min.csv"))
    
    all_trades = []
    
    print(f"Processing {len(files)} stocks with purely vectorized logic...")
    
    for file_path in files:
        symbol = os.path.basename(file_path).replace("_5min.csv", "")
        df = pd.read_csv(file_path, parse_dates=["timestamp"])
        df = df[df["timestamp"] >= "2024-01-01"].copy()
        if df.empty:
            continue
            
        df.rename(columns={"timestamp": "bucket"}, inplace=True)
        df = _build_indicators(df)
        
        # BUY_R7_VARIANT_D conditions
        # 1. price_below_vwap: close < vwap
        # 2. ema9_below_ema21: ema9 < ema21
        # 3. green_reversal: close > open
        # 4. close_below_ema9: close < ema9
        # 5. rsi_recovering: rsi < 30.0
        
        long_cond = (
            (df['close'] < df['vwap']) &
            (df['ema9'] < df['ema21']) &
            (df['close'] > df['open']) &
            (df['close'] < df['ema9']) &
            (df['rsi'] < 30.0)
        )
        
        # No entry zone
        is_no_entry = df['bucket'].dt.hour >= 15
        long_cond = long_cond & ~is_no_entry
        
        # Short conditions: SHORT_STREAK_MOMENTUM_BREAKDOWN
        # close_1_below_vwap_0 (previous close < current vwap)
        # ema20_1_below_vwap_0
        # rsi_1_below_39
        # close_0_below_period_min_10
        # close_0_not_reversing (close <= open)
        # close_0_near_vwap (within 0.5% of VWAP)
        
        short_cond = (
            (df['close'].shift(1) < df['vwap']) &
            (df['ema20'].shift(1) < df['vwap']) &
            (df['rsi'].shift(1) < 39.0) &
            (df['close'] < df['close'].rolling(10).min().shift(1)) &
            (df['close'] <= df['open']) &
            (df['close'] >= df['vwap'] * 0.995) & (df['close'] <= df['vwap'] * 1.005)
        )
        short_cond = short_cond & ~is_no_entry

        # We will loop through the dataframe. A python loop over pre-evaluated booleans is FAST.
        df['long_entry'] = long_cond
        df['short_entry'] = short_cond
        df['is_eod'] = (df['bucket'].dt.hour == 15) & (df['bucket'].dt.minute >= 15)
        
        position = None
        
        # Iterating a zip of tuples is the fastest python iteration method.
        for ts, close_p, rsi_v, ema50_v, long_e, short_e, eod in zip(
            df['bucket'], df['close'], df['rsi'], df['ema50'], 
            df['long_entry'], df['short_entry'], df['is_eod']
        ):
            if position:
                position['hold_candles'] += 1
                entry_price = position['entry_price']
                exit_reason = None
                
                if position['type'] == 'LONG':
                    if eod:
                        exit_reason = "EOD"
                    elif close_p <= entry_price * (1 - long_sl):
                        exit_reason = "STOP_LOSS"
                    elif not position['partial_taken'] and close_p >= entry_price * (1 + long_pt):
                        position['partial_taken'] = True
                        realized_pnl = (close_p - entry_price) * (position['qty'] * long_partial_frac)
                        stt = close_p * (position['qty'] * long_partial_frac) * 0.001
                        all_trades.append({
                            "symbol": symbol,
                            "month": ts.strftime("%Y-%m"),
                            "type": "LONG",
                            "pnl": realized_pnl,
                            "stt": stt,
                            "exit_reason": "PARTIAL_PROFIT"
                        })
                        position['qty'] *= (1 - long_partial_frac)
                    elif rsi_v >= long_rsi_exit:
                        exit_reason = "RSI_EXIT"
                    elif position['hold_candles'] >= r7_min_hold and close_p < ema50_v:
                        exit_reason = "EMA50_EXIT"
                        
                    if exit_reason:
                        pnl = (close_p - entry_price) * position['qty']
                        stt = close_p * position['qty'] * 0.001
                        all_trades.append({
                            "symbol": symbol,
                            "month": ts.strftime("%Y-%m"),
                            "type": "LONG",
                            "pnl": pnl,
                            "stt": stt,
                            "exit_reason": exit_reason
                        })
                        position = None
                        
                else: # SHORT
                    # Note: we need to use actual short exit logic from settings.
                    # but for basic validation we'll use standard SL/PT/EOD
                    short_pt = settings["risk"]["short_profit_target_percent"] / 100.0
                    short_sl = settings["risk"]["short_stop_loss_percent"] / 100.0
                    if eod:
                        exit_reason = "EOD"
                    elif close_p >= entry_price * (1 + short_sl):
                        exit_reason = "STOP_LOSS"
                    elif not position['partial_taken'] and close_p <= entry_price * (1 - short_pt):
                        position['partial_taken'] = True
                        exit_reason = "PROFIT_TARGET" # treating full exit for short
                    elif rsi_v <= settings["risk"]["short_rsi_exit_threshold"]:
                        exit_reason = "RSI_EXIT"
                        
                    if exit_reason:
                        pnl = (entry_price - close_p) * position['qty']
                        stt = entry_price * position['qty'] * 0.00025
                        all_trades.append({
                            "symbol": symbol,
                            "month": ts.strftime("%Y-%m"),
                            "type": "SHORT",
                            "pnl": pnl,
                            "stt": stt,
                            "exit_reason": exit_reason
                        })
                        position = None
            
            if not position:
                if long_e:
                    position = {
                        "type": "LONG",
                        "entry_price": close_p,
                        "qty": pos_size / close_p,
                        "hold_candles": 0,
                        "partial_taken": False
                    }
                elif short_e:
                    position = {
                        "type": "SHORT",
                        "entry_price": close_p,
                        "qty": pos_size / close_p,
                        "hold_candles": 0,
                        "partial_taken": False
                    }
                    
    print(f"Total trades: {len(all_trades)}")
    df_trades = pd.DataFrame(all_trades)
    if not df_trades.empty:
        df_trades.to_csv(os.path.join(PROJECT_ROOT, "research", "fast_r7_trades.csv"), index=False)
        
        # Generate Markdown Summary
        summary = ["# R7_COMB_486 Local System Backtest (Vectorized)\n"]
        summary.append(f"**Total Net Return:** {((df_trades['pnl'].sum() - df_trades['stt'].sum()) / capital) * 100:.2f}%\n")
        
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
            
        summary.append("\n## Exit Reason Breakdown\n")
        summary.append("| Reason | Trades | PnL |")
        summary.append("|--------|--------|-----|")
        for reason, group in df_trades.groupby("exit_reason"):
            summary.append(f"| {reason} | {len(group)} | {group['pnl'].sum():.2f} |")
            
        with open(os.path.join(PROJECT_ROOT, "brain", "r7_local_system_report.md"), "w") as f:
            f.write("\n".join(summary))
        print("Report written to brain/r7_local_system_report.md")

if __name__ == "__main__":
    backtest_vectorized()
