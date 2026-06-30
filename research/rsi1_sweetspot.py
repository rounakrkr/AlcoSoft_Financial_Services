"""
RSI(1) HIGH EXIT SWEET SPOT — WR + Return focused
==================================================
User wants: High WR + Good Return
Test RSI(1) high exit thresholds 68-76 (standalone, no panic)
Compare directly with current RSI(0)>=72 baseline
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
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

def fetch(symbol):
    try:
        df = _fetch_yahoo_history(symbol, period="60d", interval="5m")
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
    if not df.empty: stock_dfs[sym] = df
print(f"\n{len(stock_dfs)} stocks loaded.")

stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        by_day[d] = {"day_open": float(grp["open"].iloc[0]), "prev_close": prev_close}
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

all_dates_set = set(d for days in stock_day_data.values() for d in days.keys())
strong40_days = set()
for d in all_dates_set:
    strong = sum(1 for sym, days in stock_day_data.items()
                 if d in days and days[d]["prev_close"]
                 and (days[d]["day_open"] - days[d]["prev_close"]) / days[d]["prev_close"] >= 0.005)
    total  = sum(1 for sym, days in stock_day_data.items()
                 if d in days and days[d]["prev_close"])
    if total > 0 and strong / total >= 0.40:
        strong40_days.add(d)
print(f"STRONG_GAP_40 days: {len(strong40_days)}")

all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

def get_rsi(sliced, lb=0):
    idx = -(1 + lb)
    if len(sliced) < abs(idx): return None
    v = sliced["rsi"].iloc[idx]
    return float(v) if pd.notna(v) else None

def backtest(max_pos, allowed_dates, hi_thr, hi_lb, lo_thr=None, lo_lb=0):
    per_slot = BUYING_POWER / max_pos
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
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]

            rsi_hi = get_rsi(sliced, hi_lb)
            rsi_lo = get_rsi(sliced, lo_lb) if lo_thr else None

            if rsi_hi is not None and rsi_hi >= hi_thr:
                trades.append({"pnl": (close - ep) * qty, "win": close > ep})
                closed.append(sym); continue

            if lo_thr and rsi_lo is not None and pos["held"] >= 2 and rsi_lo < lo_thr:
                trades.append({"pnl": (close - ep) * qty, "win": close > ep})
                closed.append(sym); continue

            ex = None
            if low <= pos["tsl"]:    ex = min(pos["tsl"], float(cc["open"]))
            elif high >= pos["tgt"]: ex = max(pos["tgt"], float(cc["open"]))
            elif ts.hour == 15 and ts.minute >= 15: ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df): ex = float(df.iloc[idx+1]["open"])
            if ex:
                trades.append({"pnl": (ex - ep) * qty, "win": ex > ep})
                closed.append(sym)
            else:
                sl_pct = abs(ep - pos["sl"]) / ep if ep > 0 else 0
                if high >= ep + ep * sl_pct * tsl_activation_ratio: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high * (1 - trailing_sl_percent))
                    if n > pos["tsl"]: pos["tsl"] = n
                pos["held"] += 1
        for s in closed: del positions[s]

        if len(positions) >= max_pos: continue
        if ts.hour >= 15: continue
        if d not in allowed_dates: continue
        for sym in stock_dfs:
            if len(positions) >= max_pos: break
            if sym in positions: continue
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced,
                                            pattern_df=sliced, ws_count=0)
            cond = evaluator._evaluate_conditions(buy_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"]); qty = int(per_slot // ep)
                    if qty < 1: continue
                    sl_p = calculate_stop_loss(ep, "BUY")
                    positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p,
                                      "tgt": calculate_target(ep, sl_p),
                                      "tsl": sl_p, "tsl_on": False, "held": 0}
    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "win": lc > pos["ep"]})
    return trades

results = []
def r(label, trades):
    if not trades: print(f"  {label:70s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    # Flag: best is HIGH WR (>57%) + HIGH return (>25%)
    if wr >= 57 and ret >= 25:   flag = " *** SWEET SPOT ***"
    elif wr >= 57:               flag = " (high WR)"
    elif ret >= 30:              flag = " (high return)"
    else:                        flag = ""
    print(f"  {label:70s} | WR={wr:5.1f}% | Net={net:+10,.0f} | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")
    results.append({"label": label, "wr": wr, "net": net, "ret": ret, "t": len(pnls)})

MP = 3
print(f"\n{'='*115}")
print("RSI(1) HIGH EXIT SWEET SPOT — WR + Return balance")
print(f"{'='*115}")

print("\n--- BASELINE (current production) ---")
r("RSI(0)>=72 ONLY [production]",
  backtest(MP, strong40_days, hi_thr=72, hi_lb=0))

print("\n--- RSI(1) HIGH EXIT ONLY (no panic) — 68 to 76 ---")
for thr in range(68, 77):
    r(f"RSI(1)>={thr} only",
      backtest(MP, strong40_days, hi_thr=thr, hi_lb=1))

print("\n--- RSI(1) HIGH EXIT + RSI(1) PANIC<54 (prev best combo) ---")
for thr in range(68, 77):
    r(f"RSI(1)>={thr} HIGH + RSI(1)<54 PANIC",
      backtest(MP, strong40_days, hi_thr=thr, hi_lb=1, lo_thr=54, lo_lb=1))

print("\n--- RSI(1) HIGH EXIT + RSI(1) PANIC<52 ---")
for thr in range(68, 77):
    r(f"RSI(1)>={thr} HIGH + RSI(1)<52 PANIC",
      backtest(MP, strong40_days, hi_thr=thr, hi_lb=1, lo_thr=52, lo_lb=1))

print("\n--- RSI(1) HIGH EXIT + RSI(1) PANIC<55 ---")
for thr in range(68, 77):
    r(f"RSI(1)>={thr} HIGH + RSI(1)<55 PANIC",
      backtest(MP, strong40_days, hi_thr=thr, hi_lb=1, lo_thr=55, lo_lb=1))

# Final sorted
print(f"\n{'='*115}")
print("RANKED: Best balance of WR + Return (sorted by WR first, then return)")
print(f"{'='*115}")
ranked = sorted([x for x in results if x["ret"] > 0],
                key=lambda x: (round(x["wr"]), x["ret"]), reverse=True)
for i, rx in enumerate(ranked[:15], 1):
    print(f"  #{i:2d} | {rx['label']:70s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}%")

print(f"\n{'='*115}")
print("RANKED: Pure return (top 10)")
print(f"{'='*115}")
top_ret = sorted(results, key=lambda x: x["ret"], reverse=True)[:10]
for i, rx in enumerate(top_ret, 1):
    print(f"  #{i:2d} | {rx['label']:70s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}%")

print(f"\nDONE.")
