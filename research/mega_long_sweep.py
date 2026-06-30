import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from research.build_cache import load_cache
from core.strategy import CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext
from core.strategy_sets import load_strategy_sets
import ta

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
SL_PCT = 0.010
PROFIT_TARGET = 0.005
PARTIAL_FRAC = 0.75
STT_PCT = 0.00035
MP = 3

config = load_strategy_sets()
buy_set_long = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
sell_set_long = next((s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)

evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

stock_dfs = load_cache()

mega_df = pd.concat([df.assign(symbol=sym) for sym, df in stock_dfs.items()])
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

pre_le = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
pre_lx = {sym: [False]*len(df) for sym, df in stock_dfs.items()}

print("Precomputing Long signals...")
for sym, df in stock_dfs.items():
    for i in range(10, len(df)):
        sliced = df.iloc[:i+1]
        c_le = evaluator._evaluate_conditions(buy_set_long, StrategyEvaluationContext("buy", sliced, sliced, i+1))
        if c_le and all(r.get("fired") for r in c_le): pre_le[sym][i] = True
        c_lx = evaluator._evaluate_conditions(sell_set_long, StrategyEvaluationContext("sell", sliced, sliced, i+1))
        if c_lx and all(r.get("fired") for r in c_lx): pre_lx[sym][i] = True

def run_mega_long_sweep():
    gap_thresholds = [0.010, 0.005]
    breadth_thresholds = [0.40]
    indiv_filters = ["ALL", "EXCLUDE_GAP_DOWN"]
    
    results = []
    total_runs = len(gap_thresholds) * len(breadth_thresholds) * len(indiv_filters)
    print(f"Total Sweep Combinations: {total_runs}")
    
    run_idx = 0
    for gap_th in gap_thresholds:
        for b_th in breadth_thresholds:
            
            bull_days = set()
            for i in range(1, len(dates)):
                curr_d = dates[i]
                gaps = [g for (d, s), g in all_daily_gaps.items() if d == curr_d]
                if not gaps: continue
                if sum(1 for g in gaps if g >= gap_th) / len(gaps) >= b_th:
                    bull_days.add(curr_d)
                    
            for ifilter in indiv_filters:
                run_idx += 1
                if run_idx % 5 == 0: print(f"  Progress: {run_idx}/{total_runs}", end="\r")
                
                trades = []
                positions = {}
                per_slot = BUYING_POWER / MP
                
                for ts in timeline:
                    current_date = ts.date()
                    is_bull = current_date in bull_days
                    
                    syms_to_close = []
                    
                    for sym, pos in positions.items():
                        if ts not in stock_ts_map.get(sym, {}): continue
                        idx = stock_ts_map[sym][ts]; df = stock_dfs[sym]
                        cc = df.iloc[idx]
                        cp, lp, op, hp = float(cc["close"]), float(cc["low"]), float(cc["open"]), float(cc["high"])
                        
                        if not pos.get("partial_done", False):
                            p_now = (cp - pos["ep"]) / pos["ep"]
                            if p_now >= PROFIT_TARGET:
                                sqty = max(1, int(pos["qty"] * PARTIAL_FRAC))
                                if sqty >= pos["qty"]: sqty = max(0, pos["qty"] - 1)
                                if sqty > 0:
                                    trades.append({"sym": sym, "pnl": (cp - pos["ep"]) * sqty, "qty": sqty, "ex": cp, "reason": "PARTIAL"})
                                    pos["qty"] -= sqty; pos["partial_done"] = True
                                    if pos["qty"] <= 0: syms_to_close.append(sym); continue
                        
                        if lp <= pos["sl"]:
                            trades.append({"sym": sym, "pnl": (min(pos["sl"], op) - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": min(pos["sl"], op), "reason": "SL"})
                            syms_to_close.append(sym); continue
                        if ts.hour == 15 and ts.minute >= 15:
                            trades.append({"sym": sym, "pnl": (cp - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": cp, "reason": "EOD_SQ"})
                            syms_to_close.append(sym); continue
                            
                        # Use Original RSI exactly like the 465 trades screenshot
                        rsi = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0
                        if rsi >= 72.0:
                            trades.append({"sym": sym, "pnl": (cp - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": cp, "reason": "RSI_EXIT"})
                            syms_to_close.append(sym); continue
                            
                        if pre_lx[sym][idx] and idx+1 < len(df):
                            ex_p = float(df.iloc[idx+1]["open"])
                            trades.append({"sym": sym, "pnl": (ex_p - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": ex_p, "reason": "STRAT_EXIT"})
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
                        
                        allowed = True
                        if ifilter == "EXCLUDE_GAP_DOWN" and gap_val <= -0.008: allowed = False
                        if ifilter == "ONLY_GAP_UP" and gap_val < gap_th: allowed = False
                        
                        if allowed and pre_le[sym][idx] and not pre_lx[sym][idx] and idx+1 < len(df):
                            ep = float(df.iloc[idx+1]["open"])
                            qty = int(per_slot // ep)
                            if qty > 0:
                                positions[sym] = {"ep": ep, "qty": qty, "sl": ep * (1 - SL_PCT), "partial_done": False}

                for sym, pos in positions.items():
                    trades.append({"sym": sym, "pnl": (float(stock_dfs[sym]["close"].iloc[-1]) - pos["ep"]) * pos["qty"], "qty": pos["qty"], "ex": float(stock_dfs[sym]["close"].iloc[-1]), "reason": "EOD"})
                    
                df_t = pd.DataFrame(trades)
                if not df_t.empty:
                    df_t["stt"] = df_t["ex"] * df_t["qty"] * STT_PCT
                    net = df_t["pnl"].sum() - df_t["stt"].sum()
                    win_rate = len(df_t[df_t["pnl"] > 0]) / len(df_t) * 100
                    gross_return = df_t["pnl"].sum() / CAPITAL * 100
                    
                    results.append({
                        "Gap_Thresh": f"{gap_th*100:.1f}%",
                        "Breadth_Thresh": f"{b_th*100:.0f}%",
                        "Filter": ifilter,
                        "Trades": len(df_t),
                        "WinRate": f"{win_rate:.1f}%",
                        "Gross_Ret": f"{gross_return:.1f}%",
                        "Net_Ret": f"{net/CAPITAL*100:.2f}%"
                    })
    
    print("\nSweep Complete!")
    res_df = pd.DataFrame(results)
    
    import re
    def extract_num(val):
        try:
            return float(re.sub(r'[^\d\.-]', '', str(val)))
        except:
            return 0.0
            
    res_df["Sort_Net"] = res_df["Net_Ret"].apply(extract_num)
    res_df.sort_values(by="Sort_Net", ascending=False, inplace=True)
    res_df.drop(columns=["Sort_Net"], inplace=True)
    
    print("\n" + "="*80)
    print("FOUR TESTS AS REQUESTED BY USER:")
    print("="*80)
    print(res_df.to_string(index=False))
    
if __name__ == "__main__":
    run_mega_long_sweep()
