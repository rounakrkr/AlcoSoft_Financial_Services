import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import ta
import numpy as np
import datetime
import pytz
import warnings
warnings.filterwarnings('ignore')

from screener.morning_screener import _fetch_yahoo_history

# Nifty 50 Symbols
NIFTY_50 = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "SBI.NS", 
    "BHARTIARTL.NS", "ITC.NS", "LT.NS", "BAJFINANCE.NS", "HINDUNILVR.NS", "AXISBANK.NS", 
    "KOTAKBANK.NS", "MARUTI.NS", "SUNPHARMA.NS", "M&M.NS", "TATASTEEL.NS", "BAJAJFINSV.NS", 
    "ASIANPAINT.NS", "NTPC.NS", "TITAN.NS", "ULTRACEMCO.NS", "POWERGRID.NS", "ADANIENT.NS", 
    "HCLTECH.NS", "WIPRO.NS", "ONGC.NS", "JSWSTEEL.NS", "TECHM.NS", "ADANIPORTS.NS", 
    "HINDALCO.NS", "GRASIM.NS", "SBILIFE.NS", "LTIM.NS", "DRREDDY.NS", "EICHERMOT.NS", 
    "APOLLOHOSP.NS", "DIVISLAB.NS", "COALINDIA.NS", "BRITANNIA.NS", "TATAMOTORS.NS", 
    "BAJAJ-AUTO.NS", "CIPLA.NS", "TATACONSUM.NS", "HEROMOTOCO.NS", "NESTLEIND.NS", 
    "HDFCLIFE.NS", "UPL.NS", "INDUSINDBK.NS", "SHRIRAMFIN.NS"
]

def fetch_data():
    print("Fetching 5m and 1d historical data using the robust _fetch_yahoo_history from morning_screener...")
    
    daily_data = {}
    intraday_data = {}
    
    for symbol in NIFTY_50:
        try:
            # 5-minute data (max 59d)
            df_5m = _fetch_yahoo_history(symbol, period="59d", interval="5m")
            if not df_5m.empty:
                if df_5m.index.tz is None:
                    df_5m.index = df_5m.index.tz_localize('UTC').tz_convert('Asia/Kolkata')
                else:
                    df_5m.index = df_5m.index.tz_convert('Asia/Kolkata')
                    
                if len(df_5m) > 22:
                    df_5m['VWAP'] = ta.volume.VolumeWeightedAveragePrice(high=df_5m['High'], low=df_5m['Low'], close=df_5m['Close'], volume=df_5m['Volume']).volume_weighted_average_price()
                    df_5m['EMA20'] = ta.trend.EMAIndicator(close=df_5m['Close'], window=20).ema_indicator()
                    df_5m['EMA21'] = ta.trend.EMAIndicator(close=df_5m['Close'], window=21).ema_indicator()
                    df_5m['RSI14'] = ta.momentum.RSIIndicator(close=df_5m['Close'], window=14).rsi()
                    df_5m['RSI16'] = ta.momentum.RSIIndicator(close=df_5m['Close'], window=16).rsi()
                    df_5m['MAX10'] = df_5m['High'].rolling(10).max()
                    df_5m['MIN10'] = df_5m['Low'].rolling(10).min()
                    intraday_data[symbol] = df_5m

            # Daily data (120d)
            df_1d = _fetch_yahoo_history(symbol, period="120d", interval="1d")
            if not df_1d.empty:
                if df_1d.index.tz is None:
                    df_1d.index = df_1d.index.tz_localize('UTC').tz_convert('Asia/Kolkata')
                else:
                    df_1d.index = df_1d.index.tz_convert('Asia/Kolkata')
                df_1d['Prev_Close'] = df_1d['Close'].shift(1)
                df_1d['Prev_High'] = df_1d['High'].shift(1)
                df_1d['Prev_Prev_High'] = df_1d['High'].shift(2)
                df_1d['Gap_Pct'] = (df_1d['Open'] - df_1d['Prev_Close']) / df_1d['Prev_Close'] * 100
                daily_data[symbol] = df_1d
                
            print(f"Success: {symbol} (5m: {len(df_5m)}, 1d: {len(df_1d)})")
        except Exception as e:
            print(f"Failed to fetch {symbol}: {e}")

    return intraday_data, daily_data

