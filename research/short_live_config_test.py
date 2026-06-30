import sys, os
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
MP = 3; SL_PCT = 0.010
PROFIT_TARGET = 0.005
PARTIAL_FRAC = 0.75 # Buy back 75% at 0.50% profit
RSI_EXIT_THR = 28.0

def condition_close_1_above_ema9(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 3 or "close" not in df.columns or "ema9" not in df.columns or pd.isna(df["ema9"].iloc[-1]):
        return {"fired": False}
    close_1     = df["close"].iloc[-2]
    close_2     = df["close"].iloc[-3]
    ema9_now    = df["ema9"].iloc[-1]
    crossed_up  = (close_2 <= ema9_now) and (close_1 > ema9_now)
    return {"fired": bool(crossed_up), "msg": f"C1={close_1:.2f}, C2={close_2:.2f}, EMA9={ema9_now:.2f}"}

CONDITION_REGISTRY["close_1_above_ema9"] = condition_close_1_above_ema9
config = load_strategy_sets()
short_set_def = next((s for s in config.buy_sets if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN"), None)
cover_set_def = next((s for s in config.sell_sets if s.name == "SHORT_STREAK_MOMENTUM_RECOVERY"), None)

evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

print("Loading cache...")
stock_dfs = load_cache()
print("Cache loaded.")

# Build timeline and gap_down logic
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
    strong = (gaps <= -0.005).sum() # GAP DOWN 0.5%
    total = len(gaps)
    if total > 0 and (strong / total) >= 0.40:
        daily_gaps.append(curr_d)

strong40_down_days = set(daily_gaps)

def run_short_live_simulation():
    per_slot = BUYING_POWER / MP
    positions = {}; trades = []
    
    for ts in timeline:
        d = ts.date()
        closed = []
        
        # 1. Manage open positions (Short Cover)
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
            
            # Profit target check (Partial Book)
            if not pos.get("partial_done", False):
                profit_now = (ep - close) / ep # profit when price drops
                if profit_now >= PROFIT_TARGET:
                    cover_qty = max(1, int(qty * PARTIAL_FRAC))
                    if cover_qty >= qty: cover_qty = max(0, qty - 1)
                    if cover_qty > 0:
                        trades.append({"pnl": (ep - close) * cover_qty, "reason": "PARTIAL_PROFIT"})
                        pos["qty"] -= cover_qty
                        qty = pos["qty"]
                        pos["partial_done"] = True
                        if qty <= 0: closed.append(sym); continue
            
            # RSI exit check
            if rsi0 <= RSI_EXIT_THR:
                trades.append({"pnl": (ep - close) * qty, "reason": "RSI_EXIT"})
                closed.append(sym); continue
                
            # Normal exits (Stop Loss, Square off, Cover Strategy)
            ex = None
            reason = None
            if high >= pos["sl"]: 
                ex = max(pos["sl"], float(cc["open"]))
                reason = "SL_EXIT"
            elif ts.hour == 15 and ts.minute >= 15: 
                ex = close
                reason = "TIME_EXIT"
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                cond = [condition_close_1_above_ema9(ctx)]
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df): 
                        ex = float(df.iloc[idx+1]["open"])
                        reason = "DYN_EXIT"
            if ex:
                trades.append({"pnl": (ep - ex) * qty, "reason": reason})
                closed.append(sym)
                
        for s in closed:
            if s in positions: del positions[s]
            
        # 2. Look for new entries (Shorts)
        if len(positions) >= MP: continue
        if ts.hour >= 15: continue
        if d not in strong40_down_days: continue
        
        for sym, df in stock_dfs.items():
            if len(positions) >= MP: break
            if sym in positions: continue # Rule 1 disabled conceptually: multiple trades allowed but not overlapping
            if ts not in stock_ts_map[sym]: continue
            idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced, pattern_df=sliced, ws_count=len(sliced))
            cond = evaluator._evaluate_conditions(short_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                # Apply Rule 2 (Prevent Entry if Cover is firing)
                cover_ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                cover_cond = [condition_close_1_above_ema9(cover_ctx)]
                if cover_cond and all(r.get("fired") for r in cover_cond):
                    continue # Blocked by Rule 2
                
                if idx+1 < len(df):
                    nxt = df.iloc[idx+1]
                    ep = float(nxt["open"])
                    qty = int(per_slot // ep)
                    if qty > 0:
                        sl_p = round_to_tick(ep * (1 + SL_PCT)) # SL is ABOVE entry for shorts
                        positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p, "partial_done": False}

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (pos["ep"] - lc) * pos["qty"], "reason": "EOD"})
    
    return trades

print("Running Short Live Configuration Simulation...")
print(f"Config: MP={MP}, GapDown40=True, Partial={PARTIAL_FRAC*100}% @ {PROFIT_TARGET*100}%, RSI_Exit={RSI_EXIT_THR}, Rule1=Disabled")

trades = run_short_live_simulation()
df_t = pd.DataFrame(trades)
if not df_t.empty:
    win = len(df_t[df_t["pnl"] > 0])
    tot = len(df_t)
    wr = win / tot * 100
    gross = df_t["pnl"].sum()
    stt = tot * 30
    net = gross - stt
    print(f"\nRESULTS:")
    print(f"Total Trades: {tot}")
    print(f"Win Rate:     {wr:.1f}%")
    print(f"Gross Return: {gross/CAPITAL*100:.1f}%")
    print(f"Est. STT:     {stt/CAPITAL*100:.1f}%")
    print(f"NET RETURN:   {net/CAPITAL*100:.1f}%")
    
    print("\n================ EXIT REASON BREAKDOWN ================")
    summary = df_t.groupby("reason").agg(
        Count=("pnl", "count"),
        Gross_Return_Pct=("pnl", lambda x: x.sum() / CAPITAL * 100),
        Avg_Trade_Pct=("pnl", lambda x: (x.mean() / (BUYING_POWER / MP)) * 100) # Average percentage return per trade
    ).reset_index()
    print(summary.to_string(index=False))
    print("=======================================================")
else:
    print("No trades executed.")
