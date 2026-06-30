"""
PRACTICAL Nifty Regime Tests — NO look-ahead bias
==================================================
Tests regime filters that are ACTUALLY usable in real-time:

1. NIFTY_PREV_DAY: Did Nifty close > open YESTERDAY? (known at 9:15 AM)
2. NIFTY_10AM:     At 10:00 AM, is Nifty > today's open? (known at 10 AM)
3. NIFTY_DAILY:    [CHEATING] Nifty close > open today (look-ahead, for reference only)
4. NO_FILTER:      Baseline

Capital: 1L | 5x margin | max_pos = 5, 7, 10
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

# Load Nifty daily data for regime filters
print("Loading Nifty daily data...")
nifty_daily = _fetch_yahoo_history("^NSEI", period="60d", interval="1d")
nifty_daily.columns = [c.lower() for c in nifty_daily.columns]
print(f"  {len(nifty_daily)} daily Nifty candles")

# ===================================================================
# BUILD REGIME SETS (all knowable at the right time, no cheating)
# ===================================================================

# 1. NIFTY_PREV_DAY: yesterday Nifty closed > open → trade today
#    Knowable at: 9:15 AM (before market open)
nifty_prev_bull = set()
nifty_dates = sorted(nifty_daily.index)
for i in range(1, len(nifty_dates)):
    yesterday = nifty_daily.loc[nifty_dates[i-1]]
    if yesterday["close"] > yesterday["open"]:
        d = nifty_dates[i]
        nifty_prev_bull.add(d.date() if hasattr(d, 'date') else d)
print(f"  NIFTY_PREV_DAY bull days: {len(nifty_prev_bull)}")

# 2. NIFTY_DAILY: today Nifty close > open (LOOK-AHEAD — reference only)
nifty_today_bull = set()
for ts, row in nifty_daily.iterrows():
    if row["close"] > row["open"]:
        d = ts.date() if hasattr(ts, 'date') else ts
        nifty_today_bull.add(d)
print(f"  NIFTY_DAILY (look-ahead) bull days: {len(nifty_today_bull)}")

# 3. NIFTY_10AM: At 10:00 AM, is Nifty > today's open?
#    We use stock data as proxy since ^NSEI 5m is unavailable
#    Approach: check if 60%+ of Nifty50 stocks are above their day's open at 10 AM
#    Knowable at: 10:00 AM
nifty_10am_bull = set()
morning_check = defaultdict(lambda: {"above": 0, "total": 0})
for sym, df in stock_dfs.items():
    # Group by date, get day's first candle open and 10:00 price
    for d, grp in df.groupby(df.index.date):
        day_open = float(grp["open"].iloc[0])
        # Find candle closest to 10:00 AM
        bars_10 = grp[(grp.index.time >= dtime(9,55)) & (grp.index.time <= dtime(10,5))]
        if not bars_10.empty:
            price_10 = float(bars_10["close"].iloc[-1])
            morning_check[d]["total"] += 1
            if price_10 > day_open:
                morning_check[d]["above"] += 1

for d, counts in morning_check.items():
    if counts["total"] > 0 and counts["above"] / counts["total"] >= 0.60:
        nifty_10am_bull.add(d)
print(f"  NIFTY_10AM (60% stocks > open at 10AM) bull days: {len(nifty_10am_bull)}")

# 4. Combination: NIFTY_PREV_DAY AND NIFTY_10AM both true
nifty_combo = nifty_prev_bull & nifty_10am_bull
print(f"  COMBO (prev + 10am): {len(nifty_combo)} bull days")

total_days = len(set(d for df in stock_dfs.values() for d in df.index.date))
print(f"  Total trading days: {total_days}")

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
def portfolio_backtest(max_pos, allowed_dates=None, partial_exit=True,
                       entry_after_time=None):
    """
    entry_after_time: if set, only allow entries after this time (e.g., 10:05 AM
    for 10AM regime check)
    """
    per_slot = BUYING_POWER * position_size_margin / max_pos
    positions = {}; trades = []; cur_date = None

    for ts in timeline:
        d = ts.date()
        if d != cur_date: cur_date = d

        # Manage existing positions
        closed = []
        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 10: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos = positions[sym]
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]

            # Partial exit
            if partial_exit and not pos["half"] and "rsi" in cc.index and pd.notna(cc["rsi"]):
                if float(cc["rsi"]) > 72 and qty > 1:
                    half = qty // 2
                    trades.append({"pnl": (close - ep) * half, "exit": "PARTIAL"})
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
                trades.append({"pnl": (ex - ep) * qty, "exit": "FULL"}); closed.append(sym)
            else:
                sl_pct = abs(ep - pos["sl"]) / ep if ep > 0 else 0
                if high >= ep + ep * sl_pct * tsl_activation_ratio: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    new = round_to_tick(high * (1 - trailing_sl_percent))
                    if new > pos["tsl"]: pos["tsl"] = new

        for s in closed: del positions[s]

        # New entries
        if len(positions) >= max_pos: continue
        if ts.hour >= 15: continue
        if allowed_dates is not None and d not in allowed_dates: continue
        if entry_after_time and ts.time() < entry_after_time: continue

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
        trades.append({"pnl": (float(stock_dfs[sym]["close"].iloc[-1]) - pos["ep"]) * pos["qty"], "exit": "EOD"})
    return trades

def report(label, trades):
    if not trades:
        print(f"  {label:60s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    ann = ret * (252/58)  # annualized (58 trading days)
    flag = " <<<" if wr >= 50 and ret > 0 else ""
    print(f"  {label:60s} | WR={wr:5.1f}% | Net=Rs.{net:+10,.0f} | 60d={ret:+6.1f}% | "
          f"Ann~{ann:+6.0f}% | Trades={len(pnls):4d}{flag}")

# ===================================================================
# RUN ALL
# ===================================================================
regimes = [
    ("NO FILTER",                    None,              None),
    ("NIFTY_PREV_DAY (no cheating)", nifty_prev_bull,   None),
    ("NIFTY_10AM breadth (no cheat)",nifty_10am_bull,   dtime(10,5)),
    ("COMBO prev+10am (strict)",     nifty_combo,       dtime(10,5)),
    ("NIFTY_DAILY (LOOK-AHEAD ref)", nifty_today_bull,  None),
]

for mp in [5, 7, 10]:
    print(f"\n{'='*130}")
    print(f"max_open_positions = {mp}")
    print(f"{'='*130}")
    for pe_label, pe in [("partial=ON", True), ("partial=OFF", False)]:
        print(f"\n  --- {pe_label} ---")
        for rlabel, allowed, after_time in regimes:
            trades = portfolio_backtest(max_pos=mp, allowed_dates=allowed,
                                        partial_exit=pe, entry_after_time=after_time)
            report(f"{rlabel} | {pe_label}", trades)

print(f"\n{'='*130}")
print("DONE. 'Ann~' = annualized estimate (60d x 252/58).")
print("NIFTY_DAILY is REFERENCE ONLY (uses future data). Other filters are practical.")
print(f"{'='*130}")
