import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import ta
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from research.build_cache import load_cache
from core.strategy import (CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext)
from core.strategy_sets import load_strategy_sets
from core.order_executor import round_to_tick

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
MP = 3; SL_PCT = 0.010
PROFIT_TARGET = 0.005
PARTIAL_FRAC = 0.75 # Sell 75% at 0.50% profit
STT_PCT = 0.00035

config = load_strategy_sets()
buy_set_def = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
sell_set_def = next((s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)

evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

print("Loading cache...")
stock_dfs = load_cache()
print("Cache loaded.")

# Build timeline and gap_up logic
timeline_set = set()
for sym, df in stock_dfs.items():
    timeline_set.update(df.index)
timeline = sorted(list(timeline_set))
stock_ts_map = {}
for sym, df in stock_dfs.items():
    stock_ts_map[sym] = {ts: i for i, ts in enumerate(df.index)}

df_list = []
for sym, df in stock_dfs.items():
    d = df.copy()
    d["symbol"] = sym
    # ADD Custom RSIs to df and d
    df["rsi_13"] = ta.momentum.rsi(df["close"], window=13).fillna(50.0)
    df["rsi_14"] = ta.momentum.rsi(df["close"], window=14).fillna(50.0)
    d["rsi_13"] = df["rsi_13"]
    d["rsi_14"] = df["rsi_14"]
    df_list.append(d)
mega_df = pd.concat(df_list)
mega_df["date"] = mega_df.index.date
first_candles = mega_df.groupby(["date", "symbol"]).first().reset_index()

daily_gaps = []
dates = first_candles["date"].unique()
dates = sorted(dates)
for i in range(1, len(dates)):
    prev_d = dates[i-1]; curr_d = dates[i]
    prev_day = mega_df[mega_df["date"] == prev_d]
    curr_day = first_candles[first_candles["date"] == curr_d]
    if prev_day.empty or curr_day.empty: continue
    last_closes = prev_day.groupby("symbol").last()["close"]
    first_opens = curr_day.set_index("symbol")["open"]
    merged = pd.concat([last_closes, first_opens], axis=1, join="inner")
    if merged.empty: continue
    gaps = (merged["open"] - merged["close"]) / merged["close"]
    strong = (gaps >= 0.005).sum()
    total = len(gaps)
    if total > 0 and (strong / total) >= 0.40:
        daily_gaps.append(curr_d)

strong40_days = set(daily_gaps)

def run_live_simulation(rsi_mode):
    per_slot = BUYING_POWER / MP
    positions = {}; trades = []
    
    for ts in timeline:
        d = ts.date()
        closed = []
        
        # 1. Manage open positions
        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos = positions[sym]
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]
            if qty <= 0: closed.append(sym); continue
            
            rsi0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0
            
            # Profit target check
            if not pos.get("partial_done", False):
                profit_now = (close - ep) / ep
                if profit_now >= PROFIT_TARGET:
                    sell_qty = max(1, int(qty * PARTIAL_FRAC))
                    if sell_qty >= qty: sell_qty = max(0, qty - 1)
                    if sell_qty > 0:
                        trades.append({"pnl": (close - ep) * sell_qty, "reason": "PARTIAL_PROFIT", "ep": ep, "qty": sell_qty, "ex": close})
                        pos["qty"] -= sell_qty
                        qty = pos["qty"]
                        pos["partial_done"] = True
                        if qty <= 0: closed.append(sym); continue
            
            # RSI exit check
            if rsi_mode == "14_LAG0_72":
                rsi_val = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0
                if rsi_val >= 72.0:
                    trades.append({"pnl": (close - ep) * qty, "reason": "RSI_EXIT", "ep": ep, "qty": qty, "ex": close})
                    closed.append(sym); continue
            else:
                rsi_val = df["rsi_13"].iloc[idx-1] if idx > 0 else 50.0
                if rsi_val >= 85.0:
                    trades.append({"pnl": (close - ep) * qty, "reason": "RSI_EXIT", "ep": ep, "qty": qty, "ex": close})
                    closed.append(sym); continue
                
            # Normal exits (Stop Loss, Square off, Sell Strategy)
            ex = None
            if low <= pos["sl"]: ex = min(pos["sl"], float(cc["open"]))
            elif ts.hour == 15 and ts.minute >= 15: ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df): ex = float(df.iloc[idx+1]["open"])
            if ex:
                trades.append({"pnl": (ex - ep) * qty, "reason": "NRM_EXIT", "ep": ep, "qty": qty, "ex": ex})
                closed.append(sym)
                
        for s in closed:
            if s in positions: del positions[s]
            
        # 2. Look for new entries
        if len(positions) >= MP: continue
        if ts.hour >= 15: continue
        if d not in strong40_days: continue
        
        for sym, df in stock_dfs.items():
            if len(positions) >= MP: break
            if sym in positions: continue # Only one concurrent position per stock, but multiple per day allowed (Rule 1 disabled)
            if ts not in stock_ts_map[sym]: continue
            idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced, pattern_df=sliced, ws_count=len(sliced))
            cond = evaluator._evaluate_conditions(buy_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                if idx+1 < len(df):
                    nxt = df.iloc[idx+1]
                    ep = float(nxt["open"])
                    qty = int(per_slot // ep)
                    if qty > 0:
                        sl_p = round_to_tick(ep * (1 - SL_PCT))
                        positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p, "partial_done": False}

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "reason": "EOD", "ep": pos["ep"], "qty": pos["qty"], "ex": lc})
    
    return trades

print("Running Live Configuration Simulation Comparison...")
results = []
for mode in ["14_LAG0_72", "13_LAG1_85"]:
    print(f"Testing {mode}...")
    trades = run_live_simulation(mode)
    df_t = pd.DataFrame(trades)
    if not df_t.empty:
        win = len(df_t[df_t["pnl"] > 0])
        tot = len(df_t)
        wr = win / tot * 100
        gross = df_t["pnl"].sum()
        df_t["stt"] = df_t["ex"] * df_t["qty"] * STT_PCT
        stt = df_t["stt"].sum()
        net = gross - stt
        results.append({
            "Mode": mode,
            "Total_Trades": tot,
            "Win_Rate": wr,
            "Gross_Return": gross/CAPITAL*100,
            "Est_STT": stt/CAPITAL*100,
            "NET_RETURN": net/CAPITAL*100
        })

print("\n" + "="*80)
res_df = pd.DataFrame(results)
print(res_df.to_string(index=False))
