import os
import sys
import pandas as pd
import json
import math
from datetime import datetime, time as dt_time

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.trading_settings import get_section
from core.strategy import _build_indicators, CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext
import niftystocks.ns

def run_backtest():
    print("Loading Nifty Midcap 50 stocks...")
    try:
        midcap_50 = niftystocks.ns.get_nifty_midcap50()
    except Exception as e:
        print(f"Error fetching midcap 50: {e}")
        # fallback to all files in historical dir
        base_dir = os.path.join(os.path.dirname(__file__), '..', 'data', 'historical')
        midcap_50 = [f.replace('_5min.csv', '') for f in os.listdir(base_dir) if f.endswith('_5min.csv')]

    data = {}
    base_dir = os.path.join(os.path.dirname(__file__), '..', 'data', 'historical')
    
    # 1. Load config
    risk_cfg = get_section('risk')
    strategy_cfg = get_section('strategy')
    
    max_positions = strategy_cfg.get("max_open_positions", 2)
    
    sl_long = risk_cfg.get("long_stop_loss_percent", 0.010)
    pt_long = risk_cfg.get("long_profit_target_percent", 0.025)
    rsi_exit_long = risk_cfg.get("long_rsi_exit_threshold", 78.0)
    
    sl_short = risk_cfg.get("short_stop_loss_percent", 0.005)
    pt_short = risk_cfg.get("short_profit_target_percent", 0.025)
    rsi_exit_short = risk_cfg.get("short_rsi_exit_threshold", 17.0)
    short_target_gap = risk_cfg.get("short_target_gap_threshold", -0.008)
    long_exclude_gap = risk_cfg.get("long_exclude_gap_threshold", -0.008)
    
    r7_min_hold = risk_cfg.get("r7_min_hold_candles", 20)
    
    bull_gap = risk_cfg.get("regime_bull_gap_pct", 0.007)
    bear_gap = risk_cfg.get("regime_bear_gap_pct", -0.006)
    bull_breadth = risk_cfg.get("regime_bull_breadth_pct", 0.35)
    bear_breadth = risk_cfg.get("regime_bear_breadth_pct", 0.40)
    
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
    
    print("Pre-calculating indicators...")
    all_dates = set()
    
    for symbol in midcap_50:
        file_path = os.path.join(base_dir, f"{symbol}_5min.csv")
        if not os.path.exists(file_path):
            continue
        df = pd.read_csv(file_path)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df[df['timestamp'] >= '2024-01-01'].copy()
        if df.empty:
            continue
        
        df.set_index('timestamp', inplace=True)
        df = df[~df.index.duplicated(keep='first')]
        df.sort_index(inplace=True)
        
        # add bucket for VWAP
        df['bucket'] = df.index.strftime('%Y-%m-%d %H:%M')
        
        try:
            df = _build_indicators(df)
            data[symbol] = df
            all_dates.update(df.index.normalize().unique())
        except Exception as e:
            print(f"Error building indicators for {symbol}: {e}")
            
    sorted_dates = sorted(list(all_dates))
    print(f"Loaded {len(data)} stocks, {len(sorted_dates)} trading days.")
    
    trades = []
    
    for i, current_date in enumerate(sorted_dates):
        # compute breadth for current_date
        gap_picts = []
        stock_gaps = {}
        for symbol, df in data.items():
            daily_df = df.loc[df.index.normalize() == current_date]
            if daily_df.empty:
                continue
            first_candle = daily_df.iloc[0]
            
            # prev close
            prev_df = df.loc[df.index.normalize() < current_date]
            if prev_df.empty:
                continue
            prev_close = prev_df.iloc[-1]['close']
            
            gap_pct = (first_candle['open'] - prev_close) / prev_close
            gap_picts.append(gap_pct)
            stock_gaps[symbol] = gap_pct
            
        if not gap_picts:
            continue
            
        median_gap = pd.Series(gap_picts).median() if gap_picts else 0.0
        
        bull_b_val = sum(1 for g in gap_picts if g >= bull_gap) / len(gap_picts)
        bear_b_val = sum(1 for g in gap_picts if g <= bear_gap) / len(gap_picts)
        
        # Per tearsheet: the breadth requirement is the only aggregate constraint
        # market_gap_threshold is used inside the breadth calculation itself
        long_enabled = (bull_b_val >= bull_breadth)
        short_enabled = (bear_b_val >= bear_breadth)
        
        times = pd.date_range(start=current_date + pd.Timedelta(hours=9, minutes=15), 
                              end=current_date + pd.Timedelta(hours=15, minutes=30), 
                              freq='5min')
                              
        active_positions = []
        
        for t in times:
            t_time = t.time()
            
            # OPTIMIZATION: Skip evaluating if trading is disabled and no positions are held
            if not long_enabled and not short_enabled and not active_positions:
                continue
            
            if t_time > dt_time(15, 15):
                for position in list(active_positions):
                    exit_price = data[position['symbol']].loc[t]['close'] if t in data[position['symbol']].index else position['entry_price']
                    position['exit_price'] = float(exit_price)
                    position['exit_time'] = t
                    position['exit_reason'] = 'EOD Squareoff'
                    trades.append(position)
                    active_positions.remove(position)
                continue
                
            # Manage open positions
            for position in list(active_positions):
                symbol = position['symbol']
                df = data[symbol]
                if t not in df.index:
                    continue
                    
                row = df.loc[t]
                position['hold_candles'] += 1
                
                if position['side'] == 'buy':
                    # Stop loss
                    if row['low'] <= position['sl']:
                        position['exit_price'] = float(position['sl'])
                        position['exit_time'] = t
                        position['exit_reason'] = 'Stop Loss'
                        trades.append(position)
                        active_positions.remove(position)
                        continue
                        
                    # Profit Target
                    pt_price = position['entry_price'] * (1 + pt_long)
                    if row['high'] >= pt_price:
                        if risk_cfg.get("partial_profit_booking_enabled", False) and not position.get('partial_booked', False):
                            frac = risk_cfg.get("long_partial_profit_fraction", 0.25)
                            if frac < 1.0:
                                partial_trade = position.copy()
                                partial_trade['exit_price'] = float(pt_price)
                                partial_trade['exit_time'] = t
                                partial_trade['exit_reason'] = 'Partial Profit'
                                # BUG #2 FIX: store the fraction so PnL can compute correct qty
                                partial_trade['partial_fraction'] = frac
                                trades.append(partial_trade)
                                
                                # Store booking fraction on the continuing position
                                position['booking_fraction'] = frac
                                position['partial_booked'] = True
                                continue
                                
                        position['exit_price'] = float(pt_price)
                        position['exit_time'] = t
                        position['exit_reason'] = 'Profit Target'
                        trades.append(position)
                        active_positions.remove(position)
                        continue
                        
                    # RSI Exit - only after min_hold_time=20 minutes (4 candles) per tearsheet definition
                    if row['rsi'] >= rsi_exit_long and position['hold_candles'] * 5 >= r7_min_hold:
                        position['exit_price'] = float(row['close'])
                        position['exit_time'] = t
                        position['exit_reason'] = 'RSI Exit'
                        trades.append(position)
                        active_positions.remove(position)
                        continue
                        
                    # Dynamic EMA50 Exit - LONG only, dyn_exit_hold_time=0 (no hold constraint per tearsheet)
                    # BUG #1 FIX: idx was only defined in the entry block below, not here in exit block
                    exit_idx = df.index.get_loc(t)
                    if exit_idx >= 1:
                        close_1 = df.iloc[exit_idx - 1]['close']
                        ema50_1 = df.iloc[exit_idx - 1]['ema50'] if 'ema50' in df.columns else float('inf')
                        if close_1 < ema50_1:
                            # To perfectly match Research Engine's exact backtest logic, we must apply the 1-candle lag it had:
                            # Research Engine exited at idx+1 open instead of idx open.
                            if exit_idx + 1 < len(df):
                                delayed_exit_time = df.index[exit_idx + 1]
                                delayed_exit_price = float(df.iloc[exit_idx + 1]['open'])
                            else:
                                delayed_exit_time = t
                                delayed_exit_price = float(row['open'])
                                
                            position['exit_price'] = delayed_exit_price
                            position['exit_time'] = delayed_exit_time
                            position['exit_reason'] = 'Dynamic Exit'
                            trades.append(position)
                            active_positions.remove(position)
                            continue
                        
                else: # short
                    # Stop loss
                    if row['high'] >= position['sl']:
                        position['exit_price'] = float(position['sl'])
                        position['exit_time'] = t
                        position['exit_reason'] = 'Stop Loss'
                        trades.append(position)
                        active_positions.remove(position)
                        continue
                        
                    # Profit Target
                    pt_price = position['entry_price'] * (1 - pt_short)
                    if row['low'] <= pt_price:
                        if risk_cfg.get("partial_profit_booking_enabled", False) and not position.get('partial_booked', False):
                            frac = risk_cfg.get("short_partial_profit_fraction", 1.0)
                            if frac < 1.0:
                                partial_trade = position.copy()
                                partial_trade['exit_price'] = float(pt_price)
                                partial_trade['exit_time'] = t
                                partial_trade['exit_reason'] = 'Partial Profit'
                                partial_trade['qty'] = position['qty'] * frac
                                trades.append(partial_trade)
                                
                                position['qty'] -= position['qty'] * frac
                                position['partial_booked'] = True
                                continue
                                
                        position['exit_price'] = float(pt_price)
                        position['exit_time'] = t
                        position['exit_reason'] = 'Profit Target'
                        trades.append(position)
                        active_positions.remove(position)
                        continue
                        
                    # RSI Exit - Short (no hold constraint per tearsheet)
                    if row['rsi'] <= rsi_exit_short:
                        position['exit_price'] = float(row['close'])
                        position['exit_time'] = t
                        position['exit_reason'] = 'RSI Exit'
                        trades.append(position)
                        active_positions.remove(position)
                        continue
            
            # Entry logic
            if len(active_positions) < max_positions and t_time < dt_time(15, 0):
                for symbol, df in data.items():
                    # Double check max_positions inside loop in case multiple signals trigger at same time
                    if len(active_positions) >= max_positions:
                        break
                        
                    if t not in df.index:
                        continue
                    
                    # Prevent taking multiple positions on same stock
                    if any(p['symbol'] == symbol for p in active_positions):
                        continue
                    
                    idx = df.index.get_loc(t)
                    if idx < 20:
                        continue
                        
                    trailing_df = df.iloc[idx-20:idx+1]
                    
                    # We use side="buy" for entry evaluation because all entry sets are in buy_sets in strategy_sets.json
                    ctx = StrategyEvaluationContext(side="buy", indicator_df=trailing_df, pattern_df=trailing_df, ws_count=100)
                    res = evaluator.evaluate('buy', ctx)
                    
                    if res:
                        set_name = res["set_name"]
                        stock_gap = stock_gaps.get(symbol, 0.0)
                        
                        if set_name.startswith("SHORT_"):
                            if short_enabled and stock_gap <= short_target_gap:
                                active_positions.append({
                                    "symbol": symbol,
                                    "side": "sell",
                                    "entry_price": float(trailing_df.iloc[-1]['close']),
                                    "entry_time": t,
                                    "qty": 1.0,
                                    "partial_booked": False,
                                    "hold_candles": 0,
                                    "sl": float(trailing_df.iloc[-1]['close']) * (1 + sl_short),
                                    "realized_pnl": 0.0,
                                    "set_name": set_name
                                })
                        else:
                            if long_enabled and stock_gap > long_exclude_gap:
                                active_positions.append({
                                    "symbol": symbol,
                                    "side": "buy",
                                    "entry_price": float(trailing_df.iloc[-1]['close']),
                                    "entry_time": t,
                                    "qty": 1.0,
                                    "partial_booked": False,
                                    "hold_candles": 0,
                                    "sl": float(trailing_df.iloc[-1]['close']) * (1 - sl_long),
                                    "realized_pnl": 0.0,
                                    "set_name": set_name
                                })

    print(f"Backtest complete. Total trades: {len(trades)}")
    
    # Process trades and generate report
    # Match research engine TradeRecord exactly:
    # pnl_gross = (exit_price - entry_price) * quantity  [for LONG]
    # pnl_gross = (entry_price - exit_price) * quantity  [for SHORT]
    # stt_tax = exit_price * quantity * 0.00035
    # capital = 100000, margin = 5x, capital_per_trade = 500000
    capital = 100000.0
    buying_power = capital * 5.0  # 5x margin
    
    for trade in trades:
        entry_price = trade['entry_price']
        exit_price = trade['exit_price']
        
        # Full position qty as computed by research engine: int(buying_power // entry_price)
        full_qty = int(buying_power // entry_price)
        
        # BUG #2 FIX: correctly size partial and remaining positions
        if 'partial_fraction' in trade:
            # This is the partial exit segment (e.g. 25% of full position)
            qty = int(full_qty * trade['partial_fraction'])
        elif trade.get('partial_booked') and 'booking_fraction' in trade:
            # This is the remaining position after partial booking (e.g. 75% of full position)
            qty = full_qty - int(full_qty * trade['booking_fraction'])
        else:
            qty = full_qty
        
        if trade['side'] == 'buy':
            pnl_gross = (exit_price - entry_price) * qty
        else:
            pnl_gross = (entry_price - exit_price) * qty
            
        stt_tax = exit_price * qty * 0.00035
        pnl_net = pnl_gross - stt_tax
        
        trade['actual_qty'] = qty
        trade['gross_pnl_abs'] = pnl_gross
        trade['stt_abs'] = stt_tax
        trade['net_pnl_abs'] = pnl_net
        # Express as % of capital
        trade['gross_pnl'] = pnl_gross / capital
        trade['stt'] = stt_tax / capital
        trade['net_pnl'] = pnl_net / capital
        trade['is_win'] = pnl_net > 0
        trade['month'] = trade['entry_time'].strftime('%Y-%m')

    # Group by month
    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        print("No trades executed.")
        with open("backtest_report_data.json", "w") as f:
            json.dump({"total_return": 0}, f)
        return

    report = []
    total_net = 0.0
    for month, group in trades_df.groupby('month'):
        n_trades = len(group)
        win_rate = group['is_win'].mean() * 100
        gross = group['gross_pnl'].sum() * 100
        stt = group['stt'].sum() * 100
        net = group['net_pnl'].sum() * 100
        total_net += net
        report.append({
            "Month": month,
            "Trades": n_trades,
            "Win Rate": f"{win_rate:.1f}%",
            "Gross PnL": f"{gross:.2f}%",
            "STT": f"{stt:.4f}%",
            "Net PnL": f"{net:.2f}%"
        })

    long_trades = trades_df[trades_df['side'] == 'buy']
    short_trades = trades_df[trades_df['side'] == 'sell']
    
    summary = {
        "report": report,
        "total_net_return": total_net,
        "long_count": len(long_trades),
        "long_win": long_trades['is_win'].mean() * 100 if not long_trades.empty else 0.0,
        "short_count": len(short_trades),
        "short_win": short_trades['is_win'].mean() * 100 if not short_trades.empty else 0.0,
        "exit_reasons": trades_df['exit_reason'].value_counts().to_dict()
    }
    
    with open("backtest_report_data.json", "w") as f:
        json.dump(summary, f, indent=4)
        
    print(f"Total Net Return: {total_net:.2f}%")
    
if __name__ == "__main__":
    run_backtest()
