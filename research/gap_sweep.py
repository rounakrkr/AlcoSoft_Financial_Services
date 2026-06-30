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

def prev_high_break(ctx):
    df = ctx.indicator_df
    if len(df)<3: return {"fired": False}
    c1=df["close"].iloc[-2]; h2=df["high"].iloc[-3]
    return {"fired": bool(c1 > h2)}

IND_GAPS = [-0.004, -0.006, -0.008, -0.010, -0.012]
MKT_GAPS = [0.30, 0.40, 0.50, 0.60]

def run_gap_sweep(ind_gap_thr, mkt_gap_thr):
    # Determine valid market days based on mkt_gap_thr and a fixed base gap (e.g. -0.005 for market breadth)
    # The user asked: "gap x gap percent 30~75% of stocks". 
    # Usually breadth is defined as % of stocks that gapped down by ANY amount, or by a fixed amount?
    # Let's use the individual gap threshold to calculate breadth!
    valid_days = set()
    for i in range(1, len(dates)):
        curr_d = dates[i]
        daily_gaps = [g for (d, sym), g in all_gaps.items() if d == curr_d]
        if not daily_gaps: continue
        strong = sum(1 for g in daily_gaps if g <= ind_gap_thr)
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
            close = float(cc["close"]); high = float(cc["high"])
            ep = pos["ep"]; qty = pos["qty"]
            if qty <= 0: closed.append(sym); continue
            
            rsi0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0
            
            if not pos.get("partial_done", False):
                if (ep - close) / ep >= PROFIT_TARGET:
                    cover_qty = max(1, int(qty * PARTIAL_FRAC))
                    if cover_qty >= qty: cover_qty = max(0, qty - 1)
                    if cover_qty > 0:
                        trades.append({"pnl": (ep - close) * cover_qty, "reason": "PARTIAL", "ep": ep, "qty": cover_qty})
                        pos["qty"] -= cover_qty; qty = pos["qty"]
                        pos["partial_done"] = True
                        if qty <= 0: closed.append(sym); continue
            
            # Use RSI(14) Lag0 Thr<=25 as the standard for this test
            if rsi0 <= 25.0:
                trades.append({"pnl": (ep - close) * qty, "reason": "RSI", "ep": ep, "qty": qty})
                closed.append(sym); continue
                
            ex = None; reason = None
            if high >= pos["sl"]:
                ex = max(pos["sl"], float(cc["open"]))
                reason = "SL"
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close
                reason = "TIME"
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                if prev_high_break(ctx)["fired"]:
                    if idx+1 < len(df): 
                        ex = float(df.iloc[idx+1]["open"])
                        reason = "DYN"
            if ex:
                trades.append({"pnl": (ep - ex) * qty, "reason": reason, "ep": ep, "qty": qty})
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
            if gap_pct > ind_gap_thr: continue # INDIVIDUAL STOCK MUST MEET THRESHOLD
            
            if ts not in stock_ts_map[sym]: continue
            idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced, pattern_df=sliced, ws_count=len(sliced))
            cond = evaluator._evaluate_conditions(short_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                c_ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                if prev_high_break(c_ctx)["fired"]: continue
                
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"])
                    qty = int(per_slot // ep)
                    if qty > 0:
                        sl_p = round_to_tick(ep * (1 + SL_PCT))
                        positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p, "partial_done": False}

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (pos["ep"] - lc) * pos["qty"], "reason": "EOD", "ep": pos["ep"], "qty": pos["qty"]})
    
    return trades

results = []
print("Starting Gap Sweep...")
for ig in IND_GAPS:
    for mg in MKT_GAPS:
        name = f"IndGap<={ig*100:.1f}% | Breadth>={mg*100:.0f}%"
        print(f"Testing {name}...")
        trades = run_gap_sweep(ig, mg)
        df_t = pd.DataFrame(trades)
        if df_t.empty: continue
        
        # Calculate STT: 0.035% of selling amount. For short selling, entry is the sell side.
        df_t["stt"] = df_t["ep"] * df_t["qty"] * 0.00035
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
print("GAP SWEEP RESULTS (Sorted by Gross Return)")
print("="*80)
print(res_df.to_string(index=False))
