import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from research.build_cache import load_cache
from core.strategy import (CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext)
from core.strategy_sets import load_strategy_sets
from core.order_executor import round_to_tick

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
MP = 3; SL_PCT = 0.010; PROFIT_TARGET = 0.005; PARTIAL_FRAC = 0.75; RSI_EXIT_THR = 72.0

stock_dfs = load_cache()
config = load_strategy_sets()
long_set_def = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
cover_set_def = next((s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)
evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

timeline_set = set()
for sym, df in stock_dfs.items(): timeline_set.update(df.index)
timeline = sorted(list(timeline_set))
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

mega_df = pd.concat([df.assign(symbol=sym, date=df.index.date) for sym, df in stock_dfs.items()])
first_candles = mega_df.groupby(["date", "symbol"]).first().reset_index()

all_gaps = {}
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

print("Precomputing all entry & exit signals...")
precomputed_entries = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}
precomputed_dyn_exits = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}

for sym, df in stock_dfs.items():
    for idx in range(1, len(df)):
        sliced = df.iloc[:idx+1]
        
        # ENTRY
        ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced, pattern_df=sliced, ws_count=len(sliced))
        cond = evaluator._evaluate_conditions(long_set_def, ctx)
        if cond and all(r.get("fired") for r in cond):
            precomputed_entries[sym][idx] = True
            
        # DYN EXIT
        ctx_exit = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
        cond_exit = evaluator._evaluate_conditions(cover_set_def, ctx_exit)
        if cond_exit and all(r.get("fired") for r in cond_exit):
            precomputed_dyn_exits[sym][idx] = True

