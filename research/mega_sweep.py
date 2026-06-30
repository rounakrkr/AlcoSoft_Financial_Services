"""
MEGA REGIME SWEEP — Hundreds of combinations
=============================================
Every practical idea to approximate "is today bullish?" WITHOUT cheating.

Ideas tested:
  1. FIRST_CANDLE: First 5-min candle of Nifty50 stocks — green or red?
  2. DYNAMIC_BREADTH: At signal time, what % of stocks are above their day's open?
  3. GAP_UP: Did most stocks open above yesterday's close?
  4. FIRST_15MIN_TREND: After first 15 min, is trend up?
  5. Entry time restrictions: only after 9:20, 9:25, 9:30
  6. Multiple breadth thresholds: 40%, 50%, 55%, 60%, 65%, 70%
  7. Combinations of above
  8. NIFTY_DAILY (reference/cheating)
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

# Nifty daily for reference
nifty_daily = _fetch_yahoo_history("^NSEI", period="60d", interval="1d")
nifty_daily.columns = [c.lower() for c in nifty_daily.columns]
nifty_today_bull = set()
for ts, row in nifty_daily.iterrows():
    if row["close"] > row["open"]:
        nifty_today_bull.add(ts.date() if hasattr(ts, 'date') else ts)

# ===================================================================
# PRE-COMPUTE per-stock, per-day data
# ===================================================================
print("Pre-computing daily data...")

# For each stock, for each day: day_open, prev_day_close, first candle info
stock_day_data = {}  # {sym: {date: {day_open, prev_close, first_candle_green}}}
for sym, df in stock_dfs.items():
    by_day = {}
    prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        day_open = float(grp["open"].iloc[0])
        first_close = float(grp["close"].iloc[0])
        by_day[d] = {
            "day_open": day_open,
            "prev_close": prev_close,
            "first_candle_green": first_close > day_open,
            "first_close": first_close,
        }
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

# 1. FIRST_CANDLE days: X% of stocks have green first 5-min candle
def compute_first_candle_days(threshold):
    day_green = defaultdict(int)
    day_total = defaultdict(int)
    for sym, days in stock_day_data.items():
        for d, info in days.items():
            day_total[d] += 1
            if info["first_candle_green"]:
                day_green[d] += 1
    return {d for d in day_total
            if day_total[d] > 0 and day_green[d]/day_total[d] >= threshold}

# 2. GAP_UP days: X% of stocks opened above previous close
def compute_gap_up_days(threshold):
    day_gap = defaultdict(int)
    day_total = defaultdict(int)
    for sym, days in stock_day_data.items():
        for d, info in days.items():
            if info["prev_close"] is not None:
                day_total[d] += 1
                if info["day_open"] > info["prev_close"]:
                    day_gap[d] += 1
    return {d for d in day_total
            if day_total[d] > 0 and day_gap[d]/day_total[d] >= threshold}

# 3. FIRST_15MIN: at 9:30, X% of stocks above their day open
def compute_first_15min_days(threshold, check_time=dtime(9,30)):
    day_above = defaultdict(int)
    day_total = defaultdict(int)
    for sym, df in stock_dfs.items():
        bars = df[(df.index.time >= dtime(9,25)) & (df.index.time <= dtime(9,35))]
        for ts, row in bars.iterrows():
            d = ts.date()
            if d in stock_day_data.get(sym, {}):
                day_total[d] += 1
                if float(row["close"]) > stock_day_data[sym][d]["day_open"]:
                    day_above[d] += 1
    return {d for d in day_total
            if day_total[d] > 0 and day_above[d]/day_total[d] >= threshold}

# Pre-compute all threshold variants
print("Computing regime variants...")
first_candle = {}
gap_up = {}
first_15min = {}
for thr in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
    k = int(thr*100)
    first_candle[k] = compute_first_candle_days(thr)
    gap_up[k] = compute_gap_up_days(thr)
    first_15min[k] = compute_first_15min_days(thr)
    print(f"  thr={k}%: FC={len(first_candle[k])}d  GAP={len(gap_up[k])}d  F15={len(first_15min[k])}d")

# ===================================================================
# DYNAMIC BREADTH at signal time
# ===================================================================
# This is computed INSIDE the backtester — at the moment of a buy signal,
# check how many stocks are currently above their day's open.
# No pre-computation needed.

# ===================================================================
# TIMELINE
# ===================================================================
all_timestamps = set()
for df in stock_dfs.values():
    all_timestamps.update(df.index.tolist())
timeline = sorted(all_timestamps)
stock_ts_map = {}
for sym, df in stock_dfs.items():
    stock_ts_map[sym] = {ts: idx for idx, ts in enumerate(df.index)}

# ===================================================================
# PORTFOLIO BACKTESTER
# ===================================================================
def portfolio_backtest(max_pos, allowed_dates=None, partial_exit=False,
                       entry_after=None, dynamic_breadth_thr=None):
    """
    dynamic_breadth_thr: if set (e.g., 0.55), at signal time check if
    55%+ of stocks are above their day's open RIGHT NOW.
    """
    per_slot = BUYING_POWER * position_size_margin / max_pos
    positions = {}; trades = []; cur_date = None

    for ts in timeline:
        d = ts.date()
        if d != cur_date: cur_date = d

        closed = []
        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos = positions[sym]
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]

            if partial_exit and not pos["half"] and "rsi" in cc.index and pd.notna(cc["rsi"]):
                if float(cc["rsi"]) > 72 and qty > 1:
                    half = qty // 2
                    trades.append({"pnl": (close - ep) * half, "exit": "P"})
                    pos["qty"] -= half; pos["half"] = True; qty = pos["qty"]

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
                trades.append({"pnl": (ex - ep) * qty, "exit": "F"}); closed.append(sym)
            else:
                sl_pct = abs(ep - pos["sl"]) / ep if ep > 0 else 0
                if high >= ep + ep * sl_pct * tsl_activation_ratio: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    new = round_to_tick(high * (1 - trailing_sl_percent))
                    if new > pos["tsl"]: pos["tsl"] = new

        for s in closed: del positions[s]

        if len(positions) >= max_pos: continue
        if ts.hour >= 15: continue
        if allowed_dates is not None and d not in allowed_dates: continue
        if entry_after and ts.time() < entry_after: continue

        # Dynamic breadth check at signal time
        if dynamic_breadth_thr is not None:
            above = 0; total = 0
            for s2, days in stock_day_data.items():
                if d in days and ts in stock_ts_map.get(s2, {}):
                    df2 = stock_dfs[s2]; idx2 = stock_ts_map[s2][ts]
                    total += 1
                    if float(df2.iloc[idx2]["close"]) > days[d]["day_open"]:
                        above += 1
            if total == 0 or above / total < dynamic_breadth_thr:
                continue

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
                    ep = float(df.iloc[idx+1]["open"])
                    qty = int(per_slot // ep)
                    if qty < 1: continue
                    sl_p = calculate_stop_loss(ep, "BUY")
                    positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p,
                                      "tgt": calculate_target(ep, sl_p),
                                      "tsl": sl_p, "tsl_on": False, "half": False}

    for sym, pos in positions.items():
        trades.append({"pnl": (float(stock_dfs[sym]["close"].iloc[-1]) - pos["ep"]) * pos["qty"], "exit": "E"})
    return trades

results = []
def report(label, trades, config_id=""):
    if not trades:
        print(f"  {label:65s} | NO TRADES")
        return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    flag = " ***" if ret >= 30 else (" <<<" if ret > 0 and wr >= 40 else "")
    print(f"  {label:65s} | WR={wr:5.1f}% | Net={net:+10,.0f} | Ret={ret:+6.1f}%| T={len(pnls):4d}{flag}")
    results.append({"label": label, "wr": wr, "net": net, "ret": ret, "trades": len(pnls)})

# ===================================================================
# RUN MEGA SWEEP
# ===================================================================
MP = 5  # best max_pos from previous tests

print(f"\n{'='*120}")
print(f"MEGA SWEEP | max_pos={MP} | Capital=1L | 5x margin")
print(f"{'='*120}")

# Baseline
print("\n--- BASELINES ---")
report("NO FILTER",
       portfolio_backtest(MP))
report("NIFTY_DAILY (CHEATING reference)",
       portfolio_backtest(MP, allowed_dates=nifty_today_bull))

# A. First candle regimes
print("\n--- FIRST CANDLE (9:15-9:20 green candle %) ---")
for thr in [40, 45, 50, 55, 60, 65, 70]:
    report(f"FIRST_CANDLE >= {thr}%",
           portfolio_backtest(MP, allowed_dates=first_candle[thr]))

# B. Gap up regimes
print("\n--- GAP UP (open > prev close %) ---")
for thr in [40, 45, 50, 55, 60, 65, 70]:
    report(f"GAP_UP >= {thr}%",
           portfolio_backtest(MP, allowed_dates=gap_up[thr]))

# C. First 15 min (9:30 check)
print("\n--- FIRST 15MIN (9:30 above day open %) ---")
for thr in [40, 45, 50, 55, 60, 65, 70]:
    report(f"FIRST_15MIN >= {thr}%",
           portfolio_backtest(MP, allowed_dates=first_15min[thr], entry_after=dtime(9,35)))

# D. Dynamic breadth at signal time (THE BIG ONE)
print("\n--- DYNAMIC BREADTH (at signal time, stocks above day open %) ---")
for thr in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
    report(f"DYNAMIC_BREADTH >= {int(thr*100)}% (realtime)",
           portfolio_backtest(MP, dynamic_breadth_thr=thr))

# E. Combos: First candle + Dynamic breadth
print("\n--- COMBOS: FIRST_CANDLE + DYNAMIC_BREADTH ---")
for fc_thr in [50, 55, 60]:
    for db_thr in [0.50, 0.55, 0.60]:
        report(f"FC>={fc_thr}% + DB>={int(db_thr*100)}%",
               portfolio_backtest(MP, allowed_dates=first_candle[fc_thr],
                                  dynamic_breadth_thr=db_thr))

# F. Combos: Gap up + Dynamic breadth
print("\n--- COMBOS: GAP_UP + DYNAMIC_BREADTH ---")
for gu_thr in [50, 55, 60]:
    for db_thr in [0.50, 0.55, 0.60]:
        report(f"GAP>={gu_thr}% + DB>={int(db_thr*100)}%",
               portfolio_backtest(MP, allowed_dates=gap_up[gu_thr],
                                  dynamic_breadth_thr=db_thr))

# G. Combos: First candle + Gap up
print("\n--- COMBOS: FIRST_CANDLE + GAP_UP ---")
for fc_thr in [50, 55, 60]:
    for gu_thr in [50, 55, 60]:
        combo_days = first_candle[fc_thr] & gap_up[gu_thr]
        report(f"FC>={fc_thr}% & GAP>={gu_thr}% ({len(combo_days)}d)",
               portfolio_backtest(MP, allowed_dates=combo_days))

# H. Entry time restrictions with dynamic breadth
print("\n--- ENTRY TIME + DYNAMIC_BREADTH ---")
for after in [dtime(9,20), dtime(9,25), dtime(9,30), dtime(9,35)]:
    for db_thr in [0.50, 0.55, 0.60]:
        report(f"after {after.strftime('%H:%M')} + DB>={int(db_thr*100)}%",
               portfolio_backtest(MP, entry_after=after, dynamic_breadth_thr=db_thr))

# I. Triple combo: FC + GAP + DB
print("\n--- TRIPLE COMBOS ---")
for fc in [50, 55]:
    for gu in [50, 55]:
        for db in [0.50, 0.55]:
            combo_days = first_candle[fc] & gap_up[gu]
            report(f"FC>={fc} & GAP>={gu} & DB>={int(db*100)}",
                   portfolio_backtest(MP, allowed_dates=combo_days, dynamic_breadth_thr=db))

# ===================================================================
# TOP 10 RESULTS
# ===================================================================
print(f"\n{'='*120}")
print("TOP 10 BEST CONFIGS (by Net Return)")
print(f"{'='*120}")
top = sorted(results, key=lambda x: x["ret"], reverse=True)[:10]
for i, r in enumerate(top, 1):
    flag = " *** BEST ***" if i == 1 else ""
    print(f"  #{i:2d} | {r['label']:65s} | WR={r['wr']:5.1f}% | Net={r['net']:+10,.0f} | "
          f"Ret={r['ret']:+6.1f}% | Trades={r['trades']:4d}{flag}")

print(f"\n{'='*120}")
print(f"Total configs tested: {len(results)}")
print(f"{'='*120}")
