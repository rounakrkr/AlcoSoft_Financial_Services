"""
GAP MAGNITUDE + BREADTH Deep Sweep
===================================
Not just "did stocks gap up?" but "by HOW MUCH?"

Tests:
  1. AVG_GAP: Average gap % across all 50 stocks > threshold
  2. MEDIAN_GAP: Median gap % > threshold (less affected by outliers)
  3. GAP_COUNT + MAGNITUDE combo: 70%+ gapped up AND avg gap > X%
  4. WEIGHTED_GAP: Weight by market cap proxy (stock price × volume)
  5. STRONG_GAP: % of stocks with gap > 0.5% (strong gappers only)
  6. TOP10_GAP: Average gap of top 10 gappers > threshold
  7. BREADTH_CHANGE: Today's opening breadth vs yesterday's close breadth
  8. Best combos with dynamic breadth
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

# Nifty daily for reference
nifty_daily = _fetch_yahoo_history("^NSEI", period="60d", interval="1d")
nifty_daily.columns = [c.lower() for c in nifty_daily.columns]
nifty_today_bull = {(ts.date() if hasattr(ts,'date') else ts)
                    for ts, row in nifty_daily.iterrows() if row["close"] > row["open"]}

# Pre-compute per-stock per-day gaps
print("Computing per-stock daily gaps...")
stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        day_open = float(grp["open"].iloc[0])
        gap_pct = ((day_open - prev_close) / prev_close * 100) if prev_close else 0.0
        avg_vol = float(grp["volume"].mean()) if "volume" in grp.columns else 1.0
        by_day[d] = {
            "day_open": day_open,
            "prev_close": prev_close,
            "gap_pct": gap_pct,
            "avg_vol": avg_vol,
            "price": day_open,
        }
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

# Build per-day aggregates
all_dates = sorted(set(d for days in stock_day_data.values() for d in days.keys()))

day_stats = {}
for d in all_dates:
    gaps = []
    weighted_gaps = []
    for sym, days in stock_day_data.items():
        if d in days and days[d]["prev_close"] is not None:
            g = days[d]["gap_pct"]
            w = days[d]["price"] * days[d]["avg_vol"]  # crude market cap proxy
            gaps.append(g)
            weighted_gaps.append((g, w))
    
    if not gaps:
        continue
    
    gaps_arr = np.array(gaps)
    day_stats[d] = {
        "avg_gap": float(np.mean(gaps_arr)),
        "median_gap": float(np.median(gaps_arr)),
        "pct_up": float(np.sum(gaps_arr > 0) / len(gaps_arr)),
        "pct_strong_up": float(np.sum(gaps_arr > 0.5) / len(gaps_arr)),  # strong gap > 0.5%
        "pct_very_strong": float(np.sum(gaps_arr > 1.0) / len(gaps_arr)),  # very strong > 1%
        "top10_avg": float(np.mean(sorted(gaps_arr, reverse=True)[:10])),
        "weighted_avg": float(sum(g*w for g,w in weighted_gaps) / sum(w for _,w in weighted_gaps)) if weighted_gaps else 0,
        "n_stocks": len(gaps),
    }

# Print daily stats summary
print(f"\n{'='*100}")
print("DAILY GAP STATS (all 50 stocks)")
print(f"{'='*100}")
for d in sorted(day_stats.keys())[:5]:
    s = day_stats[d]
    bull = "BULL" if d in nifty_today_bull else "BEAR"
    print(f"  {d} [{bull}] avg={s['avg_gap']:+.2f}% med={s['median_gap']:+.2f}% "
          f"up={s['pct_up']:.0%} strong={s['pct_strong_up']:.0%} top10={s['top10_avg']:+.2f}%")
print("  ...")
for d in sorted(day_stats.keys())[-3:]:
    s = day_stats[d]
    bull = "BULL" if d in nifty_today_bull else "BEAR"
    print(f"  {d} [{bull}] avg={s['avg_gap']:+.2f}% med={s['median_gap']:+.2f}% "
          f"up={s['pct_up']:.0%} strong={s['pct_strong_up']:.0%} top10={s['top10_avg']:+.2f}%")

# ===================================================================
# BUILD REGIME SETS
# ===================================================================
def days_where(metric, op, threshold):
    """Get dates where day_stats[metric] op threshold"""
    result = set()
    for d, s in day_stats.items():
        if metric in s:
            if op == ">=" and s[metric] >= threshold:
                result.add(d)
            elif op == ">" and s[metric] > threshold:
                result.add(d)
    return result

# ===================================================================
# TIMELINE + BACKTESTER (same as before)
# ===================================================================
all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

def portfolio_backtest(max_pos, allowed_dates=None, dynamic_breadth_thr=None):
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

results = []
def r(label, trades):
    if not trades: print(f"  {label:70s} | NO TRADES"); return
    pnls=[t["pnl"] for t in trades]; w=sum(1 for p in pnls if p>0)
    wr=w/len(pnls)*100; net=sum(pnls); ret=net/CAPITAL*100
    flag=" ***" if ret>=20 else (" <<<" if ret>5 else "")
    print(f"  {label:70s} | WR={wr:5.1f}% | Net={net:+10,.0f} | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")
    results.append({"label": label, "wr": wr, "net": net, "ret": ret, "t": len(pnls)})

# ===================================================================
# MEGA SWEEP
# ===================================================================
MP = 5

print(f"\n{'='*120}")
print(f"GAP MAGNITUDE DEEP SWEEP | max_pos={MP}")
print(f"{'='*120}")

# Baselines
print("\n--- BASELINES ---")
r("NO FILTER", portfolio_backtest(MP))
r("NIFTY_DAILY (CHEAT)", portfolio_backtest(MP, allowed_dates=nifty_today_bull))
r("GAP_UP >= 70% (prev best)", portfolio_backtest(MP, allowed_dates=days_where("pct_up",">=",0.70)))

# 1. Average gap thresholds
print("\n--- AVG GAP (all 50 stocks average gap %) ---")
for thr in [0.0, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0]:
    ds = days_where("avg_gap", ">=", thr)
    r(f"AVG_GAP >= {thr:.1f}% ({len(ds)}d)", portfolio_backtest(MP, allowed_dates=ds))

# 2. Median gap thresholds
print("\n--- MEDIAN GAP ---")
for thr in [0.0, 0.1, 0.2, 0.3, 0.5, 0.8]:
    ds = days_where("median_gap", ">=", thr)
    r(f"MEDIAN_GAP >= {thr:.1f}% ({len(ds)}d)", portfolio_backtest(MP, allowed_dates=ds))

# 3. Top 10 gappers average
print("\n--- TOP 10 GAPPERS AVG ---")
for thr in [0.5, 0.8, 1.0, 1.5, 2.0]:
    ds = days_where("top10_avg", ">=", thr)
    r(f"TOP10_AVG >= {thr:.1f}% ({len(ds)}d)", portfolio_backtest(MP, allowed_dates=ds))

# 4. Strong gap % (stocks with gap > 0.5%)
print("\n--- STRONG GAPPERS (gap > 0.5%) ---")
for thr in [0.30, 0.40, 0.50, 0.60, 0.70]:
    ds = days_where("pct_strong_up", ">=", thr)
    r(f"STRONG_GAP >= {int(thr*100)}% ({len(ds)}d)", portfolio_backtest(MP, allowed_dates=ds))

# 5. Very strong gap % (stocks with gap > 1%)
print("\n--- VERY STRONG GAPPERS (gap > 1%) ---")
for thr in [0.20, 0.30, 0.40, 0.50]:
    ds = days_where("pct_very_strong", ">=", thr)
    r(f"VSTRONG_GAP >= {int(thr*100)}% ({len(ds)}d)", portfolio_backtest(MP, allowed_dates=ds))

# 6. Weighted average gap (market cap proxy)
print("\n--- WEIGHTED AVG GAP ---")
for thr in [0.0, 0.1, 0.2, 0.3, 0.5, 0.8]:
    ds = days_where("weighted_avg", ">=", thr)
    r(f"WEIGHTED_GAP >= {thr:.1f}% ({len(ds)}d)", portfolio_backtest(MP, allowed_dates=ds))

# 7. Best combos: gap direction + magnitude
print("\n--- COMBOS: GAP_UP% + AVG_GAP magnitude ---")
for up_thr in [0.60, 0.65, 0.70]:
    for mag_thr in [0.1, 0.2, 0.3, 0.5]:
        combo = days_where("pct_up",">=",up_thr) & days_where("avg_gap",">=",mag_thr)
        r(f"UP>={int(up_thr*100)}% + AVG>={mag_thr}% ({len(combo)}d)",
          portfolio_backtest(MP, allowed_dates=combo))

# 8. Gap + Dynamic breadth
print("\n--- BEST GAP + DYNAMIC_BREADTH combos ---")
for gap_metric, gap_thr, gap_label in [
    ("avg_gap", 0.2, "AVG>=0.2"),
    ("avg_gap", 0.3, "AVG>=0.3"),
    ("pct_up", 0.70, "UP>=70"),
    ("top10_avg", 1.0, "TOP10>=1.0"),
    ("pct_strong_up", 0.50, "STRONG>=50"),
]:
    for db in [0.55, 0.60, 0.65, 0.70]:
        ds = days_where(gap_metric, ">=", gap_thr)
        r(f"{gap_label} + DB>={int(db*100)}%",
          portfolio_backtest(MP, allowed_dates=ds, dynamic_breadth_thr=db))

# ===================================================================
# TOP 15
# ===================================================================
print(f"\n{'='*120}")
print("TOP 15 BEST CONFIGS (by Net Return)")
print(f"{'='*120}")
top = sorted(results, key=lambda x: x["ret"], reverse=True)[:15]
for i, rx in enumerate(top, 1):
    flag = " *** BEST ***" if i == 1 else ""
    print(f"  #{i:2d} | {rx['label']:70s} | WR={rx['wr']:5.1f}% | Net={rx['net']:+10,.0f} | "
          f"Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{flag}")

# Also show worst 5 to understand what NOT to do
print(f"\n--- WORST 5 (avoid these) ---")
worst = sorted(results, key=lambda x: x["ret"])[:5]
for rx in worst:
    print(f"  AVOID | {rx['label']:70s} | Ret={rx['ret']:+6.1f}%")

print(f"\nTotal configs tested: {len(results)}")
print("DONE.")
