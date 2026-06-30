import os
import sys
import pandas as pd
import json
import logging
from datetime import datetime, time
from collections import defaultdict
import concurrent.futures

sys.path.append(r"C:\Extra Programs\Files\AlcoSoft_Financial_Services")

from core.strategy import (
    _build_indicators, 
    StrategyEvaluationContext, 
    StrategySetEvaluator, 
    CONDITION_REGISTRY
)
from core.trading_settings import get as cfg

def process_symbol(f, hist_dir, capital_per_trade, evaluator, params):
    symbol = f.replace("_5min.csv", "")
    fpath = os.path.join(hist_dir, f)
    trades = []
    
    try:
        df = pd.read_csv(fpath)
        if 'timestamp' in df.columns:
            df['_ts'] = pd.to_datetime(df['timestamp'])
        elif '_ts' in df.columns:
            df['_ts'] = pd.to_datetime(df['_ts'])
        else:
            df['_ts'] = pd.to_datetime(df.iloc[:, 0])
            
        df = df.set_index('_ts').sort_index()
        df = df[~df.index.duplicated(keep='first')]
        
        df_ind = _build_indicators(df)
        
        df_ind['date'] = df_ind.index.date
        daily_closes = df_ind.groupby('date')['close'].last()
        daily_opens = df_ind.groupby('date')['open'].first()
        
        prev_closes = daily_closes.shift(1)
        df_ind['prev_day_close'] = df_ind['date'].map(prev_closes)
        df_ind['day_open'] = df_ind['date'].map(daily_opens)
        df_ind['gap_pct'] = (df_ind['day_open'] - df_ind['prev_day_close']) / df_ind['prev_day_close']
        
        df_eval = df_ind.loc['2024-01-01':]
        if df_eval.empty:
            return trades
            
        start_idx = df_ind.index.get_loc(df_eval.index[0])
        if isinstance(start_idx, slice):
            start_idx = start_idx.start
        
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
                
                if position.direction == "LONG":
                    sl_price = position.entry_price * (1 - params['long_stop_loss_percent'])
                    if curr_row['low'] <= sl_price:
                        exit_reason = "SL"
                else:
                    sl_price = position.entry_price * (1 + params['short_stop_loss_percent'])
                    if curr_row['high'] >= sl_price:
                        exit_reason = "SL"
                        
                if not exit_reason and position.qty == position.initial_qty:
                    if position.direction == "LONG":
                        pt_price = position.entry_price * (1 + params['long_profit_target_percent'])
                        if curr_row['high'] >= pt_price:
                            exit_reason = "Partial Profit"
                            exit_fraction = params['long_partial_profit_fraction']
                    else:
                        pt_price = position.entry_price * (1 - params['short_profit_target_percent'])
                        if curr_row['low'] <= pt_price:
                            exit_reason = "Partial Profit"
                            exit_fraction = params['short_partial_profit_fraction']
                            
                if not exit_reason:
                    if position.direction == "LONG":
                        if curr_row['rsi'] >= params['long_rsi_exit_threshold']:
                            exit_reason = "RSI Exit"
                    else:
                        if curr_row['rsi'] <= params['short_rsi_exit_threshold']:
                            exit_reason = "RSI Exit"
                            
                if not exit_reason and position.direction == "LONG":
                    if position.hold_candles >= params['r7_min_hold_candles']:
                        if curr_row['close'] < curr_row['ema50']:
                            exit_reason = "EMA50 Exit"
                            
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
                    
                    trades.append({
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
                    if position.qty <= 0.01 or exit_fraction == 1.0:
                        position = None

            if not position and time_only < time(15, 0):
                ctx = StrategyEvaluationContext()
                ctx.side = "buy"
                ctx.indicator_df = df_ind.iloc[max(0, i-50):i+1] # No .copy()
                
                result = evaluator.evaluate("buy", ctx)
                if result:
                    set_name = result['set_name']
                    
                    if set_name == "BUY_R7_VARIANT_D":
                        if i + 1 < len(df_ind):
                            next_row = df_ind.iloc[i+1]
                            entry_price = next_row['open']
                            entry_time = df_ind.index[i+1]
                            qty = capital_per_trade / entry_price
                            position = Position("LONG", entry_price, entry_time, qty)
                            
                    elif set_name == "SHORT_STREAK_MOMENTUM_BREAKDOWN":
                        if curr_row['gap_pct'] <= params['short_target_gap_threshold']:
                            if i + 1 < len(df_ind):
                                next_row = df_ind.iloc[i+1]
                                entry_price = next_row['open']
                                entry_time = df_ind.index[i+1]
                                qty = capital_per_trade / entry_price
                                position = Position("SHORT", entry_price, entry_time, qty)
                                
    except Exception as e:
        print(f"Error on {symbol}: {e}")
        
    return trades

def main():
    print("Starting Backtest script with multiprocessing...")
    hist_dir = r"C:\Extra Programs\Files\AlcoSoft_Financial_Services\data\historical"
    files = [f for f in os.listdir(hist_dir) if f.endswith("_5min.csv")]
    files.sort()
    
    params = {
        'long_stop_loss_percent': cfg('risk', 'long_stop_loss_percent', 0.010),
        'long_profit_target_percent': cfg('risk', 'long_profit_target_percent', 0.025),
        'short_stop_loss_percent': cfg('risk', 'short_stop_loss_percent', 0.005),
        'short_profit_target_percent': cfg('risk', 'short_profit_target_percent', 0.025),
        'long_rsi_exit_threshold': cfg('risk', 'long_rsi_exit_threshold', 78.0),
        'short_rsi_exit_threshold': cfg('risk', 'short_rsi_exit_threshold', 17.0),
        'long_partial_profit_fraction': cfg('risk', 'long_partial_profit_fraction', 0.25),
        'short_partial_profit_fraction': cfg('risk', 'short_partial_profit_fraction', 1.0),
        'r7_min_hold_candles': cfg('risk', 'r7_min_hold_candles', 20),
        'short_target_gap_threshold': cfg('risk', 'short_target_gap_threshold', -0.015)
    }
    
    max_open_positions = cfg('strategy', 'max_open_positions', 1)
    paper_capital = 100000.0
    margin_leverage = 5.0
    buying_power = paper_capital * margin_leverage
    capital_per_trade = buying_power / max_open_positions
    
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
    
    all_trades = []
    
    with concurrent.futures.ProcessPoolExecutor() as executor:
        futures = [executor.submit(process_symbol, f, hist_dir, capital_per_trade, evaluator, params) for f in files]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                all_trades.extend(res)
    
    print(f"Total trades executed: {len(all_trades)}")
    
    if all_trades:
        df_trades = pd.DataFrame(all_trades)
        
        report_lines = []
        report_lines.append("# R7 Local System Backtest Report")
        report_lines.append("")
        report_lines.append("## Configuration Used")
        report_lines.append(f"- Strategy sets: BUY_R7_VARIANT_D, SHORT_STREAK_MOMENTUM_BREAKDOWN")
        report_lines.append(f"- Capital: {paper_capital}")
        report_lines.append(f"- Leverage: {margin_leverage}x (Buying Power: {buying_power})")
        report_lines.append(f"- Max open positions: {max_open_positions}")
        report_lines.append(f"- Capital per trade: {capital_per_trade}")
        report_lines.append("")
        
        months = sorted(df_trades['month'].unique())
        total_long_trades = 0
        total_short_trades = 0
        total_long_wins = 0
        total_short_wins = 0
        total_gross_pnl = 0.0
        total_stt = 0.0
        total_net_pnl = 0.0
        exit_counts = defaultdict(float)
        
        for m in months:
            m_df = df_trades[df_trades['month'] == m]
            
            l_df = m_df[m_df['direction'] == 'LONG']
            s_df = m_df[m_df['direction'] == 'SHORT']
            
            l_count = len(l_df)
            s_count = len(s_df)
            
            l_wins = len(l_df[l_df['net_pnl'] > 0])
            s_wins = len(s_df[s_df['net_pnl'] > 0])
            
            m_gross = m_df['gross_pnl'].sum()
            m_stt = m_df['stt'].sum()
            m_net = m_df['net_pnl'].sum()
            
            total_long_trades += l_count
            total_short_trades += s_count
            total_long_wins += l_wins
            total_short_wins += s_wins
            total_gross_pnl += m_gross
            total_stt += m_stt
            total_net_pnl += m_net
            
            report_lines.append(f"### Month: {m}")
            report_lines.append(f"- **Total Trades**: {l_count + s_count} (Long: {l_count}, Short: {s_count})")
            
            l_win_rate = (l_wins / l_count * 100) if l_count > 0 else 0
            s_win_rate = (s_wins / s_count * 100) if s_count > 0 else 0
            
            report_lines.append(f"- **Win Rate**: Long {l_win_rate:.1f}%, Short {s_win_rate:.1f}%")
            report_lines.append(f"- **Gross PnL**: Rs. {m_gross:.2f}")
            report_lines.append(f"- **STT**: Rs. {m_stt:.2f}")
            report_lines.append(f"- **Net PnL**: Rs. {m_net:.2f}")
            
            m_exits = m_df.groupby('exit_reason')['net_pnl'].sum()
            report_lines.append("- **PnL by Exit Reason**:")
            for reason, val in m_exits.items():
                report_lines.append(f"  - {reason}: Rs. {val:.2f}")
                exit_counts[reason] += val
            report_lines.append("")
            
        report_lines.append("## Overall Summary")
        tot_trades = total_long_trades + total_short_trades
        report_lines.append(f"- **Total Trades**: {tot_trades} (Long: {total_long_trades}, Short: {total_short_trades})")
        
        tot_l_win_rate = (total_long_wins / total_long_trades * 100) if total_long_trades > 0 else 0
        tot_s_win_rate = (total_short_wins / total_short_trades * 100) if total_short_trades > 0 else 0
        
        report_lines.append(f"- **Win Rate**: Long {tot_l_win_rate:.1f}%, Short {tot_s_win_rate:.1f}%")
        report_lines.append(f"- **Gross PnL**: Rs. {total_gross_pnl:.2f}")
        report_lines.append(f"- **STT**: Rs. {total_stt:.2f}")
        report_lines.append(f"- **Net PnL**: Rs. {total_net_pnl:.2f}")
        report_lines.append(f"- **Net Return % (on 100k Base)**: {(total_net_pnl / 100000.0) * 100:.2f}%")
        
        report_lines.append("- **Total PnL by Exit Reason**:")
        for reason, val in exit_counts.items():
            report_lines.append(f"  - {reason}: Rs. {val:.2f}")
            
        artifact_path = r"C:\Users\RounakKR\.gemini\antigravity\brain\7e2d0406-a439-4972-95e1-fce5d3c0d70a\r7_local_system_report.md"
        with open(artifact_path, 'w') as f:
            f.write('\n'.join(report_lines))
            
        print("Summary generated successfully!")
        print(f"Total Net PnL: Rs. {total_net_pnl:.2f}")
        print(f"Net Return %: {(total_net_pnl / 100000.0) * 100:.2f}%")

if __name__ == "__main__":
    main()
