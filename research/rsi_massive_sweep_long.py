import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from research.build_cache import load_cache
from core.strategy import (StrategyEvaluationContext, StrategySetEvaluator, CONDITION_REGISTRY)
from core.strategy_sets import load_strategy_sets
from core.order_executor import round_to_tick

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
MP = 3; SL_PCT = 0.010
PROFIT_TARGET = 0.005
PARTIAL_FRAC = 0.75

print("Loading cache...")
stock_dfs = load_cache()

config = load_strategy_sets()
long_set_def = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
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

# Baseline Market Gap for Long: 30% of stocks gap up >= 0.8%
# Baseline Individual Gap for Long: Only buy if stock gaps up >= 0.8%
IND_GAP_BASE = 0.008
all_gaps = {}
daily_gaps = []
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
    for sym, gap_val in gaps.items(): all_gaps[(curr_d, sym)] = float(gap_val)
    strong = (gaps >= IND_GAP_BASE).sum()
    total = len(gaps)
    if total > 0 and (strong / total) >= 0.30:
        daily_gaps.append(curr_d)

strong_up_days = set(daily_gaps)

# Sweep Configuration
RSI_LENGTHS = list(range(8, 26)) # 8 to 25
RSI_LAGS = [0, 1] 
# For Long, RSI threshold is usually high (overbought). E.g. 50 to 90.
RSI_THRESHOLDS = list(range(50, 91)) # 50 to 90

print("Precomputing all RSI lengths...")
for rsi_len in RSI_LENGTHS:
    rsi_col = f"rsi_{rsi_len}"
    for sym, df in stock_dfs.items():
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=rsi_len).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_len).mean()
        rs = gain / loss
        df[rsi_col] = 100 - (100 / (1 + rs))

print("Precomputing all entry signals...")
precomputed_entries = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}
for sym, df in stock_dfs.items():
    for idx in range(10, len(df)):
        sliced = df.iloc[:idx+1]
        ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced, pattern_df=sliced, ws_count=len(sliced))
        cond = evaluator._evaluate_conditions(long_set_def, ctx)
        if cond and all(r.get("fired") for r in cond):
            # Check standard DYN_EXIT to ensure we don't instantly exit
            c1 = float(sliced["close"].iloc[-2]); e21 = float(sliced["ema21"].iloc[-2])
            if c1 >= e21:
                precomputed_entries[sym][idx] = True

def run_rsi_sweep_long(rsi_len, rsi_lag, rsi_thr):
    per_slot = BUYING_POWER / MP
    positions = {}; trades = []
    rsi_col = f"rsi_{rsi_len}"
    
    for ts in timeline:
        d = ts.date()
        closed = []
        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos = positions[sym]
            close = float(cc["close"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]
            if qty <= 0: closed.append(sym); continue
            
            try:
                if rsi_lag == 0:
                    rsi_val = float(cc[rsi_col])
                else:
                    rsi_val = float(df[rsi_col].iloc[idx-1])
            except:
                rsi_val = 50.0
            if pd.isna(rsi_val): rsi_val = 50.0
            
            if not pos.get("partial_done", False):
                if (close - ep) / ep >= PROFIT_TARGET:
                    cover_qty = max(1, int(qty * PARTIAL_FRAC))
                    if cover_qty >= qty: cover_qty = max(0, qty - 1)
                    if cover_qty > 0:
                        trades.append({"pnl": (close - ep) * cover_qty, "ex": close, "qty": cover_qty, "reason": "PARTIAL_PROFIT"})
                        pos["qty"] -= cover_qty; qty = pos["qty"]
                        pos["partial_done"] = True
                        if qty <= 0: closed.append(sym); continue
            
            # Long Exit: RSI goes HIGH
            if rsi_val >= rsi_thr:
                trades.append({"pnl": (close - ep) * qty, "ex": close, "qty": qty, "reason": "RSI_EXIT"})
                closed.append(sym); continue
                
            ex = None; reason = None
            if low <= pos["sl"]:
                ex = min(pos["sl"], float(cc["open"]))
                reason = "SL_EXIT"
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close
                reason = "TIME_EXIT"
            else:
                # LONG DYN EXIT: Close falls below EMA21
                c1 = float(sliced["close"].iloc[-2]); e21 = float(sliced["ema21"].iloc[-2])
                if c1 < e21:
                    if idx+1 < len(df): 
                        ex = float(df.iloc[idx+1]["open"])
                        reason = "DYN_EXIT"
            if ex:
                trades.append({"pnl": (ex - ep) * qty, "ex": ex, "qty": qty, "reason": reason})
                closed.append(sym)
                
        for s in closed:
            if s in positions: del positions[s]
            
        if len(positions) >= MP: continue
        if ts.hour >= 15: continue
        if d not in strong_up_days: continue
        
        for sym, df in stock_dfs.items():
            if len(positions) >= MP: break
            if sym in positions: continue
            
            gap_pct = all_gaps.get((d, sym), 0.0)
            if gap_pct < IND_GAP_BASE: continue
            
            if ts not in stock_ts_map[sym]: continue
            idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            
            if precomputed_entries[sym][idx]:
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"])
                    qty = int(per_slot // ep)
                    if qty > 0:
                        sl_p = round_to_tick(ep * (1 - SL_PCT))
                        positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p, "partial_done": False}

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "ex": lc, "qty": pos["qty"], "reason": "EOD"})
    
    return trades

results = []
print(f"Starting Massive RSI Sweep for LONG (Total tests: {len(RSI_LENGTHS) * len(RSI_LAGS) * len(RSI_THRESHOLDS)})...")
import time
start_t = time.time()
counter = 0
for rsi_len in RSI_LENGTHS:
    for rsi_lag in RSI_LAGS:
        for rsi_thr in RSI_THRESHOLDS:
            name = f"RSI({rsi_len})_Lag{rsi_lag}_Thr>={rsi_thr}"
            trades = run_rsi_sweep_long(rsi_len, rsi_lag, rsi_thr)
            counter += 1
            if counter % 50 == 0:
                print(f"Finished {counter} tests in {time.time() - start_t:.1f}s...")
            df_t = pd.DataFrame(trades)
            if df_t.empty: continue
            
            df_t["stt"] = df_t["ex"] * df_t["qty"] * 0.00035
            total_stt = df_t["stt"].sum()
            
            win = len(df_t[df_t["pnl"] > 0])
            tot = len(df_t)
            wr = win / tot * 100
            gross = df_t["pnl"].sum()
            gross_pct = gross / CAPITAL * 100
            net_pct = (gross - total_stt) / CAPITAL * 100
            
            rsi_exits = len(df_t[df_t["reason"] == "RSI_EXIT"])
            
            results.append({
                "Name": name,
                "Trades": tot,
                "WR": wr,
                "Gross_Pct": gross_pct,
                "Net_Pct": net_pct,
                "RSI_Hits": rsi_exits
            })

res_df = pd.DataFrame(results).sort_values("Gross_Pct", ascending=False)
print("\n" + "="*80)
print("MASSIVE RSI SWEEP RESULTS LONG (Top 20, Sorted by Gross Return)")
print("="*80)
print(res_df.head(20).to_string(index=False))
