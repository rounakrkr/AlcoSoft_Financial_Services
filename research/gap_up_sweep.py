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

# Precompute gaps for all days and stocks
all_gaps = {} # (date, symbol) -> gap_pct
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
        all_gaps[(curr_d, sym)] = float(gap_val)

IND_GAPS_UP = [0.004, 0.006, 0.008, 0.010, 0.012]
MKT_GAPS_UP = [0.30, 0.40, 0.50, 0.60]

def run_gap_up_sweep(ind_gap_thr, mkt_gap_thr):
    valid_days = set()
    for i in range(1, len(dates)):
        curr_d = dates[i]
        daily_gaps = [g for (d, sym), g in all_gaps.items() if d == curr_d]
        if not daily_gaps: continue
        strong = sum(1 for g in daily_gaps if g >= ind_gap_thr)
        total = len(daily_gaps)
        if total > 0 and (strong / total) >= mkt_gap_thr:
            valid_days.add(curr_d)

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
            close = float(cc["close"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]
            if qty <= 0: closed.append(sym); continue
            
            rsi0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0
            
            if not pos.get("partial_done", False):
                if (close - ep) / ep >= PROFIT_TARGET:
                    cover_qty = max(1, int(qty * PARTIAL_FRAC))
                    if cover_qty >= qty: cover_qty = max(0, qty - 1)
                    if cover_qty > 0:
                        trades.append({"pnl": (close - ep) * cover_qty, "reason": "PARTIAL", "ex": close, "qty": cover_qty})
                        pos["qty"] -= cover_qty; qty = pos["qty"]
                        pos["partial_done"] = True
                        if qty <= 0: closed.append(sym); continue
            
            # Using standard long exit logic (e.g. RSI >= 72 from earlier knowledge)
            if rsi0 >= 72.0:
                trades.append({"pnl": (close - ep) * qty, "reason": "RSI", "ex": close, "qty": qty})
                closed.append(sym); continue
                
            ex = None; reason = None
            if low <= pos["sl"]:
                ex = min(pos["sl"], float(cc["open"]))
                reason = "SL"
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close
                reason = "TIME"
            else:
                c_ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                # DYN_EXIT for Longs: Usually it's close below EMA21.
                # Let's use standard from the config.
                c1 = float(df["close"].iloc[-2]); e21 = float(df["ema21"].iloc[-2])
                if c1 < e21:
                    if idx+1 < len(df):
                        ex = float(df.iloc[idx+1]["open"])
                        reason = "DYN"

            if ex:
                trades.append({"pnl": (ex - ep) * qty, "reason": reason, "ex": ex, "qty": qty})
                closed.append(sym)
                
        for s in closed:
            if s in positions: del positions[s]
            
        if len(positions) >= MP: continue
        if ts.hour >= 15: continue
        if d not in valid_days: continue
        
        for sym, df in stock_dfs.items():
            if len(positions) >= MP: break
            if sym in positions: continue
            gap_pct = all_gaps.get((d, sym), 0.0)
            if gap_pct < ind_gap_thr: continue # INDIVIDUAL STOCK MUST MEET GAP UP THRESHOLD
            
            if ts not in stock_ts_map[sym]: continue
            idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced, pattern_df=sliced, ws_count=len(sliced))
            cond = evaluator._evaluate_conditions(long_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                # Ensure it doesn't immediately DYN exit
                c1 = float(df["close"].iloc[-2]); e21 = float(df["ema21"].iloc[-2])
                if c1 < e21: continue
                
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"])
                    qty = int(per_slot // ep)
                    if qty > 0:
                        sl_p = round_to_tick(ep * (1 - SL_PCT))
                        positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p, "partial_done": False}

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "reason": "EOD", "ex": lc, "qty": pos["qty"]})
    
    return trades

results = []
print("Starting LONG Gap Up Sweep...")
for ig in IND_GAPS_UP:
    for mg in MKT_GAPS_UP:
        name = f"IndGap>={ig*100:.1f}% | Breadth>={mg*100:.0f}%"
        print(f"Testing {name}...")
        trades = run_gap_up_sweep(ig, mg)
        df_t = pd.DataFrame(trades)
        if df_t.empty: continue
        
        # Calculate STT: 0.035% of SELLING amount. For Long Buying, exit is the sell side.
        df_t["stt"] = df_t["ex"] * df_t["qty"] * 0.00035
        total_stt = df_t["stt"].sum()
        
        win = len(df_t[df_t["pnl"] > 0])
        tot = len(df_t)
        wr = win / tot * 100
        gross = df_t["pnl"].sum()
        gross_pct = gross / CAPITAL * 100
        net_pct = (gross - total_stt) / CAPITAL * 100
        
        results.append({
            "Name": name,
            "Trades": tot,
            "WR": wr,
            "Gross_Pct": gross_pct,
            "Net_Pct": net_pct
        })

res_df = pd.DataFrame(results).sort_values("Gross_Pct", ascending=False)
print("\n" + "="*80)
print("LONG GAP UP SWEEP RESULTS (Sorted by Gross Return)")
print("="*80)
print(res_df.to_string(index=False))
