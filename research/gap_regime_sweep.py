import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import time as dtime
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from screener.morning_screener import NIFTY_50, _fetch_yahoo_history
from core.strategy import (CONDITION_REGISTRY, StrategySetEvaluator,
                            StrategyEvaluationContext, _build_indicators)
from core.strategy_sets import load_strategy_sets
from core.order_executor import calculate_stop_loss, calculate_target, round_to_tick
from core.trading_settings import get as cfg

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
config       = load_strategy_sets()
buy_set_def  = next((s for s in config.buy_sets  if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
sell_set_def = next((s for s in config.buy_sets  if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN"), None)
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)

if not buy_set_def or not sell_set_def:
    print("Error: Required strategies not found in config.")
    sys.exit(1)

tsl_activation_ratio = float(cfg("risk", "tsl_activation_ratio", 1.4))
trailing_sl_percent  = float(cfg("risk", "trailing_sl_percent",  0.008))
position_size_margin = float(cfg("risk", "position_size_margin", 1.0))

def fetch(symbol, period="60d", interval="5m"):
    try:
        df = _fetch_yahoo_history(symbol, period=period, interval=interval)
        if df is None or df.empty: return pd.DataFrame()
        df.columns = [c.lower() for c in df.columns]
        df.dropna(subset=["close"], inplace=True)
        df["bucket"] = df.index
        df = _build_indicators(df)
        df.dropna(subset=["ema21","vwap"], inplace=True)
        return df if len(df) >= 20 else pd.DataFrame()
    except Exception:
        return pd.DataFrame()

print("Loading stocks...")
stock_dfs = {}
for i, sym in enumerate(NIFTY_50):
    print(f"  {i+1}/{len(NIFTY_50)}: {sym}    ", end="\r")
    df = fetch(sym)
    if not df.empty:
        stock_dfs[sym] = df
print(f"\n{len(stock_dfs)} stocks loaded.")

print("Computing per-stock daily gaps...")
stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        day_open = float(grp["open"].iloc[0])
        gap_pct = ((day_open - prev_close) / prev_close) if prev_close else 0.0
        by_day[d] = {
            "gap_pct": gap_pct,
        }
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

all_dates = sorted(set(d for days in stock_day_data.values() for d in days.keys()))

print("Running baseline strategy simulation...")
timeline = []
for sym, df in stock_dfs.items():
    for t, row in df.iterrows():
        if t.time() >= dtime(9, 15) and t.time() <= dtime(15, 0):
            timeline.append((t, sym, row, df))
timeline.sort(key=lambda x: x[0])

# Pre-compute all potential trades
all_long_trades = []
all_short_trades = []

active_longs = {}
active_shorts = {}
daily_long_counts = {}
daily_short_counts = {}

for t, sym, row, df in timeline:
    date_val = t.date()
    current_price = row["close"]
    high = row["high"]
    low = row["low"]
    slice_df = df.loc[:t].copy()
    if len(slice_df) < 20: continue
    rsi_0 = slice_df["rsi"].iloc[-1] if "rsi" in slice_df.columns else 50
    
    # Manage active LONGS
    if sym in active_longs:
        trade = active_longs[sym]
        ep = trade['entry_price']
        qty = trade['qty']
        
        # RSI Exit
        if rsi_0 >= 72:
            trade['exit_price'] = current_price
            trade['exit_time'] = t
            trade['pnl'] = (current_price - ep) * qty
            trade['reason'] = 'RSI_EXIT'
            all_long_trades.append(trade)
            del active_longs[sym]
            continue
            
        ex = None
        if low <= trade['tsl']:
            ex = min(trade['tsl'], row["open"])
        elif t.time() >= dtime(15, 10):
            ex = current_price
            
        if ex is not None:
            trade['exit_price'] = ex
            trade['exit_time'] = t
            trade['pnl'] = (ex - ep) * qty
            trade['reason'] = 'TSL/TIME'
            all_long_trades.append(trade)
            del active_longs[sym]
        else:
            # Update TSL
            if high >= ep + abs(ep - trade['sl']) * tsl_activation_ratio:
                trade['tsl_on'] = True
            if trade['tsl_on']:
                n = round_to_tick(high * (1 - trailing_sl_percent))
                if n > trade['tsl']: trade['tsl'] = n

    # Manage active SHORTS
    elif sym in active_shorts:
        trade = active_shorts[sym]
        ep = trade['entry_price']
        qty = trade['qty']
        
        # RSI Cover
        if rsi_0 <= 28:
            trade['exit_price'] = current_price
            trade['exit_time'] = t
            trade['pnl'] = (ep - current_price) * qty
            trade['reason'] = 'RSI_COVER'
            all_short_trades.append(trade)
            del active_shorts[sym]
            continue
            
        ex = None
        if high >= trade['tsl']:
            ex = max(trade['tsl'], row["open"])
        elif t.time() >= dtime(15, 10):
            ex = current_price
            
        if ex is not None:
            trade['exit_price'] = ex
            trade['exit_time'] = t
            trade['pnl'] = (ep - ex) * qty
            trade['reason'] = 'TSL/TIME'
            all_short_trades.append(trade)
            del active_shorts[sym]
        else:
            # Update TSL
            if low <= ep - abs(ep - trade['sl']) * tsl_activation_ratio:
                trade['tsl_on'] = True
            if trade['tsl_on']:
                n = round_to_tick(low * (1 + trailing_sl_percent))
                if n < trade['tsl']: trade['tsl'] = n

    else:
        if dtime(9, 20) <= t.time() <= dtime(14, 0):
            ctx_long = StrategyEvaluationContext(side="buy", indicator_df=slice_df)
            res_long = evaluator.evaluate("buy", ctx_long)
            if res_long and res_long['set_name'] == buy_set_def.name:
                if len(active_longs) < 3:
                    qty = max(1, int(((BUYING_POWER / 3) * position_size_margin) / current_price))
                    active_longs[sym] = {
                        'date': date_val, 'sym': sym, 'entry_time': t, 'entry_price': current_price,
                        'qty': qty, 'sl': current_price * (1 - 0.01), 'tsl': current_price * (1 - 0.01),
                        'tsl_on': False
                    }
                    continue
            
            ctx_short = StrategyEvaluationContext(side="buy", indicator_df=slice_df)
            res_short = evaluator.evaluate("buy", ctx_short)
            if res_short and res_short['set_name'] == sell_set_def.name:
                if len(active_shorts) < 3:
                    qty = max(1, int(((BUYING_POWER / 3) * position_size_margin) / current_price))
                    active_shorts[sym] = {
                        'date': date_val, 'sym': sym, 'entry_time': t, 'entry_price': current_price,
                        'qty': qty, 'sl': current_price * (1 + 0.01), 'tsl': current_price * (1 + 0.01),
                        'tsl_on': False
                    }

print(f"Simulation generated {len(all_long_trades)} longs and {len(all_short_trades)} shorts total.")

thresholds = [0.004, 0.005, 0.006, 0.007]

print("\n=========================================================================")
print("GAP MAGNITUDE SWEEP RESULTS (WITH TSL & RSI EXITS)")
print("=========================================================================")

for thr in thresholds:
    gap_up_days = set()
    gap_down_days = set()
    neutral_days = set()
    
    for d in all_dates:
        ups = downs = 0
        total = 0
        for sym, days in stock_day_data.items():
            if d in days:
                total += 1
                g = days[d]["gap_pct"]
                if g >= thr: ups += 1
                elif g <= -thr: downs += 1
        
        if total > 0:
            if ups / total >= 0.40: gap_up_days.add(d)
            elif downs / total >= 0.40: gap_down_days.add(d)
            else: neutral_days.add(d)
            
    long_gap_up_trades = [t for t in all_long_trades if t['date'] in gap_up_days]
    short_gap_down_trades = [t for t in all_short_trades if t['date'] in gap_down_days]
    long_neutral_trades = [t for t in all_long_trades if t['date'] in neutral_days]
    short_neutral_trades = [t for t in all_short_trades if t['date'] in neutral_days]
    
    lg_pnl = sum(t['pnl'] for t in long_gap_up_trades)
    sd_pnl = sum(t['pnl'] for t in short_gap_down_trades)
    ln_pnl = sum(t['pnl'] for t in long_neutral_trades)
    sn_pnl = sum(t['pnl'] for t in short_neutral_trades)
    
    lg_wr = sum(1 for t in long_gap_up_trades if t['pnl'] > 0) / max(1, len(long_gap_up_trades))
    sd_wr = sum(1 for t in short_gap_down_trades if t['pnl'] > 0) / max(1, len(short_gap_down_trades))
    ln_wr = sum(1 for t in long_neutral_trades if t['pnl'] > 0) / max(1, len(long_neutral_trades))
    sn_wr = sum(1 for t in short_neutral_trades if t['pnl'] > 0) / max(1, len(short_neutral_trades))
    
    print(f"\nTHRESHOLD: {thr*100:.2f}% (Days: Up={len(gap_up_days)}, Down={len(gap_down_days)}, Neutral={len(neutral_days)})")
    print(f"  [GAP UP 40]   Longs:  Trades={len(long_gap_up_trades):<3} | WR={lg_wr*100:5.1f}% | PnL=Rs{lg_pnl:>9.2f}")
    print(f"  [GAP DOWN 40] Shorts: Trades={len(short_gap_down_trades):<3} | WR={sd_wr*100:5.1f}% | PnL=Rs{sd_pnl:>9.2f}")
    print(f"  [NEUTRAL]     Longs:  Trades={len(long_neutral_trades):<3} | WR={ln_wr*100:5.1f}% | PnL=Rs{ln_pnl:>9.2f}")
    print(f"  [NEUTRAL]     Shorts: Trades={len(short_neutral_trades):<3} | WR={sn_wr*100:5.1f}% | PnL=Rs{sn_pnl:>9.2f}")
    
    net_pnl = lg_pnl + sd_pnl
    print(f"  => NET STRATEGY P&L (Regime Matching Only): Rs{net_pnl:>9.2f}")
