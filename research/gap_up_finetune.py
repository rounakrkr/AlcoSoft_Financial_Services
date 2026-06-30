"""
GAP_UP 70% Fine-tuning — the winner from mega sweep
Test: partial ON/OFF, max_pos 5/7/10, GAP+DB high combos
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from datetime import time as dtime
from collections import defaultdict
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from screener.morning_screener import NIFTY_50, _fetch_yahoo_history
from core.strategy import (CONDITION_REGISTRY, StrategySetEvaluator,
                            StrategyEvaluationContext, _build_indicators)
from core.strategy_sets import load_strategy_sets
from core.order_executor import calculate_stop_loss, calculate_target, round_to_tick
from core.trading_settings import get as cfg

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
config       = load_strategy_sets()
buy_set_def  = next(s for s in config.buy_sets  if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
sell_set_def = next(s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS")
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)
tsl_activation_ratio = float(cfg("risk", "tsl_activation_ratio", 1.4))
trailing_sl_percent  = float(cfg("risk", "trailing_sl_percent",  0.008))
position_size_margin = float(cfg("risk", "position_size_margin", 1.0))

def fetch(symbol, period="60d", interval="5m"):
    try:
        df = _fetch_yahoo_history(symbol, period=period, interval=interval)
        if df is None or df.empty: return pd.DataFrame()
        df.columns = [c.lower() for c in df.columns]
        df.dropna(subset=["close"], inplace=True)
        df["bucket"] = df.index
        df = _build_indicators(df)
        df.dropna(subset=["ema21","vwap"], inplace=True)
        return df if len(df) >= 20 else pd.DataFrame()
    except Exception:
        return pd.DataFrame()

print("Loading stocks...")
stock_dfs = {}
for i, sym in enumerate(NIFTY_50):
    print(f"  {i+1}/{len(NIFTY_50)}: {sym}    ", end="\r")
    df = fetch(sym)
    if not df.empty:
        stock_dfs[sym] = df
print(f"\n{len(stock_dfs)} stocks loaded.")

# Pre-compute
stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        by_day[d] = {"day_open": float(grp["open"].iloc[0]), "prev_close": prev_close}
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

def compute_gap_up_days(threshold):
    dg = defaultdict(int); dt = defaultdict(int)
    for sym, days in stock_day_data.items():
        for d, info in days.items():
            if info["prev_close"] is not None:
                dt[d] += 1
                if info["day_open"] > info["prev_close"]: dg[d] += 1
    return {d for d in dt if dt[d] > 0 and dg[d]/dt[d] >= threshold}

gap65 = compute_gap_up_days(0.65)
gap70 = compute_gap_up_days(0.70)
gap75 = compute_gap_up_days(0.75)
print(f"  GAP65: {len(gap65)}d | GAP70: {len(gap70)}d | GAP75: {len(gap75)}d")

# Nifty daily ref
nifty_daily = _fetch_yahoo_history("^NSEI", period="60d", interval="1d")
nifty_daily.columns = [c.lower() for c in nifty_daily.columns]
nifty_today_bull = {(ts.date() if hasattr(ts,'date') else ts)
                    for ts, row in nifty_daily.iterrows() if row["close"] > row["open"]}

# Timeline
all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

def portfolio_backtest(max_pos, allowed_dates=None, partial_exit=False, dynamic_breadth_thr=None):
    per_slot = BUYING_POWER * position_size_margin / max_pos
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
            close=float(cc["close"]); high=float(cc["high"]); low=float(cc["low"])
            ep=pos["ep"]; qty=pos["qty"]
            if partial_exit and not pos["half"] and "rsi" in cc.index and pd.notna(cc["rsi"]):
                if float(cc["rsi"])>72 and qty>1:
                    h=qty//2; trades.append({"pnl":(close-ep)*h}); pos["qty"]-=h; pos["half"]=True; qty=pos["qty"]
            ex=None
            if low<=pos["tsl"]: ex=min(pos["tsl"],float(cc["open"]))
            elif high>=pos["tgt"]: ex=max(pos["tgt"],float(cc["open"]))
            elif ts.hour==15 and ts.minute>=15: ex=close
            else:
                ctx=StrategyEvaluationContext(side="sell",indicator_df=sliced,pattern_df=sliced,ws_count=0)
                cond=evaluator._evaluate_conditions(sell_set_def,ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1<len(df): ex=float(df.iloc[idx+1]["open"])
            if ex:
                trades.append({"pnl":(ex-ep)*qty}); closed.append(sym)
            else:
                sl_pct=abs(ep-pos["sl"])/ep if ep>0 else 0
                if high>=ep+ep*sl_pct*tsl_activation_ratio: pos["tsl_on"]=True
                if pos["tsl_on"]:
                    n=round_to_tick(high*(1-trailing_sl_percent))
                    if n>pos["tsl"]: pos["tsl"]=n
        for s in closed: del positions[s]
        if len(positions)>=max_pos: continue
        if ts.hour>=15: continue
        if allowed_dates is not None and d not in allowed_dates: continue
        if dynamic_breadth_thr is not None:
            above=0; total=0
            for s2, days in stock_day_data.items():
                if d in days and ts in stock_ts_map.get(s2,{}):
                    df2=stock_dfs[s2]; idx2=stock_ts_map[s2][ts]; total+=1
                    if float(df2.iloc[idx2]["close"])>days[d]["day_open"]: above+=1
            if total==0 or above/total<dynamic_breadth_thr: continue
        for sym in stock_dfs:
            if len(positions)>=max_pos: break
            if sym in positions: continue
            if ts not in stock_ts_map.get(sym,{}): continue
            df=stock_dfs[sym]; idx=stock_ts_map[sym][ts]
            if idx<10: continue
            sliced=df.iloc[:idx+1]
            ctx=StrategyEvaluationContext(side="buy",indicator_df=sliced,pattern_df=sliced,ws_count=0)
            cond=evaluator._evaluate_conditions(buy_set_def,ctx)
            if cond and all(r.get("fired") for r in cond):
                if idx+1<len(df):
                    ep=float(df.iloc[idx+1]["open"]); qty=int(per_slot//ep)
                    if qty<1: continue
                    sl_p=calculate_stop_loss(ep,"BUY")
                    positions[sym]={"ep":ep,"qty":qty,"sl":sl_p,"tgt":calculate_target(ep,sl_p),
                                    "tsl":sl_p,"tsl_on":False,"half":False}
    for sym,pos in positions.items():
        trades.append({"pnl":(float(stock_dfs[sym]["close"].iloc[-1])-pos["ep"])*pos["qty"]})
    return trades

def r(label, trades):
    if not trades: print(f"  {label:65s} | NO TRADES"); return
    pnls=[t["pnl"] for t in trades]; w=sum(1 for p in pnls if p>0)
    wr=w/len(pnls)*100; net=sum(pnls); ret=net/CAPITAL*100
    flag=" ***" if ret>=15 else (" <<<" if ret>5 else "")
    print(f"  {label:65s} | WR={wr:5.1f}% | Net={net:+10,.0f} | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")

print(f"\n{'='*110}")
print("GAP_UP WINNER FINE-TUNING")
print(f"{'='*110}")

# A. Gap thresholds + partial + max_pos
for mp in [5, 7, 10]:
    print(f"\n--- max_pos={mp} ---")
    for gap_label, gap_set in [("GAP65",gap65),("GAP70",gap70),("GAP75",gap75)]:
        for pe_label, pe in [("pOFF",False),("pON",True)]:
            r(f"{gap_label} | {pe_label} | pos={mp}",
              portfolio_backtest(mp, allowed_dates=gap_set, partial_exit=pe))

# B. GAP + DB high combos
print(f"\n--- GAP + DYNAMIC_BREADTH high combos (max_pos=5) ---")
for gt in [("GAP65",gap65),("GAP70",gap70)]:
    for db in [0.60, 0.65, 0.70]:
        for pe in [False, True]:
            r(f"{gt[0]} + DB>={int(db*100)}% | {'pON' if pe else 'pOFF'}",
              portfolio_backtest(5, allowed_dates=gt[1], dynamic_breadth_thr=db, partial_exit=pe))

# C. Reference
print(f"\n--- REFERENCE ---")
r("NIFTY_DAILY (CHEAT) | pOFF",
  portfolio_backtest(5, allowed_dates=nifty_today_bull, partial_exit=False))
r("NO FILTER | pOFF",
  portfolio_backtest(5, partial_exit=False))

print("\nDONE.")
