import os
import sys
import pandas as pd
import json
import logging
from datetime import datetime, time

sys.path.append(r"C:\Extra Programs\Files\AlcoSoft_Financial_Services")

from core.strategy import (
    _build_indicators, 
    StrategyEvaluationContext, 
    StrategySetEvaluator, 
    CONDITION_REGISTRY
)
from core.trading_settings import get as cfg

def main():
    print("Starting Backtest script...")
    hist_dir = r"C:\Extra Programs\Files\AlcoSoft_Financial_Services\data\historical"
    files = [f for f in os.listdir(hist_dir) if f.endswith("_5min.csv")]
    
    # Sort files to ensure deterministic behavior (and limit for testing if needed)
    files.sort()
    
    # Read risk parameters from config
    long_stop_loss_percent = cfg('risk', 'long_stop_loss_percent', 0.010)
    long_profit_target_percent = cfg('risk', 'long_profit_target_percent', 0.025)
    short_stop_loss_percent = cfg('risk', 'short_stop_loss_percent', 0.005)
    short_profit_target_percent = cfg('risk', 'short_profit_target_percent', 0.025)
    
    long_rsi_exit_threshold = cfg('risk', 'long_rsi_exit_threshold', 78.0)
    short_rsi_exit_threshold = cfg('risk', 'short_rsi_exit_threshold', 17.0)
    
    long_partial_profit_fraction = cfg('risk', 'long_partial_profit_fraction', 0.25)
    short_partial_profit_fraction = cfg('risk', 'short_partial_profit_fraction', 1.0)
    
    r7_min_hold_candles = cfg('risk', 'r7_min_hold_candles', 20)
    short_target_gap_threshold = cfg('risk', 'short_target_gap_threshold', -0.015)
    
    max_open_positions = cfg('strategy', 'max_open_positions', 1)
    
    paper_capital = 100000.0
    margin_leverage = 5.0
    buying_power = paper_capital * margin_leverage
    capital_per_trade = buying_power / max_open_positions
    
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
    
    all_trades = []
    
    for f in files:
        symbol = f.replace("_5min.csv", "")
        fpath = os.path.join(hist_dir, f)
        try:
            df = pd.read_csv(fpath)
            if 'timestamp' in df.columns:
                df['_ts'] = pd.to_datetime(df['timestamp'])
            elif '_ts' in df.columns:
                df['_ts'] = pd.to_datetime(df['_ts'])
            else:
                df['_ts'] = pd.to_datetime(df.iloc[:, 0])
                
            df = df.set_index('_ts').sort_index()
            
            # Remove duplicates just in case
            df = df[~df.index.duplicated(keep='first')]
            
            # Build indicators
            df_ind = _build_indicators(df)
            
            # Calculate gap percentage
            df_ind['date'] = df_ind.index.date
            daily_closes = df_ind.groupby('date')['close'].last()
            daily_opens = df_ind.groupby('date')['open'].first()
            
            prev_closes = daily_closes.shift(1)
            df_ind['prev_day_close'] = df_ind['date'].map(prev_closes)
            df_ind['day_open'] = df_ind['date'].map(daily_opens)
            df_ind['gap_pct'] = (df_ind['day_open'] - df_ind['prev_day_close']) / df_ind['prev_day_close']
            
            # Filter from 2024
            df_eval = df_ind.loc['2024-01-01':]
            if df_eval.empty:
                continue
                
            start_idx = df_ind.index.get_loc(df_eval.index[0])
            if isinstance(start_idx, slice):
                start_idx = start_idx.start
            
            print(f"Processing {symbol} from 2024. Candles: {len(df_eval)}")
            
            class Position:
                def __init__(self, direction, entry_price, entry_time, qty):
                    self.direction = direction
                    self.entry_price = entry_price
                    self.entry_time = entry_time
                    self.qty = qty
                    self.initial_qty = qty
                    self.hold_candles = 0
            
            position = None
            
            for i in range(start_idx, len(df_ind)):
                current_dt = df_ind.index[i]
                curr_row = df_ind.iloc[i]
                time_only = current_dt.time()
                
                if position:
                    position.hold_candles += 1
                    
                    exit_reason = None
                    exit_fraction = 1.0
                    pt_price = None
                    sl_price = None
                    
                    # a. Stop Loss
                    if position.direction == "LONG":
                        sl_price = position.entry_price * (1 - long_stop_loss_percent)
                        if curr_row['low'] <= sl_price:
                            exit_reason = "SL"
                    else:
                        sl_price = position.entry_price * (1 + short_stop_loss_percent)
                        if curr_row['high'] >= sl_price:
                            exit_reason = "SL"
                            
                    # b. Partial Profit
                    if not exit_reason and position.qty == position.initial_qty:
                        if position.direction == "LONG":
                            pt_price = position.entry_price * (1 + long_profit_target_percent)
                            if curr_row['high'] >= pt_price:
                                exit_reason = "Partial Profit"
                                exit_fraction = long_partial_profit_fraction
                        else:
                            pt_price = position.entry_price * (1 - short_profit_target_percent)
                            if curr_row['low'] <= pt_price:
                                exit_reason = "Partial Profit"
                                exit_fraction = short_partial_profit_fraction
                                
                    # c. RSI Exit
                    if not exit_reason:
                        if position.direction == "LONG":
                            if curr_row['rsi'] >= long_rsi_exit_threshold:
                                exit_reason = "RSI Exit"
                        else:
                            if curr_row['rsi'] <= short_rsi_exit_threshold:
                                exit_reason = "RSI Exit"
                                
                    # d. EMA50 Dynamic Exit
                    if not exit_reason and position.direction == "LONG":
                        if position.hold_candles >= r7_min_hold_candles:
                            if curr_row['close'] < curr_row['ema50']:
                                exit_reason = "EMA50 Exit"
                                
                    # e. EOD Squareoff
                    if not exit_reason:
                        if time_only >= time(15, 15):
                            exit_reason = "EOD Squareoff"
                            
                    if exit_reason:
                        exit_price = curr_row['close']
                        if exit_reason == "SL":
                            if position.direction == "LONG":
                                exit_price = min(curr_row['open'], sl_price)
                            else:
                                exit_price = max(curr_row['open'], sl_price)
                        elif exit_reason == "Partial Profit":
                            if position.direction == "LONG":
                                exit_price = max(curr_row['open'], pt_price)
                            else:
                                exit_price = min(curr_row['open'], pt_price)
                                
                        qty_to_exit = position.qty * exit_fraction
                        
                        stt = 0
                        if position.direction == "LONG":
                            stt = exit_price * qty_to_exit * 0.001
                        else:
                            stt = exit_price * qty_to_exit * 0.00025
                            
                        gross_pnl = (exit_price - position.entry_price) * qty_to_exit if position.direction == "LONG" else (position.entry_price - exit_price) * qty_to_exit
                        net_pnl = gross_pnl - stt
                        
                        all_trades.append({
                            'symbol': symbol,
                            'direction': position.direction,
                            'month': current_dt.strftime('%Y-%m'),
                            'entry_time': position.entry_time,
                            'exit_time': current_dt,
                            'entry_price': position.entry_price,
                            'exit_price': exit_price,
                            'qty': qty_to_exit,
                            'gross_pnl': gross_pnl,
                            'stt': stt,
                            'net_pnl': net_pnl,
                            'exit_reason': exit_reason,
                            'hold_candles': position.hold_candles
                        })
                        
                        position.qty -= qty_to_exit
                        # Due to floating point math, check a small threshold
                        if position.qty <= 0.01 or exit_fraction == 1.0:
                            position = None

                if not position and time_only < time(15, 0):
                    ctx = StrategyEvaluationContext()
                    ctx.side = "buy" # Evaluating entry sets
                    ctx.indicator_df = df_ind.iloc[max(0, i-50):i+1].copy()
                    
                    result = evaluator.evaluate("buy", ctx)
                    if result:
                        set_name = result['set_name']
                        
                        if set_name == "BUY_R7_VARIANT_D":
                            # Next candle open entry assumption (we just use current close for simplicity, 
                            # or next candle open. The prompt says "enter LONG at next candle open".
                            # Let's enter at next candle open if available.
                            if i + 1 < len(df_ind):
                                next_row = df_ind.iloc[i+1]
                                entry_price = next_row['open']
                                entry_time = df_ind.index[i+1]
                                qty = capital_per_trade / entry_price
                                position = Position("LONG", entry_price, entry_time, qty)
                                
                        elif set_name == "SHORT_STREAK_MOMENTUM_BREAKDOWN":
                            if curr_row['gap_pct'] <= short_target_gap_threshold:
                                if i + 1 < len(df_ind):
                                    next_row = df_ind.iloc[i+1]
                                    entry_price = next_row['open']
                                    entry_time = df_ind.index[i+1]
                                    qty = capital_per_trade / entry_price
                                    position = Position("SHORT", entry_price, entry_time, qty)
                                    
        except Exception as e:
            print(f"Error on {symbol}: {e}")
            
    print(f"Total trades executed: {len(all_trades)}")
    
    # Save trades to a CSV or print summary
    if all_trades:
        df_trades = pd.DataFrame(all_trades)
        df_trades.to_csv("research/r7_backtest_trades.csv", index=False)
        print("Summary saved to research/r7_backtest_trades.csv")

if __name__ == "__main__":
    main()
