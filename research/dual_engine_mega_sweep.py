import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from research.build_cache import load_cache
from core.strategy import CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext
from core.strategy_sets import load_strategy_sets
from core.order_executor import round_to_tick
import ta
import time

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
SL_PCT = 0.010
PROFIT_TARGET = 0.005
PARTIAL_FRAC = 0.75
STT_PCT = 0.00035

config = load_strategy_sets()
buy_set_long = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
sell_set_long = next((s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)

# Fix Short strategy sets to use RSI(16) <= 28
buy_set_short = next((s for s in config.buy_sets if s.name == "SHORT_GAP_MOMENTUM"), None)
sell_set_short = next((s for s in config.sell_sets if s.name == "SHORT_EXIT_MOMENTUM"), None)

evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

stock_dfs = load_cache()

# Add standard custom RSIs to dataframe efficiently
df_list = []
for sym, df in stock_dfs.items():
    d = df.copy()
    d["symbol"] = sym
    # For Long exit
    d["rsi_13"] = ta.momentum.rsi(d["close"], window=13).fillna(50.0)
    # For Short exit
    d["rsi_16"] = ta.momentum.rsi(d["close"], window=16).fillna(50.0)
    df_list.append(d)
mega_df = pd.concat(df_list)
mega_df["date"] = mega_df.index.date
first_candles = mega_df.groupby(["date", "symbol"]).first().reset_index()

dates = sorted(first_candles["date"].unique())
all_daily_gaps = {}
daily_gap_df = []

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

# Precompute Long Entries/Exits
pre_le = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
pre_lx = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
# Precompute Short Entries/Exits
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

def run_mega_sweep():
    gap_thresholds = [0.004, 0.006, 0.008, 0.010]
    breadth_thresholds = [0.30, 0.40, 0.50, 0.60]
    indiv_filters = ["ALL", "EXCLUDE_GAP_DOWN", "ONLY_GAP_UP"]
    dual_mps = [3, 4, 5, 6, 7]
    
    results = []
    total_runs = len(gap_thresholds) * len(breadth_thresholds) * len(indiv_filters) * len(dual_mps)
    print(f"Total Sweep Combinations: {total_runs}")
    
    run_idx = 0
    for gap_th in gap_thresholds:
        for b_th in breadth_thresholds:
            
            # Determine Bull and Bear days
            bull_days = set(); bear_days = set()
            for i in range(1, len(dates)):
                curr_d = dates[i]
                gaps = [g for (d, s), g in all_daily_gaps.items() if d == curr_d]
                if not gaps: continue
                # Bull: x% stocks >= gap_th
                if sum(1 for g in gaps if g >= gap_th) / len(gaps) >= b_th:
                    bull_days.add(curr_d)
                # Bear: 40% stocks <= -0.004 (Fixed as per original short strategy base)
                if sum(1 for g in gaps if g <= -0.004) / len(gaps) >= 0.40:
                    bear_days.add(curr_d)
                    
            for ifilter in indiv_filters:
                for dual_mp in dual_mps:
                    run_idx += 1
                    if run_idx % 10 == 0: print(f"  Progress: {run_idx}/{total_runs}", end="\r")
                    
                    trades = []
                    positions_long = {}
                    positions_short = {}
                    
                    for ts in timeline:
                        current_date = ts.date()
                        is_bull = current_date in bull_days
                        is_bear = current_date in bear_days
                        
                        if not is_bull and not is_bear:
                            continue
                            
                        # Set dynamic MP
                        mp_long = dual_mp if (is_bull and is_bear) else (3 if is_bull else 0)
                        mp_short = dual_mp if (is_bull and is_bear) else (3 if is_bear else 0)
                        
                        per_slot_long = BUYING_POWER / max(1, mp_long)
                        per_slot_short = BUYING_POWER / max(1, mp_short)
                        
                        syms_to_close_L = []; syms_to_close_S = []
                        
                        # --- MANAGE LONG ---
                        for sym, pos in positions_long.items():
                            if ts not in stock_ts_map.get(sym, {}): continue
                            idx = stock_ts_map[sym][ts]; df = stock_dfs[sym]
                            cc = df.iloc[idx]
                            cp, lp, op, hp = float(cc["close"]), float(cc["low"]), float(cc["open"]), float(cc["high"])
                            
                            # Partial Profit
                            if not pos.get("partial_done", False):
                                p_now = (cp - pos["ep"]) / pos["ep"]
                                if p_now >= PROFIT_TARGET:
                                    sqty = max(1, int(pos["qty"] * PARTIAL_FRAC))
                                    if sqty >= pos["qty"]: sqty = max(0, pos["qty"] - 1)
                                    if sqty > 0:
                                        trades.append({"pnl": (cp - pos["ep"]) * sqty, "qty": sqty, "ex": cp, "type": "LONG"})
                                        pos["qty"] -= sqty; pos["partial_done"] = True
                                        if pos["qty"] <= 0: syms_to_close_L.append(sym); continue
                            
                            if lp <= pos["sl"]:
                                trades.append({"pnl": (min(pos["sl"], op) - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": min(pos["sl"], op), "type": "LONG"})
                                syms_to_close_L.append(sym); continue
                            if ts.hour == 15 and ts.minute >= 15:
                                trades.append({"pnl": (cp - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": cp, "type": "LONG"})
                                syms_to_close_L.append(sym); continue
                                
                            # RSI 14 Lag0 >= 72 (Using Original RSI from the user's correct test)
                            rsi = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0
                            if rsi >= 72.0:
                                trades.append({"pnl": (cp - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": cp, "type": "LONG"})
                                syms_to_close_L.append(sym); continue
                                
                            if pre_lx[sym][idx] and idx+1 < len(df):
                                trades.append({"pnl": (float(df.iloc[idx+1]["open"]) - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": float(df.iloc[idx+1]["open"]), "type": "LONG"})
                                syms_to_close_L.append(sym); continue
                                
                        # --- MANAGE SHORT ---
                        for sym, pos in positions_short.items():
                            if ts not in stock_ts_map.get(sym, {}): continue
                            idx = stock_ts_map[sym][ts]; df = stock_dfs[sym]
                            cc = df.iloc[idx]
                            cp, lp, op, hp = float(cc["close"]), float(cc["low"]), float(cc["open"]), float(cc["high"])
                            
                            # DYN EXIT
                            if hp >= pos["dyn_sl"]:
                                ex_p = max(pos["dyn_sl"], op)
                                trades.append({"pnl": (pos["ep"] - ex_p) * pos["qty"], "qty": pos["qty"], "ex": ex_p, "type": "SHORT"})
                                syms_to_close_S.append(sym); continue
                            
                            pos["dyn_sl"] = min(pos["dyn_sl"], float(df.iloc[max(0, idx-1)]["high"]))
                            
                            if hp >= pos["sl"]:
                                trades.append({"pnl": (pos["ep"] - max(pos["sl"], op)) * pos["qty"], "qty": pos["qty"], "ex": max(pos["sl"], op), "type": "SHORT"})
                                syms_to_close_S.append(sym); continue
                            if ts.hour == 15 and ts.minute >= 15:
                                trades.append({"pnl": (pos["ep"] - cp) * pos["qty"], "qty": pos["qty"], "ex": cp, "type": "SHORT"})
                                syms_to_close_S.append(sym); continue
                                
                            # RSI 16 Lag 0 <= 28
                            rsi = df["rsi_16"].iloc[idx]
                            if rsi <= 28.0:
                                trades.append({"pnl": (pos["ep"] - cp) * pos["qty"], "qty": pos["qty"], "ex": cp, "type": "SHORT"})
                                syms_to_close_S.append(sym); continue
                                
                            if pre_sx[sym][idx] and idx+1 < len(df):
                                trades.append({"pnl": (pos["ep"] - float(df.iloc[idx+1]["open"])) * pos["qty"], "qty": pos["qty"], "ex": float(df.iloc[idx+1]["open"]), "type": "SHORT"})
                                syms_to_close_S.append(sym); continue
                                
                        for s in syms_to_close_L: del positions_long[s]
                        for s in syms_to_close_S: del positions_short[s]
                        
                        if ts.hour >= 15: continue
                        
                        # --- SCAN ENTRIES ---
                        for sym, df in stock_dfs.items():
                            if ts not in stock_ts_map[sym]: continue
                            idx = stock_ts_map[sym][ts]
                            gap_val = all_daily_gaps.get((current_date, sym), 0.0)
                            
                            # Long Entry
                            if is_bull and len(positions_long) < mp_long and sym not in positions_long:
                                allowed = True
                                if ifilter == "EXCLUDE_GAP_DOWN" and gap_val <= -0.008: allowed = False
                                if ifilter == "ONLY_GAP_UP" and gap_val < gap_th: allowed = False
                                
                                if allowed and pre_le[sym][idx] and not pre_lx[sym][idx] and idx+1 < len(df):
                                    ep = float(df.iloc[idx+1]["open"])
                                    qty = int(per_slot_long // ep)
                                    if qty > 0:
                                        positions_long[sym] = {"ep": ep, "qty": qty, "sl": ep * (1 - SL_PCT), "partial_done": False}
                                        
                            # Short Entry (Fixed Base Rules)
                            if is_bear and len(positions_short) < mp_short and sym not in positions_short:
                                if gap_val <= -0.008: # Short stock must be gap down
                                    if pre_se[sym][idx] and not pre_sx[sym][idx] and idx+1 < len(df):
                                        ep = float(df.iloc[idx+1]["open"])
                                        qty = int(per_slot_short // ep)
                                        if qty > 0:
                                            dyn_sl = float(df.iloc[idx]["high"])
                                            positions_short[sym] = {"ep": ep, "qty": qty, "sl": ep * (1 + SL_PCT), "dyn_sl": dyn_sl}

                    for sym, pos in positions_long.items():
                        trades.append({"pnl": (float(stock_dfs[sym]["close"].iloc[-1]) - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": float(stock_dfs[sym]["close"].iloc[-1]), "type": "LONG"})
                    for sym, pos in positions_short.items():
                        trades.append({"pnl": (pos["ep"] - float(stock_dfs[sym]["close"].iloc[-1])) * pos["qty"], "qty": pos["qty"], "ex": float(stock_dfs[sym]["close"].iloc[-1]), "type": "SHORT"})
                        
                    df_t = pd.DataFrame(trades)
                    if not df_t.empty:
                        df_t["stt"] = df_t["ex"] * df_t["qty"] * STT_PCT
                        net = df_t["pnl"].sum() - df_t["stt"].sum()
                        
                        df_l = df_t[df_t["type"] == "LONG"]
                        df_s = df_t[df_t["type"] == "SHORT"]
                        net_l = df_l["pnl"].sum() - df_l["stt"].sum() if not df_l.empty else 0
                        net_s = df_s["pnl"].sum() - df_s["stt"].sum() if not df_s.empty else 0
                        
                        results.append({
                            "Gap_Thresh": gap_th,
                            "Breadth_Thresh": b_th,
                            "Filter": ifilter,
                            "Dual_MP": dual_mp,
                            "Trades": len(df_t),
                            "WinRate": len(df_t[df_t["pnl"] > 0]) / len(df_t) * 100,
                            "Net": net / CAPITAL * 100,
                            "Net_Long": net_l / CAPITAL * 100,
                            "Net_Short": net_s / CAPITAL * 100
                        })
    
    print("\nSweep Complete!")
    res_df = pd.DataFrame(results)
    res_df.sort_values(by="Net", ascending=False, inplace=True)
    print("\nTOP 20 COMBINATIONS:")
    print(res_df.head(20).to_string(index=False))
    
    res_df.to_csv("research/mega_dual_sweep_results.csv", index=False)
    
if __name__ == "__main__":
    run_mega_sweep()
