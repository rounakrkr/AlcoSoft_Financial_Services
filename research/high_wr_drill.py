"""
HIGH WIN-RATE DRILL — Partial Exit ON focus
============================================
Goal: Push WR above 60% while keeping positive returns
Baseline: GAP70 + partial ON = 57.1% WR, +14.7%

Strategy: tighter regime filters with partial exit ON
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
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

nifty_daily = _fetch_yahoo_history("^NSEI", period="60d", interval="1d")
nifty_daily.columns = [c.lower() for c in nifty_daily.columns]
nifty_today_bull = {(ts.date() if hasattr(ts,'date') else ts)
                    for ts, row in nifty_daily.iterrows() if row["close"] > row["open"]}

print("Pre-computing gap stats...")
stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        day_open = float(grp["open"].iloc[0])
        gap_pct = ((day_open - prev_close) / prev_close * 100) if prev_close else 0.0
        by_day[d] = {"day_open": day_open, "prev_close": prev_close, "gap_pct": gap_pct}
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

# Per-day aggregates
all_dates = sorted(set(d for days in stock_day_data.values() for d in days.keys()))
day_stats = {}
for d in all_dates:
    gaps = [days[d]["gap_pct"] for sym, days in stock_day_data.items()
            if d in days and days[d]["prev_close"] is not None]
    if not gaps: continue
    g = np.array(gaps)
    day_stats[d] = {
        "pct_up": float(np.sum(g > 0) / len(g)),
        "avg_gap": float(np.mean(g)),
        "median_gap": float(np.median(g)),
        "pct_strong": float(np.sum(g > 0.5) / len(g)),  # gap > 0.5%
        "pct_vstrong": float(np.sum(g > 1.0) / len(g)), # gap > 1%
        "top10_avg": float(np.mean(sorted(g, reverse=True)[:10])),
        "min_gap": float(np.min(g)),      # weakest stock
        "pct_neg": float(np.sum(g < 0) / len(g)),  # % that gapped DOWN
    }

def days_where(metric, op, thr):
    result = set()
    for d, s in day_stats.items():
        v = s.get(metric, 0)
        if (op == ">=" and v >= thr) or (op == "<=" and v <= thr) or (op == "<" and v < thr):
            result.add(d)
    return result

# Timeline
all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

def portfolio_backtest(max_pos, allowed_dates=None, partial_exit=True, 
                       dynamic_breadth_thr=None, rsi_thr=72):
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
            # Partial exit
            if partial_exit and not pos["half"] and "rsi" in cc.index and pd.notna(cc["rsi"]):
                if float(cc["rsi"]) > rsi_thr and qty > 1:
                    h = qty // 2
                    trades.append({"pnl": (close - ep) * h, "win": (close > ep)})
                    pos["qty"] -= h; pos["half"] = True; qty = pos["qty"]
            ex = None
            if low <= pos["tsl"]: ex = min(pos["tsl"], float(cc["open"]))
            elif high >= pos["tgt"]: ex = max(pos["tgt"], float(cc["open"]))
            elif ts.hour == 15 and ts.minute >= 15: ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df): ex = float(df.iloc[idx+1]["open"])
            if ex:
                trades.append({"pnl": (ex - ep) * qty, "win": (ex > ep)}); closed.append(sym)
            else:
                sl_pct = abs(ep - pos["sl"]) / ep if ep > 0 else 0
                if high >= ep + ep * sl_pct * tsl_activation_ratio: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high * (1 - trailing_sl_percent))
                    if n > pos["tsl"]: pos["tsl"] = n
        for s in closed: del positions[s]
        if len(positions) >= max_pos: continue
        if ts.hour >= 15: continue
        if allowed_dates is not None and d not in allowed_dates: continue
        if dynamic_breadth_thr is not None:
            above = 0; total = 0
            for s2, days in stock_day_data.items():
                if d in days and ts in stock_ts_map.get(s2, {}):
                    df2 = stock_dfs[s2]; idx2 = stock_ts_map[s2][ts]; total += 1
                    if float(df2.iloc[idx2]["close"]) > days[d]["day_open"]: above += 1
            if total == 0 or above / total < dynamic_breadth_thr: continue
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
                                      "tsl": sl_p, "tsl_on": False, "half": False}
    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "win": (lc > pos["ep"])})
    return trades

results = []
def r(label, trades):
    if not trades: print(f"  {label:72s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]; w = sum(1 for p in pnls if p > 0)
    wr = w/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    flag = " ***" if wr >= 60 and ret > 0 else (" <<<" if wr >= 55 and ret > 5 else "")
    print(f"  {label:72s} | WR={wr:5.1f}% | Net={net:+10,.0f} | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")
    results.append({"label": label, "wr": wr, "net": net, "ret": ret, "t": len(pnls)})

MP = 5
print(f"\n{'='*120}")
print("HIGH WIN-RATE DRILL | Partial ON | max_pos=5")
print(f"{'='*120}")

# Baselines
print("\n--- BASELINES ---")
r("NO FILTER | pON",           portfolio_backtest(MP, partial_exit=True))
r("GAP70 | pON (prev best B)", portfolio_backtest(MP, allowed_dates=days_where("pct_up",">=",0.70), partial_exit=True))
r("STRONG40% | pOFF (new best)",portfolio_backtest(MP, allowed_dates=days_where("pct_strong",">=",0.40), partial_exit=False))
r("NIFTY_DAILY | pON (cheat)", portfolio_backtest(MP, allowed_dates=nifty_today_bull, partial_exit=True))

# A. All top configs WITH partial ON
print("\n--- ALL BEST GAP REGIMES + Partial ON ---")
regimes = [
    ("AVG>=0.5%",     days_where("avg_gap",">=",0.5)),
    ("STRONG>=40%",   days_where("pct_strong",">=",0.40)),
    ("STRONG>=50%",   days_where("pct_strong",">=",0.50)),
    ("VSTRONG>=20%",  days_where("pct_vstrong",">=",0.20)),
    ("VSTRONG>=30%",  days_where("pct_vstrong",">=",0.30)),
    ("VSTRONG>=40%",  days_where("pct_vstrong",">=",0.40)),
    ("TOP10>=1.5%",   days_where("top10_avg",">=",1.5)),
    ("TOP10>=2.0%",   days_where("top10_avg",">=",2.0)),
    ("MEDIAN>=0.5%",  days_where("median_gap",">=",0.5)),
    ("GAP70+AVG0.5",  days_where("pct_up",">=",0.70) & days_where("avg_gap",">=",0.5)),
    ("NO_NEG_GAPS",   days_where("pct_neg","<=",0.20)),  # less than 20% stocks gapped down
    ("NO_NEG+AVG0.3", days_where("pct_neg","<=",0.20) & days_where("avg_gap",">=",0.3)),
]
for label, ds in regimes:
    r(f"{label} ({len(ds)}d) | pON", portfolio_backtest(MP, allowed_dates=ds, partial_exit=True))

# B. RSI threshold for partial exit (instead of 72, try 65, 68, 75, 80)
print("\n--- RSI THRESHOLD for partial exit (GAP70 regime) ---")
gap70_days = days_where("pct_up",">=",0.70)
for rsi_t in [60, 65, 68, 70, 72, 75, 78, 80]:
    r(f"GAP70 | pON RSI>{rsi_t}",
      portfolio_backtest(MP, allowed_dates=gap70_days, partial_exit=True, rsi_thr=rsi_t))

# C. Partial ON + top regimes + RSI tuning
print("\n--- STRONG_GAP40 + RSI tuning ---")
sg40_days = days_where("pct_strong",">=",0.40)
for rsi_t in [60, 65, 68, 70, 72, 75, 78, 80]:
    r(f"STRONG40 | pON RSI>{rsi_t}",
      portfolio_backtest(MP, allowed_dates=sg40_days, partial_exit=True, rsi_thr=rsi_t))

# D. Dynamic breadth at signal time + Partial ON
print("\n--- GAP70 + DYNAMIC_BREADTH + Partial ON ---")
for db in [0.50, 0.55, 0.60, 0.65, 0.70]:
    r(f"GAP70 + DB>={int(db*100)}% | pON",
      portfolio_backtest(MP, allowed_dates=gap70_days, partial_exit=True, dynamic_breadth_thr=db))

# E. Higher max_pos with partial ON
print("\n--- GAP70 | Partial ON | different max_pos ---")
for mp in [3, 5, 7, 10, 15, 20]:
    r(f"GAP70 | pON | max_pos={mp}",
      portfolio_backtest(mp, allowed_dates=gap70_days, partial_exit=True))

# F. STRONG40 + different max_pos
print("\n--- STRONG40 | Partial ON | different max_pos ---")
for mp in [3, 5, 7, 10, 15, 20]:
    r(f"STRONG40 | pON | max_pos={mp}",
      portfolio_backtest(mp, allowed_dates=sg40_days, partial_exit=True))

# Top 15 by WR (since user wants high WR)
print(f"\n{'='*120}")
print("TOP 15 by WIN RATE (all partial ON configs)")
print(f"{'='*120}")
top_wr = sorted([x for x in results if x["ret"] > 0], key=lambda x: x["wr"], reverse=True)[:15]
for i, rx in enumerate(top_wr, 1):
    flag = " *** HIGHEST WR ***" if i == 1 else ""
    print(f"  #{i:2d} | {rx['label']:72s} | WR={rx['wr']:5.1f}% | Net={rx['net']:+10,.0f} | "
          f"Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{flag}")

print(f"\n{'='*120}")
print("TOP 15 by RETURN (all configs)")
print(f"{'='*120}")
top_ret = sorted(results, key=lambda x: x["ret"], reverse=True)[:15]
for i, rx in enumerate(top_ret, 1):
    flag = " *** BEST RETURN ***" if i == 1 else ""
    print(f"  #{i:2d} | {rx['label']:72s} | WR={rx['wr']:5.1f}% | Net={rx['net']:+10,.0f} | "
          f"Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{flag}")

print(f"\nTotal: {len(results)} configs | DONE.")
