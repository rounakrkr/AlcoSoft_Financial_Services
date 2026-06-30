import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from research.build_cache import load_cache
from core.strategy import CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext
from core.strategy_sets import load_strategy_sets
import ta

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
MP = 3
STT_PCT = 0.00035

# --- LONG CONFIG ---
L_SL_PCT = 0.010
L_PROFIT_TARGET = 0.005
L_PARTIAL_FRAC = 0.75
L_GAP_BREADTH = 0.40
L_GAP_THRESH = 0.010
L_EXCLUDE_GAP = -0.008
L_RSI_EXIT = 72.0

# --- SHORT CONFIG ---
S_SL_PCT = 0.010
S_GAP_BREADTH = 0.40
S_GAP_THRESH = -0.008
S_INDIV_GAP = -0.008
S_RSI_EXIT = 15.0
S_PROFIT_TARGET = 0.005
S_PARTIAL_FRAC = 0.75

config = load_strategy_sets()
buy_set_long = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
sell_set_long = next((s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)
buy_set_short = next((s for s in config.buy_sets if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN"), None)
sell_set_short = next((s for s in config.sell_sets if s.name == "SHORT_STREAK_MOMENTUM_RECOVERY"), None)

evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

stock_dfs = load_cache()

# Build custom RSIs correctly on the original dataframes
print("Building Custom RSIs...")
df_list = []
for sym, df in stock_dfs.items():
    # Long exit uses RSI 14
    df["rsi_14"] = ta.momentum.rsi(df["close"], window=14).fillna(50.0)
    # Short exit uses RSI 16
    df["rsi_16"] = ta.momentum.rsi(df["close"], window=16).fillna(50.0)
    df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9).fillna(method="bfill")
    
    d = df.copy()
    d["symbol"] = sym
    df_list.append(d)

mega_df = pd.concat(df_list)
mega_df["date"] = mega_df.index.date
first_candles = mega_df.groupby(["date", "symbol"]).first().reset_index()

dates = sorted(first_candles["date"].unique())
all_daily_gaps = {}

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
        all_daily_gaps[(curr_d, sym)] = float(gap_val)

timeline_set = set()
for sym, df in stock_dfs.items(): timeline_set.update(df.index)
timeline = sorted(list(timeline_set))
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

# Precompute Signals
pre_le = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
pre_lx = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
pre_se = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
pre_sx = {sym: [False]*len(df) for sym, df in stock_dfs.items()}

print("Precomputing signals...")
for sym, df in stock_dfs.items():
    for i in range(10, len(df)):
        sliced = df.iloc[:i+1]
        c_le = evaluator._evaluate_conditions(buy_set_long, StrategyEvaluationContext("buy", sliced, sliced, i+1))
        if c_le and all(r.get("fired") for r in c_le): pre_le[sym][i] = True
        c_lx = evaluator._evaluate_conditions(sell_set_long, StrategyEvaluationContext("sell", sliced, sliced, i+1))
        if c_lx and all(r.get("fired") for r in c_lx): pre_lx[sym][i] = True
        
        c_se = evaluator._evaluate_conditions(buy_set_short, StrategyEvaluationContext("buy", sliced, sliced, i+1))
        if c_se and all(r.get("fired") for r in c_se): pre_se[sym][i] = True
        c_sx = evaluator._evaluate_conditions(sell_set_short, StrategyEvaluationContext("sell", sliced, sliced, i+1))
        if c_sx and all(r.get("fired") for r in c_sx): pre_sx[sym][i] = True

def run_long_engine():
    bull_days = set()
    for i in range(1, len(dates)):
        curr_d = dates[i]
        gaps = [g for (d, s), g in all_daily_gaps.items() if d == curr_d]
        if not gaps: continue
        if sum(1 for g in gaps if g >= L_GAP_THRESH) / len(gaps) >= L_GAP_BREADTH:
            bull_days.add(curr_d)
            
    trades = []; positions = {}; per_slot = BUYING_POWER / MP
    
    for ts in timeline:
        current_date = ts.date()
        is_bull = current_date in bull_days
        syms_to_close = []
        
        for sym, pos in positions.items():
            if ts not in stock_ts_map.get(sym, {}): continue
            idx = stock_ts_map[sym][ts]; df = stock_dfs[sym]; cc = df.iloc[idx]
            cp, lp, op, hp = float(cc["close"]), float(cc["low"]), float(cc["open"]), float(cc["high"])
            
            if not pos.get("partial_done", False):
                p_now = (cp - pos["ep"]) / pos["ep"]
                if p_now >= L_PROFIT_TARGET:
                    sqty = max(1, int(pos["qty"] * L_PARTIAL_FRAC))
                    if sqty >= pos["qty"]: sqty = max(0, pos["qty"] - 1)
                    if sqty > 0:
                        trades.append({"sym": sym, "pnl": (cp - pos["ep"]) * sqty, "qty": sqty, "ex": cp, "type": "LONG_PARTIAL"})
                        pos["qty"] -= sqty; pos["partial_done"] = True
                        if pos["qty"] <= 0: syms_to_close.append(sym); continue
            
            if lp <= pos["sl"]:
                trades.append({"sym": sym, "pnl": (min(pos["sl"], op) - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": min(pos["sl"], op), "type": "LONG_SL"})
                syms_to_close.append(sym); continue
                
            if ts.hour == 15 and ts.minute >= 15:
                trades.append({"sym": sym, "pnl": (cp - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": cp, "type": "LONG_EOD"})
                syms_to_close.append(sym); continue
                
            rsi = df["rsi_14"].iloc[idx]
            if rsi >= L_RSI_EXIT:
                trades.append({"sym": sym, "pnl": (cp - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": cp, "type": "LONG_RSI"})
                syms_to_close.append(sym); continue
                
            if pre_lx[sym][idx] and idx+1 < len(df):
                ex_p = float(df.iloc[idx+1]["open"])
                trades.append({"sym": sym, "pnl": (ex_p - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": ex_p, "type": "LONG_STRAT"})
                syms_to_close.append(sym); continue
                
        for s in syms_to_close: del positions[s]
        
        if ts.hour >= 15: continue
        if not is_bull: continue
        
        for sym, df in stock_dfs.items():
            if len(positions) >= MP: break
            if sym in positions: continue
            if ts not in stock_ts_map[sym]: continue
            idx = stock_ts_map[sym][ts]
            gap_val = all_daily_gaps.get((current_date, sym), 0.0)
            
            if gap_val <= L_EXCLUDE_GAP: continue # EXCLUDE GAP DOWN
            
            if pre_le[sym][idx] and not pre_lx[sym][idx] and idx+1 < len(df):
                ep = float(df.iloc[idx+1]["open"])
                qty = int(per_slot // ep)
                if qty > 0:
                    positions[sym] = {"ep": ep, "qty": qty, "sl": ep * (1 - L_SL_PCT), "partial_done": False}

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"sym": sym, "pnl": (lc - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": lc, "type": "LONG_EOD"})
        
    return trades

def run_short_engine():
    bear_days = set()
    for i in range(1, len(dates)):
        curr_d = dates[i]
        gaps = [g for (d, s), g in all_daily_gaps.items() if d == curr_d]
        if not gaps: continue
        if sum(1 for g in gaps if g <= S_GAP_THRESH) / len(gaps) >= S_GAP_BREADTH:
            bear_days.add(curr_d)
            
    trades = []; positions = {}; per_slot = BUYING_POWER / MP
    
    for ts in timeline:
        current_date = ts.date()
        is_bear = current_date in bear_days
        syms_to_close = []
        
        for sym, pos in positions.items():
            if ts not in stock_ts_map.get(sym, {}): continue
            idx = stock_ts_map[sym][ts]; df = stock_dfs[sym]; cc = df.iloc[idx]
            cp, lp, op, hp = float(cc["close"]), float(cc["low"]), float(cc["open"]), float(cc["high"])
            
            # Check RSI Exit
            rsi = df["rsi_16"].iloc[idx]
            if rsi <= S_RSI_EXIT:
                trades.append({"sym": sym, "pnl": (pos["ep"] - cp) * pos["qty"], "qty": pos["qty"], "ex": cp, "type": "SHORT_RSI"})
                syms_to_close.append(sym); continue
                
            if not pos.get("partial_done", False):
                profit_now = (pos["ep"] - cp) / pos["ep"]
                if profit_now >= S_PROFIT_TARGET:
                    cover_qty = max(1, int(pos["qty"] * S_PARTIAL_FRAC))
                    if cover_qty >= pos["qty"]: cover_qty = max(0, pos["qty"] - 1)
                    if cover_qty > 0:
                        trades.append({"sym": sym, "pnl": (pos["ep"] - cp) * cover_qty, "qty": cover_qty, "ex": cp, "type": "SHORT_PARTIAL"})
                        pos["qty"] -= cover_qty
                        pos["partial_done"] = True
                        if pos["qty"] <= 0: syms_to_close.append(sym); continue
                        
            # Check Dynamic SL (c1 > h2)
            if idx >= 2:
                c1 = float(df["close"].iloc[idx-1])
                h2 = float(df["high"].iloc[idx-2])
                if c1 > h2:
                    ex_p = cp
                    if idx + 1 < len(df): ex_p = float(df["open"].iloc[idx+1])
                    trades.append({"sym": sym, "pnl": (pos["ep"] - ex_p) * pos["qty"], "qty": pos["qty"], "ex": ex_p, "type": "SHORT_DYN_SL_LAG1"})
                    syms_to_close.append(sym); continue
                
            if ts.hour == 15 and ts.minute >= 15:
                trades.append({"sym": sym, "pnl": (pos["ep"] - cp) * pos["qty"], "qty": pos["qty"], "ex": cp, "type": "SHORT_EOD"})
                syms_to_close.append(sym); continue
            
            if hp >= pos["sl"]:
                trades.append({"sym": sym, "pnl": (pos["ep"] - max(pos["sl"], op)) * pos["qty"], "qty": pos["qty"], "ex": max(pos["sl"], op), "type": "SHORT_SL"})
                syms_to_close.append(sym); continue
                
            if pre_sx[sym][idx] and idx+1 < len(df):
                ex_p = float(df.iloc[idx+1]["open"])
                trades.append({"sym": sym, "pnl": (pos["ep"] - ex_p) * pos["qty"], "qty": pos["qty"], "ex": ex_p, "type": "SHORT_STRAT"})
                syms_to_close.append(sym); continue
                
        for s in syms_to_close: del positions[s]
        
        if ts.hour >= 15: continue
        if not is_bear: continue
        
        for sym, df in stock_dfs.items():
            if len(positions) >= MP: break
            if sym in positions: continue
            if ts not in stock_ts_map[sym]: continue
            idx = stock_ts_map[sym][ts]
            gap_val = all_daily_gaps.get((current_date, sym), 0.0)
            
            if gap_val > S_INDIV_GAP: continue # ONLY GAP DOWN <= -0.8%
            
            if pre_se[sym][idx] and idx+1 < len(df):
                ep = float(df.iloc[idx+1]["open"])
                qty = int(per_slot // ep)
                if qty > 0:
                    positions[sym] = {"ep": ep, "qty": qty, "sl": ep * (1 + S_SL_PCT)}

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"sym": sym, "pnl": (pos["ep"] - lc) * pos["qty"], "qty": pos["qty"], "ex": lc, "type": "SHORT_EOD"})
        
    return trades

print("\n--- RUNNING FINAL LONG ENGINE ---")
# long_trades = run_long_engine()
print("--- RUNNING FINAL SHORT ENGINE ---")
short_trades = run_short_engine()

df_s = pd.DataFrame(short_trades)
if not df_s.empty:
    df_s["stt"] = df_s["ex"] * df_s["qty"] * STT_PCT
    net = df_s["pnl"].sum() - df_s["stt"].sum()
    print(f"SHORT NET: {net/CAPITAL*100:.2f}%")


# Analyze Results
def analyze(trades, name):
    df_t = pd.DataFrame(trades)
    if df_t.empty:
        return f"{name} Engine:\n  No Trades Executed\n"
        
    tot = len(df_t)
    wins = len(df_t[df_t["pnl"] > 0])
    wr = wins / tot * 100
    gross = df_t["pnl"].sum()
    df_t["stt"] = df_t["ex"] * df_t["qty"] * STT_PCT
    stt = df_t["stt"].sum()
    net = gross - stt
    
    return (f"{name} Engine:\n"
            f"  Total Trades : {tot}\n"
            f"  Win Rate     : {wr:.1f}%\n"
            f"  Gross Return : {gross/CAPITAL*100:.2f}%\n"
            f"  STT Tax Est. : {stt/CAPITAL*100:.2f}%\n"
            f"  NET RETURN   : {net/CAPITAL*100:.2f}%\n")


