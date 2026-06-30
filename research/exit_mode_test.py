"""
Quick test: STRONG_GAP_40 + FULL EXIT at RSI>72 (instead of partial half-sell)
Compare against:
  A. STRONG40 | partial ON (RSI>72, half sell) = +21.8%, WR 59.8%  [prev best]
  B. STRONG40 | partial OFF (no RSI exit)       = +19.0%, WR 43.4%  [prev]
  C. STRONG40 | FULL EXIT at RSI>72             = ???               [new test]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
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

# Gap stats
stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        by_day[d] = {"day_open": float(grp["open"].iloc[0]), "prev_close": prev_close}
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

# STRONG_GAP_40 days
def compute_strong_gap_days():
    result = set()
    all_dates = set(d for days in stock_day_data.values() for d in days.keys())
    for d in all_dates:
        strong = 0; total = 0
        for sym, days in stock_day_data.items():
            if d in days and days[d]["prev_close"] is not None:
                gap = (days[d]["day_open"] - days[d]["prev_close"]) / days[d]["prev_close"]
                total += 1
                if gap >= 0.005: strong += 1
        if total > 0 and strong / total >= 0.40:
            result.add(d)
    return result

strong40_days = compute_strong_gap_days()
print(f"STRONG_GAP_40 days: {len(strong40_days)} out of 58 trading days")

all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

def portfolio_backtest(max_pos, allowed_dates, exit_mode="partial_half"):
    """
    exit_mode:
      'partial_half' = RSI>72 → sell half, keep rest (current config)
      'full_at_rsi'  = RSI>72 → sell EVERYTHING immediately
      'no_partial'   = never partial exit (only SL/target/sell signal/EOD)
    """
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
            close=float(cc["close"]); high=float(cc["high"]); low=float(cc["low"])
            ep=pos["ep"]; qty=pos["qty"]

            # RSI-based exit
            rsi_val = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 0

            if exit_mode == "partial_half" and not pos["half"] and rsi_val > 72 and qty > 1:
                h = qty // 2
                trades.append({"pnl": (close - ep) * h, "note": "PARTIAL"})
                pos["qty"] -= h; pos["half"] = True; qty = pos["qty"]

            elif exit_mode == "full_at_rsi" and not pos["rsi_exited"] and rsi_val > 72:
                # Full exit at RSI > 72
                trades.append({"pnl": (close - ep) * qty, "note": "RSI_FULL"})
                closed.append(sym); continue

            ex = None
            if low <= pos["tsl"]: ex = min(pos["tsl"], float(cc["open"]))
            elif high >= pos["tgt"]: ex = max(pos["tgt"], float(cc["open"]))
            elif ts.hour == 15 and ts.minute >= 15: ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1<len(df): ex = float(df.iloc[idx+1]["open"])
            if ex:
                trades.append({"pnl": (ex - ep) * qty, "note": "FULL"}); closed.append(sym)
            else:
                sl_pct = abs(ep - pos["sl"]) / ep if ep > 0 else 0
                if high >= ep + ep * sl_pct * tsl_activation_ratio: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high * (1 - trailing_sl_percent))
                    if n > pos["tsl"]: pos["tsl"] = n
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
                if idx+1<len(df):
                    ep = float(df.iloc[idx+1]["open"]); qty = int(per_slot // ep)
                    if qty < 1: continue
                    sl_p = calculate_stop_loss(ep, "BUY")
                    positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p,
                                      "tgt": calculate_target(ep, sl_p),
                                      "tsl": sl_p, "tsl_on": False,
                                      "half": False, "rsi_exited": False}
    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "note": "EOD"})
    return trades

def report(label, trades):
    if not trades: print(f"  {label:55s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    partials = sum(1 for t in trades if t.get("note") in ("PARTIAL", "RSI_FULL"))
    print(f"  {label:55s} | WR={wr:5.1f}% | Net={net:+10,.0f} | Ret={ret:+6.1f}% | T={len(pnls):4d} | RSI_exits={partials}")

print(f"\n{'='*110}")
print("STRONG_GAP_40 | max_pos=3 | EXIT MODE COMPARISON")
print(f"{'='*110}")
for mp in [3, 5]:
    print(f"\n--- max_pos={mp} ---")
    report(f"A. partial_half (RSI>72 sell 50%) CURRENT",
           portfolio_backtest(mp, strong40_days, exit_mode="partial_half"))
    report(f"B. no_partial   (only SL/target/signal)",
           portfolio_backtest(mp, strong40_days, exit_mode="no_partial"))
    report(f"C. full_at_rsi  (RSI>72 sell 100%) NEW",
           portfolio_backtest(mp, strong40_days, exit_mode="full_at_rsi"))

print(f"\n{'='*110}")
print("DONE.")
