import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import logging, warnings
warnings.filterwarnings("ignore")

from research.build_cache import load_cache
from core.strategy import (StrategyEvaluationContext, StrategySetEvaluator, CONDITION_REGISTRY)
from core.strategy_sets import load_strategy_sets
from core.order_executor import round_to_tick

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
MP = 3; SL_PCT = 0.010
PROFIT_TARGET = 0.005
PARTIAL_FRAC = 0.75
RSI_EXIT_THR = 28.0

print("Loading cache...")
stock_dfs = load_cache()

config = load_strategy_sets()
short_set_def = next((s for s in config.buy_sets if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN"), None)
evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

timeline_set = set()
for sym, df in stock_dfs.items(): timeline_set.update(df.index)
timeline = sorted(list(timeline_set))
stock_ts_map = {}
for sym, df in stock_dfs.items(): stock_ts_map[sym] = {ts: i for i, ts in enumerate(df.index)}

df_list = []
for sym, df in stock_dfs.items():
    d = df.copy(); d["symbol"] = sym; df_list.append(d)
mega_df = pd.concat(df_list)
mega_df["date"] = mega_df.index.date
first_candles = mega_df.groupby(["date", "symbol"]).first().reset_index()

daily_gaps = []
symbol_daily_gaps = {} # Map (date, symbol) -> gap_pct
dates = sorted(first_candles["date"].unique())
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
    
    for sym, gap_val in gaps.items():
        symbol_daily_gaps[(curr_d, sym)] = float(gap_val)
        
    strong = (gaps <= -0.005).sum()
    total = len(gaps)
    if total > 0 and (strong / total) >= 0.40:
        daily_gaps.append(curr_d)

strong40_down_days = set(daily_gaps)

def prev_high_break(ctx):
    df = ctx.indicator_df
    if len(df)<3: return {"fired": False}
    c1=df["close"].iloc[-2]; h2=df["high"].iloc[-3]
    return {"fired": bool(c1 > h2)}

def ema9_lag0(ctx):
    df = ctx.indicator_df
    if len(df)<2: return {"fired": False}
    c0=df["close"].iloc[-1]; c1=df["close"].iloc[-2]; e9=df["ema9"].iloc[-1]
    return {"fired": bool(c1 <= e9 and c0 > e9)}

CONDITIONS = [
    ("PREV_CANDLE_HIGH_LAG1", prev_high_break),
    ("EMA9_LAG0", ema9_lag0)
]

def run_deep_dive(cond_func):
    per_slot = BUYING_POWER / MP
    positions = {}; trades = []
    
    for ts in timeline:
        d = ts.date()
        closed = []
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
            
            if not pos.get("partial_done", False):
                if (ep - close) / ep >= PROFIT_TARGET:
                    cover_qty = max(1, int(qty * PARTIAL_FRAC))
                    if cover_qty >= qty: cover_qty = max(0, qty - 1)
                    if cover_qty > 0:
                        trades.append({"symbol": sym, "date": d, "gap": pos["gap"], "pnl": (ep - close) * cover_qty, "reason": "PARTIAL_PROFIT"})
                        pos["qty"] -= cover_qty; qty = pos["qty"]
                        pos["partial_done"] = True
                        if qty <= 0: closed.append(sym); continue
            
            if rsi0 <= RSI_EXIT_THR:
                trades.append({"symbol": sym, "date": d, "gap": pos["gap"], "pnl": (ep - close) * qty, "reason": "RSI_EXIT"})
                closed.append(sym); continue
                
            ex = None; reason = None
            if high >= pos["sl"]:
                ex = max(pos["sl"], float(cc["open"]))
                reason = "SL_EXIT"
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close
                reason = "TIME_EXIT"
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                if cond_func(ctx)["fired"]:
                    if idx+1 < len(df): 
                        ex = float(df.iloc[idx+1]["open"])
                        reason = "DYN_EXIT"
            if ex:
                trades.append({"symbol": sym, "date": d, "gap": pos["gap"], "pnl": (ep - ex) * qty, "reason": reason})
                closed.append(sym)
                
        for s in closed:
            if s in positions: del positions[s]
            
        if len(positions) >= MP: continue
        if ts.hour >= 15: continue
        if d not in strong40_down_days: continue
        
        for sym, df in stock_dfs.items():
            if len(positions) >= MP: break
            if sym in positions: continue
            if ts not in stock_ts_map[sym]: continue
            idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced, pattern_df=sliced, ws_count=len(sliced))
            cond = evaluator._evaluate_conditions(short_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                c_ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                if cond_func(c_ctx)["fired"]: continue
                
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"])
                    qty = int(per_slot // ep)
                    if qty > 0:
                        sl_p = round_to_tick(ep * (1 + SL_PCT))
                        gap_pct = symbol_daily_gaps.get((d, sym), 0.0)
                        positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p, "gap": gap_pct, "partial_done": False}

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"symbol": sym, "date": list(positions.keys())[0], "gap": pos["gap"], "pnl": (pos["ep"] - lc) * pos["qty"], "reason": "EOD"})
    
    return trades

for name, func in CONDITIONS:
    print(f"\n=======================================================")
    print(f"DEEP DIVE: {name}")
    print(f"=======================================================")
    trades = run_deep_dive(func)
    df_t = pd.DataFrame(trades)
    
    # Analysis
    for reason in ["RSI_EXIT", "DYN_EXIT", "PARTIAL_PROFIT"]:
        sub_df = df_t[df_t["reason"] == reason]
        if sub_df.empty: continue
        
        total_count = len(sub_df)
        gapped_down = len(sub_df[sub_df["gap"] <= -0.005])
        gap_pct = (gapped_down / total_count) * 100
        avg_gap = sub_df["gap"].mean() * 100
        
        profit = sub_df["pnl"].sum() / CAPITAL * 100
        
        print(f"\n[{reason}]")
        print(f"  - Total Occurrences: {total_count}")
        print(f"  - Gross Return:      {profit:+.2f}%")
        print(f"  - Individually Gapped Down (<-0.5%): {gapped_down} out of {total_count} ({gap_pct:.1f}%)")
        print(f"  - Average Gap % of these stocks: {avg_gap:.2f}%")
        
        # Win rate for this specific exit
        win = len(sub_df[sub_df["pnl"] > 0])
        print(f"  - Win Rate for this exit: {win/total_count*100:.1f}%")
