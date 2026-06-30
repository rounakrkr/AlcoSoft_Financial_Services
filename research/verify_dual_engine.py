import sys, os
import pandas as pd
import ta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.build_cache import load_cache
from core.strategy_sets import load_strategy_sets
from core.strategy import StrategySetEvaluator, StrategyEvaluationContext, CONDITION_REGISTRY

# System Settings
CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
MP = 3
STT_PCT = 0.00035

# LONG ENGINE SETTINGS
L_MKT_THRESH = 0.010
L_MKT_BREADTH = 0.40
L_EXCLUDE_GAP = -0.008
L_RSI_EXIT = 72.0
L_PROFIT_TARGET = 0.005
L_PARTIAL_FRAC = 0.75
L_SL_PCT = 0.010

# SHORT ENGINE SETTINGS
S_MKT_THRESH = -0.006
S_MKT_BREADTH = 0.40
S_INDIV_GAP = -0.008
S_RSI_EXIT = 15.0
S_PROFIT_TARGET = 0.005
S_PARTIAL_FRAC = 0.75

config = load_strategy_sets()
buy_set_long = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
buy_set_short = next((s for s in config.buy_sets if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN"), None)

evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
stock_dfs = load_cache()

print("Building Custom RSIs...")
for sym, df in stock_dfs.items():
    df["rsi_14"] = ta.momentum.rsi(df["close"], window=14).fillna(50.0)
    df["rsi_16"] = ta.momentum.rsi(df["close"], window=16).fillna(50.0)

all_daily_gaps = {}
first_opens = {}
last_closes = {}
for sym, df in stock_dfs.items():
    if "open" not in df.columns: continue
    daily_first = df.groupby(df.index.date).first()
    daily_last = df.groupby(df.index.date).last()
    first_opens[sym] = daily_first["open"]
    last_closes[sym] = daily_last["close"].shift(1)

last_closes_df = pd.DataFrame(last_closes)
first_opens_df = pd.DataFrame(first_opens)
merged = pd.concat([last_closes_df, first_opens_df], axis=1, join="inner")
for sym in stock_dfs:
    if sym in last_closes_df and sym in first_opens_df:
        gaps = (first_opens_df[sym] - last_closes_df[sym]) / last_closes_df[sym]
        for d, gap_val in gaps.items():
            all_daily_gaps[(d, sym)] = float(gap_val)

timeline_set = set()
for sym, df in stock_dfs.items(): timeline_set.update(df.index)
timeline = sorted(list(timeline_set))
dates = sorted(list(set(d for d, s in all_daily_gaps.keys())))
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

pre_le = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
pre_se = {sym: [False]*len(df) for sym, df in stock_dfs.items()}

print("Precomputing signals (this takes a moment)...")
for sym, df in stock_dfs.items():
    for i in range(10, len(df)):
        sliced = df.iloc[:i+1]
        c_le = evaluator._evaluate_conditions(buy_set_long, StrategyEvaluationContext("buy", sliced, sliced, i+1))
        if c_le and all(r.get("fired") for r in c_le): pre_le[sym][i] = True
        
        c_se = evaluator._evaluate_conditions(buy_set_short, StrategyEvaluationContext("buy", sliced, sliced, i+1))
        if c_se and all(r.get("fired") for r in c_se): pre_se[sym][i] = True

def run_long_engine():
    bull_days = set()
    for i in range(1, len(dates)):
        curr_d = dates[i]
        gaps = [g for (d, s), g in all_daily_gaps.items() if d == curr_d]
        if not gaps: continue
        if sum(1 for g in gaps if g >= L_MKT_THRESH) / len(gaps) >= L_MKT_BREADTH:
            bull_days.add(curr_d)
            
    trades = []; positions = {}; per_slot = BUYING_POWER / MP
    for ts in timeline:
        d = ts.date()
        syms_to_close = []
        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            idx = stock_ts_map[sym][ts]; df = stock_dfs[sym]; cc = df.iloc[idx]
            cp, lp, op, hp = float(cc["close"]), float(cc["low"]), float(cc["open"]), float(cc["high"])
            pos = positions[sym]
            
            rsi = df["rsi_14"].iloc[idx]
            if rsi >= L_RSI_EXIT:
                trades.append({"sym": sym, "pnl": (cp - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": cp, "type": "LONG_RSI"})
                syms_to_close.append(sym); continue
                
            if not pos.get("partial_done", False):
                profit_now = (cp - pos["ep"]) / pos["ep"]
                if profit_now >= L_PROFIT_TARGET:
                    cover_qty = max(1, int(pos["qty"] * L_PARTIAL_FRAC))
                    if cover_qty >= pos["qty"]: cover_qty = max(0, pos["qty"] - 1)
                    if cover_qty > 0:
                        trades.append({"sym": sym, "pnl": (cp - pos["ep"]) * cover_qty, "qty": cover_qty, "ex": cp, "type": "LONG_PARTIAL"})
                        pos["qty"] -= cover_qty
                        pos["partial_done"] = True
                        if pos["qty"] <= 0: syms_to_close.append(sym); continue
                        
            if lp <= pos["sl"]:
                ex_p = max(pos["sl"], op)
                trades.append({"sym": sym, "pnl": (ex_p - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": ex_p, "type": "LONG_SL"})
                syms_to_close.append(sym); continue
                
            if ts.hour == 15 and ts.minute >= 15:
                trades.append({"sym": sym, "pnl": (cp - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": cp, "type": "LONG_EOD"})
                syms_to_close.append(sym); continue
                
        for s in syms_to_close: del positions[s]
            
        if len(positions) >= MP: continue
        if ts.hour >= 15: continue
        if d not in bull_days: continue
        
        for sym in stock_dfs:
            if len(positions) >= MP: break
            if sym in positions: continue
            if all_daily_gaps.get((d, sym), 0.0) <= L_EXCLUDE_GAP: continue
            if not pre_le[sym][stock_ts_map[sym][ts]]: continue
            
            idx = stock_ts_map[sym][ts]; df = stock_dfs[sym]
            if idx + 1 < len(df):
                ep = float(df.iloc[idx+1]["open"])
                qty = int(per_slot // ep)
                if qty > 0:
                    positions[sym] = {"ep": ep, "qty": qty, "sl": ep * (1 - L_SL_PCT)}
                    
    return trades

def run_short_engine():
    bear_days = set()
    for i in range(1, len(dates)):
        curr_d = dates[i]
        gaps = [g for (d, s), g in all_daily_gaps.items() if d == curr_d]
        if not gaps: continue
        if sum(1 for g in gaps if g <= S_MKT_THRESH) / len(gaps) >= S_MKT_BREADTH:
            bear_days.add(curr_d)
            
    trades = []; positions = {}; per_slot = BUYING_POWER / MP
    for ts in timeline:
        d = ts.date()
        syms_to_close = []
        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            idx = stock_ts_map[sym][ts]; df = stock_dfs[sym]; cc = df.iloc[idx]
            cp, lp, op, hp = float(cc["close"]), float(cc["low"]), float(cc["open"]), float(cc["high"])
            pos = positions[sym]
            
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
                
        for s in syms_to_close: del positions[s]
            
        if len(positions) >= MP: continue
        if ts.hour >= 15: continue
        if d not in bear_days: continue
        
        for sym in stock_dfs:
            if len(positions) >= MP: break
            if sym in positions: continue
            if all_daily_gaps.get((d, sym), 0.0) > S_INDIV_GAP: continue
            if not pre_se[sym][stock_ts_map[sym][ts]]: continue
            
            idx = stock_ts_map[sym][ts]; df = stock_dfs[sym]
            if idx + 1 < len(df):
                ep = float(df.iloc[idx+1]["open"])
                qty = int(per_slot // ep)
                if qty > 0:
                    positions[sym] = {"ep": ep, "qty": qty}
                    
    return trades

def analyze(trades_list, label):
    if not trades_list: return f"{label}: No trades"
    df_t = pd.DataFrame(trades_list)
    tot = len(df_t)
    wins = len(df_t[df_t["pnl"] > 0])
    wr = wins / tot * 100
    gross = df_t["pnl"].sum()
    stt = (df_t["ex"] * df_t["qty"] * STT_PCT).sum()
    net = gross - stt
    n_pct = net / CAPITAL * 100
    return f"{label:<10} | Trades: {tot:<4} | WinRate: {wr:>5.1f}% | Gross: {gross/CAPITAL*100:>6.2f}% | STT: {-stt/CAPITAL*100:>6.2f}% | NET: {n_pct:>+6.2f}%"

long_trades = run_long_engine()
short_trades = run_short_engine()

print("\n" + "="*80)
print("VERIFIED DUAL-ENGINE STANDALONE OUTPUT")
print("="*80)
print(analyze(long_trades, "LONG"))
print(analyze(short_trades, "SHORT"))
print("="*80)