def run_backtest():
    intraday_data, daily_data = fetch_data()
    print("Data acquired. Running Backtest with REVISED Master Blueprints...")
    
    all_times = []
    for sym, df in intraday_data.items():
        all_times.extend(df.index.normalize().unique())
    unique_days = sorted(list(set(all_times)))
    
    long_trades = []
    short_trades = []
    
    for day in unique_days:
        day_str_5m = day.strftime('%Y-%m-%d')
        gap_ups_1pct = 0
        gap_downs_06pct = 0
        total_stocks_with_data = 0
        day_gaps = {}
        daily_c1 = {}
        daily_h2 = {}
        
        for sym, df_1d in daily_data.items():
            idx_match = [i for i in df_1d.index if i.strftime('%Y-%m-%d') == day_str_5m]
            if idx_match:
                idx = idx_match[0]
                row = df_1d.loc[idx]
                gap = row.get('Gap_Pct', 0)
                if pd.isna(gap):
                    continue
                day_gaps[sym] = gap
                daily_c1[sym] = row.get('Prev_Close', 0)
                daily_h2[sym] = row.get('Prev_Prev_High', 0)
                total_stocks_with_data += 1
                if gap >= 1.0:
                    gap_ups_1pct += 1
                if gap <= -0.6:
                    gap_downs_06pct += 1
                    
        is_bull_day = gap_ups_1pct >= 20
        is_bear_day = gap_downs_06pct >= 20
        
        if not is_bull_day and not is_bear_day:
            continue
            
        for sym, df_5m in intraday_data.items():
            if sym not in day_gaps:
                continue
            stock_gap = day_gaps[sym]
            day_mask = (df_5m.index.normalize() == day)
            df_day = df_5m.loc[day_mask]
            
            if len(df_day) < 15:
                continue
                
            c1 = daily_c1.get(sym, 0)
            h2 = daily_h2.get(sym, 0)
            
            # --- LONG ENGINE ---
            if is_bull_day and stock_gap > -0.8:
                in_position = False
                entry_price = 0
                qty_held = 0  
                entry_time = None
                
                for i in range(11, len(df_day)-1):
                    prev_candle = df_day.iloc[i-1]
                    curr_candle = df_day.iloc[i]
                    next_candle = df_day.iloc[i+1]
                    
                    if not in_position:
                        if curr_candle.name.hour >= 15 and curr_candle.name.minute >= 10:
                            continue
                            
                        cond1 = prev_candle['Close'] > curr_candle['VWAP']
                        cond2 = prev_candle['EMA20'] > curr_candle['VWAP']
                        cond3 = prev_candle['RSI14'] > 61.0
                        max_10 = df_day['High'].iloc[i-10:i].max()
                        cond4 = curr_candle['Close'] > max_10
                        
                        # Entry Blocker
                        sell_ema_momentum_loss = prev_candle['Close'] < curr_candle['EMA21']
                        
                        if cond1 and cond2 and cond3 and cond4 and not sell_ema_momentum_loss:
                            in_position = True
                            entry_price = next_candle['Open']
                            entry_time = next_candle.name
                            qty_held = 1.0
                    else:
                        high_price = curr_candle['High']
                        low_price = curr_candle['Low']
                        
                        # 1. SL (1%)
                        if low_price <= entry_price * 0.99:
                            exit_price = entry_price * 0.99
                            pnl = (exit_price - entry_price) / entry_price * 100
                            long_trades.append({'Date': day_str_5m, 'Symbol': sym, 'Type': 'LONG', 'EntryTime': entry_time, 'EntryPrice': entry_price, 'ExitTime': curr_candle.name, 'ExitPrice': exit_price, 'PnL_Pct': pnl, 'Reason': 'SL', 'Weight': qty_held})
                            in_position = False
                            continue
                            
                        # 2. Dynamic Exit
                        dyn_exit = prev_candle['Close'] < curr_candle['EMA21']
                        if dyn_exit:
                            exit_price = next_candle['Open']
                            pnl = (exit_price - entry_price) / entry_price * 100
                            long_trades.append({'Date': day_str_5m, 'Symbol': sym, 'Type': 'LONG', 'EntryTime': entry_time, 'EntryPrice': entry_price, 'ExitTime': curr_candle.name, 'ExitPrice': exit_price, 'PnL_Pct': pnl, 'Reason': 'DYN_EXIT_EMA', 'Weight': qty_held})
                            in_position = False
                            continue
                            
                        # 3. Partial
                        if qty_held == 1.0 and high_price >= entry_price * 1.005:
                            qty_held = 0.25
                            exit_price = entry_price * 1.005
                            pnl = (exit_price - entry_price) / entry_price * 100
                            long_trades.append({'Date': day_str_5m, 'Symbol': sym, 'Type': 'LONG', 'EntryTime': entry_time, 'EntryPrice': entry_price, 'ExitTime': curr_candle.name, 'ExitPrice': exit_price, 'PnL_Pct': pnl, 'Reason': 'PT_75', 'Weight': 0.75})
                            
                        # 4. Runner
                        if qty_held == 0.25 and curr_candle['RSI14'] >= 72.0:
                            qty_held = 0.0
                            exit_price = curr_candle['Close']
                            pnl = (exit_price - entry_price) / entry_price * 100
                            long_trades.append({'Date': day_str_5m, 'Symbol': sym, 'Type': 'LONG', 'EntryTime': entry_time, 'EntryPrice': entry_price, 'ExitTime': curr_candle.name, 'ExitPrice': exit_price, 'PnL_Pct': pnl, 'Reason': 'OB_Exit_25', 'Weight': 0.25})
                            in_position = False
                            continue
                            
                        # 5. EOD
                        if curr_candle.name.hour == 15 and curr_candle.name.minute >= 15:
                            exit_price = curr_candle['Close']
                            pnl = (exit_price - entry_price) / entry_price * 100
                            long_trades.append({'Date': day_str_5m, 'Symbol': sym, 'Type': 'LONG', 'EntryTime': entry_time, 'EntryPrice': entry_price, 'ExitTime': curr_candle.name, 'ExitPrice': exit_price, 'PnL_Pct': pnl, 'Reason': 'EOD', 'Weight': qty_held})
                            in_position = False
                            continue
            
            # --- SHORT ENGINE ---
            if is_bear_day and stock_gap <= -0.8:
                in_position = False
                entry_price = 0
                qty_held = 0  
                entry_time = None
                
                # Rule 2 Entry Blocker
                skip_short = (c1 > h2)
                
                for i in range(11, len(df_day)-1):
                    prev_candle = df_day.iloc[i-1]
                    curr_candle = df_day.iloc[i]
                    next_candle = df_day.iloc[i+1]
                    
                    if not in_position:
                        if curr_candle.name.hour >= 15 and curr_candle.name.minute >= 10:
                            continue
                            
                        cond1 = prev_candle['Close'] < curr_candle['VWAP']
                        cond2 = prev_candle['EMA20'] < curr_candle['VWAP']
                        cond3 = prev_candle['RSI14'] < 39.0
                        min_10 = df_day['Low'].iloc[i-10:i].min()
                        cond4 = curr_candle['Close'] < min_10
                        
                        if skip_short:
                            pass
                        elif cond1 and cond2 and cond3 and cond4:
                            in_position = True
                            entry_price = next_candle['Open']
                            entry_time = next_candle.name
                            qty_held = 1.0
                    else:
                        high_price = curr_candle['High']
                        low_price = curr_candle['Low']
                        
                        # NO FIXED STOP LOSS!
                        
                        # 1. Partial Target
                        if qty_held == 1.0 and low_price <= entry_price * 0.995:
                            qty_held = 0.25
                            exit_price = entry_price * 0.995
                            pnl = (entry_price - exit_price) / entry_price * 100
                            short_trades.append({'Date': day_str_5m, 'Symbol': sym, 'Type': 'SHORT', 'EntryTime': entry_time, 'EntryPrice': entry_price, 'ExitTime': curr_candle.name, 'ExitPrice': exit_price, 'PnL_Pct': pnl, 'Reason': 'PT_75', 'Weight': 0.75})
                            
                        # 2. Panic Runner
                        if qty_held == 0.25 and curr_candle['RSI16'] <= 15.0:
                            qty_held = 0.0
                            exit_price = curr_candle['Close']
                            pnl = (entry_price - exit_price) / entry_price * 100
                            short_trades.append({'Date': day_str_5m, 'Symbol': sym, 'Type': 'SHORT', 'EntryTime': entry_time, 'EntryPrice': entry_price, 'ExitTime': curr_candle.name, 'ExitPrice': exit_price, 'PnL_Pct': pnl, 'Reason': 'OS_Exit_25', 'Weight': 0.25})
                            in_position = False
                            continue
                            
                        # 3. EOD
                        if curr_candle.name.hour == 15 and curr_candle.name.minute >= 15:
                            exit_price = curr_candle['Close']
                            pnl = (entry_price - exit_price) / entry_price * 100
                            short_trades.append({'Date': day_str_5m, 'Symbol': sym, 'Type': 'SHORT', 'EntryTime': entry_time, 'EntryPrice': entry_price, 'ExitTime': curr_candle.name, 'ExitPrice': exit_price, 'PnL_Pct': pnl, 'Reason': 'EOD', 'Weight': qty_held})
                            in_position = False
                            continue

    def consolidate_trades(trades):
        df = pd.DataFrame(trades)
        if df.empty:
            return df
        cons = []
        for (sym, etime), group in df.groupby(['Symbol', 'EntryTime']):
            row = group.iloc[0].copy()
            if 'SL' in group['Reason'].values:
                row['Net_PnL'] = group[group['Reason'] == 'SL'].iloc[0]['PnL_Pct']
            else:
                net_pnl = sum([r['PnL_Pct'] * r['Weight'] for idx, r in group.iterrows()])
                row['Net_PnL'] = net_pnl
            row['ExitReason'] = " | ".join(group['Reason'].tolist())
            row['FinalExitTime'] = group['ExitTime'].max()
            cons.append(row)
        return pd.DataFrame(cons).sort_values(by=['Date', 'EntryTime'])
        
    df_long = consolidate_trades(long_trades)
    df_short = consolidate_trades(short_trades)
    
    def simulate_portfolio(df_t, name):
        print(f"\n{'='*50}\n{name} PORTFOLIO SIMULATION\n{'='*50}")
        if df_t.empty:
            print("No trades found.")
            return
            
        capital = 100000.0  # 1 Lakh Base Capital
        margin_multiplier = 5.0
        max_open_pos = 3
        tax_rate = 0.00035 # 0.035% STT + Charges on EXIT AMOUNT
        
        simulated_trades = []
        
        # Group trades by day
        for day, group in df_t.groupby('Date'):
            day_trades = group.sort_values(by='EntryTime')
            active_positions = []  
            
            for idx, trade in day_trades.iterrows():
                entry_time = trade['EntryTime']
                
                # Free up positions that have exited before or exactly at this entry_time
                active_positions = [ex for ex in active_positions if ex > entry_time]
                
                if len(active_positions) < max_open_pos:
                    # Execute trade
                    buying_power = capital * margin_multiplier
                    position_size = buying_power / max_open_pos
                    
                    gross_pnl_inr = position_size * (trade['Net_PnL'] / 100.0)
                    
                    # Exit amount is approx position size + gross_pnl
                    exit_amount = position_size + gross_pnl_inr
                    tax_amount = exit_amount * tax_rate
                    
                    net_pnl_inr = gross_pnl_inr - tax_amount
                    
                    capital += net_pnl_inr # Update running capital compounding
                    
                    trade_copy = trade.copy()
                    trade_copy['Pos_Size_INR'] = position_size
                    trade_copy['Gross_PnL_INR'] = gross_pnl_inr
                    trade_copy['Tax_INR'] = tax_amount
                    trade_copy['Net_PnL_INR'] = net_pnl_inr
                    trade_copy['Running_Capital'] = capital
                    
                    simulated_trades.append(trade_copy)
                    active_positions.append(trade['FinalExitTime'])
                else:
                    # Trade skipped due to max_open_pos limit
                    pass
                    
        sim_df = pd.DataFrame(simulated_trades)
        if sim_df.empty:
            print("No executed trades after applying capital limits.")
            return
            
        total_trades = len(sim_df)
        winners = len(sim_df[sim_df['Net_PnL_INR'] > 0])
        win_rate = (winners / total_trades) * 100 if total_trades > 0 else 0
        total_net_profit_inr = sim_df['Net_PnL_INR'].sum()
        total_tax_paid = sim_df['Tax_INR'].sum()
        
        print(f"Base Capital      : INR 1,00,000.00")
        print(f"Margin Applied    : 5x")
        print(f"Max Open Pos      : {max_open_pos}")
        print(f"Total Exec Trades : {total_trades}")
        print(f"Win Rate          : {win_rate:.1f}%")
        print(f"Total Tax/Charges : INR {total_tax_paid:.2f}")
        print(f"Net Profit (INR)  : INR {total_net_profit_inr:.2f}")
        print(f"Final Capital     : INR {capital:.2f}")
        print(f"ROI on Base Cap   : {((capital - 100000)/100000)*100:.2f}%\n")
        
        fname = f"c:/Extra Programs/Files/AlcoSoft_Financial_Services/research/{name.lower()}_portfolio_trades.csv"
        sim_df.to_csv(fname, index=False)
        print(f"Saved exact trade log to {fname}")

    simulate_portfolio(df_long, "LONG_ENGINE")
    simulate_portfolio(df_short, "SHORT_ENGINE")
    print("\nBacktest Complete.")
    
if __name__ == "__main__":
    run_backtest()