def run_sim(valid_days, mode, ig):
    per_slot = BUYING_POWER / MP
    positions = {}; trades = []
    
    for ts in timeline:
        d = ts.date()
        closed = []
        
        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            cc = df.iloc[idx]
            pos = positions[sym]
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]
            if qty <= 0: closed.append(sym); continue
            
            rsi0 = float(cc["rsi"]) if pd.notna(cc["rsi"]) else 50.0
            
            if not pos.get("partial_done", False):
                if (close - ep) / ep >= PROFIT_TARGET:
                    cover_qty = max(1, int(qty * PARTIAL_FRAC))
                    if cover_qty >= qty: cover_qty = max(0, qty - 1)
                    if cover_qty > 0:
                        trades.append({"sym": sym, "d": d, "pnl": (close - ep) * cover_qty, "reason": "PARTIAL", "ex": close, "qty": cover_qty})
                        pos["qty"] -= cover_qty; qty = pos["qty"]
                        pos["partial_done"] = True
                        if qty <= 0: closed.append(sym); continue
            
            if rsi0 >= RSI_EXIT_THR:
                trades.append({"sym": sym, "d": d, "pnl": (close - ep) * qty, "reason": "RSI", "ex": close, "qty": qty})
                closed.append(sym); continue
                
            ex = None; reason = None
            if low <= pos["sl"]:
                ex = min(pos["sl"], float(cc["open"]))
                reason = "SL"
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close
                reason = "TIME"
            elif precomputed_dyn_exits[sym][idx]:
                if idx+1 < len(df): ex = float(df.iloc[idx+1]["open"]); reason = "DYN"
                
            if ex:
                trades.append({"sym": sym, "d": d, "pnl": (ex - ep) * qty, "reason": reason, "ex": ex, "qty": qty})
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
            if mode == "EXCLUDE_GAP_DOWN" and gap_pct <= -0.008: continue
            if mode == "ONLY_GAP_UP" and gap_pct < ig: continue
            
            if ts not in stock_ts_map[sym]: continue
            idx = stock_ts_map[sym][ts]
            
            if precomputed_entries[sym][idx]:
                # Prevent entry if dyn exit also fires immediately
                if precomputed_dyn_exits[sym][idx]: continue
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"])
                    qty = int(per_slot // ep)
                    if qty > 0:
                        positions[sym] = {"ep": ep, "qty": qty, "sl": round_to_tick(ep * (1 - SL_PCT)), "partial_done": False}

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        # Find last date for this position (approx logic using max date from df, but d is fine for tracking)
        trades.append({"sym": sym, "d": dates[-1], "pnl": (lc - pos["ep"]) * pos["qty"], "reason": "EOD", "ex": lc, "qty": pos["qty"]})
    return trades

IND_GAPS = [0.004, 0.006, 0.008, 0.010]
MKT_BREADTHS = [0.30, 0.40, 0.50, 0.60]
MODES = ["ALL_STOCKS", "EXCLUDE_GAP_DOWN", "ONLY_GAP_UP"]

results = []
diagnostics = {} # Keep detailed trades for deep dive

start_t = time.time()
counter = 0

print(f"Starting universe sweeps. Total loops = {len(IND_GAPS)*len(MKT_BREADTHS)*len(MODES)}")

for ig in IND_GAPS:
    for mg in MKT_BREADTHS:
        valid_days = set()
        for i in range(1, len(dates)):
            curr_d = dates[i]
            daily_gaps_vals = [g for (d, s), g in all_gaps.items() if d == curr_d]
            if not daily_gaps_vals: continue
            strong = sum(1 for g in daily_gaps_vals if g >= ig)
            if (strong / len(daily_gaps_vals)) >= mg:
                valid_days.add(curr_d)
                
        if not valid_days: continue
        
        for mode in MODES:
            trades = run_sim(valid_days, mode, ig)
            counter += 1
            if counter % 5 == 0: print(f"Completed {counter} sweeps in {time.time()-start_t:.1f}s")
            
            df_t = pd.DataFrame(trades)
            if df_t.empty: continue
            
            df_t["gap_pct"] = df_t.apply(lambda row: all_gaps.get((row["d"], row["sym"]), 0.0), axis=1)
            
            df_t["stt"] = df_t["ex"] * df_t["qty"] * 0.00035
            gross = df_t["pnl"].sum()
            net = gross - df_t["stt"].sum()
            wr = len(df_t[df_t["pnl"]>0]) / len(df_t) * 100
            
            key = f"IG>={ig*100:.1f}%|MB>={mg*100:.0f}%"
            results.append({
                "Config": key, "Mode": mode,
                "Trades": len(df_t), "WinRate": wr,
                "Gross%": gross/CAPITAL*100, "Net%": net/CAPITAL*100
            })
            if key not in diagnostics: diagnostics[key] = {}
            diagnostics[key][mode] = df_t

res_df = pd.DataFrame(results)

# DEEP DIVE REPORT GENERATOR
print("\nGenerating Deep Dive Report...\n")
with open("research/long_universe_deep_dive.md", "w", encoding="utf-8") as f:
    f.write("# 🕵️ Long Universe Sweep Deep Dive Report\n\n")
    
    # 1. Top Combinations
    f.write("## 🏆 Top 5 Combinations (Ranked by Net Return)\n")
    top5 = res_df.sort_values("Net%", ascending=False).head(5)
    f.write(top5.to_markdown(index=False))
    f.write("\n\n")
    
    # 2. Comparative Analysis per Configuration
    f.write("## 🔍 Detailed Postmortem (Mode Comparison)\n")
    for key in res_df["Config"].unique():
        f.write(f"### {key}\n")
        sub_df = res_df[res_df["Config"] == key]
        f.write(sub_df[["Mode", "Trades", "WinRate", "Gross%", "Net%"]].to_markdown(index=False))
        f.write("\n\n")
        
        # Analyze why ALL_STOCKS vs EXCLUDE_GAP_DOWN
        if "ALL_STOCKS" in diagnostics[key] and "EXCLUDE_GAP_DOWN" in diagnostics[key]:
            all_df = diagnostics[key]["ALL_STOCKS"]
            exc_df = diagnostics[key]["EXCLUDE_GAP_DOWN"]
            
            all_net = all_df["pnl"].sum() - all_df["stt"].sum()
            exc_net = exc_df["pnl"].sum() - exc_df["stt"].sum()
            
            diff_net = all_net - exc_net
            diff_net_pct = diff_net / CAPITAL * 100
            
            # Find the trades in ALL_STOCKS that were on <= -0.8% gap down stocks
            gd_trades = all_df[all_df["gap_pct"] <= -0.008]
            gd_gross = gd_trades["pnl"].sum() / CAPITAL * 100
            
            f.write(f"**Insight for {key}:**\n")
            if all_net > exc_net:
                f.write(f"👉 `ALL_STOCKS` made **{diff_net_pct:.2f}% MORE** net profit than excluding -0.8% gap down stocks.\n")
                f.write(f"👉 The -0.8% gap down stocks themselves contributed **{gd_gross:.2f}% Gross Profit** to the `ALL_STOCKS` mode. These are stocks that opened weakly but then staged a massive 'V-Shape' recovery and broke out to the upside!\n")
            else:
                f.write(f"👉 `EXCLUDE_GAP_DOWN` protected the system, making **{-diff_net_pct:.2f}% MORE** net profit.\n")
                f.write(f"👉 The -0.8% gap down stocks dragged down the `ALL_STOCKS` mode by losing **{gd_gross:.2f}% Gross Profit**. They were fake breakouts that failed.\n")
            
        f.write("---\n\n")
print("Report generated at research/long_universe_deep_dive.md")
