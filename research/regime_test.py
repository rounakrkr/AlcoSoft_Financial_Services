"""
Regime Filter Comparison Test
Uses exact BacktestRunner engine to compare 4 regime approaches.

Tests:
  0. BASELINE       — No filter
  1. PREV_NIFTY     — Did Nifty close up yesterday?
  2. PREV_BREADTH   — Did 55%+ Nifty50 stocks close up yesterday?
  3. INTRADAY_VWAP  — Is Nifty above its VWAP at signal time?
  4. MORNING_45     — At 9:45 AM, are 55%+ stocks above their VWAP?
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from datetime import date, time
from collections import defaultdict
import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

from screener.morning_screener import NIFTY_50, _fetch_yahoo_history
from core.strategy import (CONDITION_REGISTRY, StrategySetEvaluator,
                            StrategyEvaluationContext, _build_indicators)
from core.strategy_sets import load_strategy_sets
from core.order_executor import calculate_stop_loss, calculate_target, round_to_tick
from core.trading_settings import get as cfg

BUY_STRATEGY  = "BUY_STREAK_MOMENTUM_BREAKOUT"
SELL_STRATEGY = "SELL_EMA_MOMENTUM_LOSS"
CAPITAL = 20000.0
MARGIN  = 5.0
BUYING_POWER = CAPITAL * MARGIN   # 100,000

config       = load_strategy_sets()
buy_set_def  = next(s for s in config.buy_sets  if s.name == BUY_STRATEGY)
sell_set_def = next(s for s in config.sell_sets if s.name == SELL_STRATEGY)
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)

stop_loss_percent    = float(cfg("risk", "stop_loss_percent",    0.01))
tsl_activation_ratio = float(cfg("risk", "tsl_activation_ratio", 1.4))
trailing_sl_percent  = float(cfg("risk", "trailing_sl_percent",  0.008))
position_size_margin = float(cfg("risk", "position_size_margin", 1.0))
MAX_POSITIONS        = 3   # 20K / 3 = ~33K per slot

# ─────────────────────────────────────────────────────────────
# DATA LOAD
# ─────────────────────────────────────────────────────────────
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

print("Loading data (this may take a few minutes)...")
stock_dfs = {}
for i, sym in enumerate(NIFTY_50):
    print(f"  {i+1}/{len(NIFTY_50)}: {sym}", end="\r")
    df = fetch(sym)
    if not df.empty:
        stock_dfs[sym] = df
print(f"\n{len(stock_dfs)} stocks loaded.")

# Also load Nifty index (^NSEI) for intraday VWAP regime
print("Loading Nifty50 index data...")
try:
    nifty_df = _fetch_yahoo_history("^NSEI", period="60d", interval="5m")
    nifty_df.columns = [c.lower() for c in nifty_df.columns]
    nifty_df.dropna(subset=["close"], inplace=True)
    nifty_df["bucket"] = nifty_df.index
    nifty_df = _build_indicators(nifty_df)
    nifty_df.dropna(subset=["vwap"], inplace=True)
    print(f"Nifty data loaded: {len(nifty_df)} candles.")
except Exception as e:
    nifty_df = pd.DataFrame()
    print(f"Could not load Nifty data: {e}")

# ─────────────────────────────────────────────────────────────
# PRE-COMPUTE REGIME SETS
# ─────────────────────────────────────────────────────────────

# 1. PREV_NIFTY: days after Nifty closed up
def compute_prev_nifty_days(all_dfs):
    """Set of dates where Nifty closed up the PREVIOUS day."""
    # Use any liquid stock as Nifty proxy if ^NSEI failed
    ref = nifty_df if not nifty_df.empty else all_dfs.get("RELIANCE", pd.DataFrame())
    if ref.empty: return set()
    daily = ref.groupby(ref.index.date)["close"].last()
    prev_return = daily.pct_change()
    # Dates where YESTERDAY was positive
    bull_next_days = set()
    dates = sorted(daily.index)
    for i in range(1, len(dates)):
        if prev_return.iloc[i] > 0:
            bull_next_days.add(dates[i])
    return bull_next_days

# 2. PREV_BREADTH: days after 55%+ stocks closed up
def compute_prev_breadth_days(all_dfs, threshold=0.55):
    """Set of dates where previous day had 55%+ breadth."""
    daily_bull = defaultdict(int)
    daily_total = defaultdict(int)
    for sym, df in all_dfs.items():
        by_day = df.groupby(df.index.date)
        for d, grp in by_day:
            daily_total[d] += 1
            if grp["close"].iloc[-1] > grp["open"].iloc[0]:
                daily_bull[d] += 1
    all_dates = sorted(daily_total.keys())
    bull_next_days = set()
    for i in range(1, len(all_dates)):
        prev_d = all_dates[i-1]
        today_d = all_dates[i]
        if daily_total[prev_d] > 0:
            if daily_bull[prev_d] / daily_total[prev_d] >= threshold:
                bull_next_days.add(today_d)
    return bull_next_days

# 3. INTRADAY_VWAP: Nifty above its VWAP at signal time
def build_nifty_vwap_lookup(nifty_df):
    """Map of timestamp -> bool (nifty above vwap at that time)."""
    if nifty_df.empty: return {}
    return {ts: row["close"] >= row["vwap"]
            for ts, row in nifty_df.iterrows()}

# 4. MORNING_BREADTH: 55%+ stocks above VWAP at 9:45
def compute_morning_breadth_days(all_dfs, morning_time=time(9, 45), threshold=0.55):
    """Set of dates where at 9:45 AM, 55%+ stocks were above their VWAP."""
    morning_bull  = defaultdict(int)
    morning_total = defaultdict(int)
    for sym, df in all_dfs.items():
        morning_bars = df[df.index.time == morning_time]
        for ts, row in morning_bars.iterrows():
            d = ts.date()
            morning_total[d] += 1
            if row["close"] >= row.get("vwap", 0):
                morning_bull[d] += 1
    return {d for d in morning_total
            if morning_total[d] > 0 and morning_bull[d]/morning_total[d] >= threshold}

print("Computing regime filters...")
prev_nifty_days    = compute_prev_nifty_days(stock_dfs)
prev_breadth_days  = compute_prev_breadth_days(stock_dfs)
nifty_vwap_lookup  = build_nifty_vwap_lookup(nifty_df)
morning_bread_days = compute_morning_breadth_days(stock_dfs)

print(f"  PREV_NIFTY    bull days: {len(prev_nifty_days)}")
print(f"  PREV_BREADTH  bull days: {len(prev_breadth_days)}")
print(f"  MORNING_45    bull days: {len(morning_bread_days)}")

# ─────────────────────────────────────────────────────────────
# BACKTESTER (single stock, with regime + partial exit)
# ─────────────────────────────────────────────────────────────
def backtest(df, sym, allowed_dates=None, intraday_vwap_lookup=None, partial_exit=True):
    """
    allowed_dates: set of date objects to allow entry (None = all days)
    intraday_vwap_lookup: dict of {timestamp: bool} for Nifty VWAP check
    partial_exit: exit 50% at RSI > 72
    """
    trades = []
    in_pos = False
    ep = 0.0; qty = 0; half_done = False
    sl = 0.0; target = 0.0; tsl = 0.0; tsl_on = False
    trades_today = 0; cur_date = None; entry_time = None

    for i in range(10, len(df)):
        sliced = df.iloc[:i+1]
        cc = sliced.iloc[-1]
        ct = sliced.index[-1]
        close = cc["close"]; high = cc["high"]; low = cc["low"]
        d = ct.date()

        if d != cur_date:
            cur_date = d
            trades_today = 0

        if not in_pos:
            if ct.hour == 15: continue
            if trades_today >= 1: continue

            # Regime check
            if allowed_dates is not None and d not in allowed_dates:
                continue
            if intraday_vwap_lookup is not None:
                if not intraday_vwap_lookup.get(ct, False):
                    continue

            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced,
                                            pattern_df=sliced, ws_count=0)
            cond = evaluator._evaluate_conditions(buy_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                if i + 1 < len(df):
                    ep         = float(df.iloc[i+1]["open"])
                    entry_time = df.index[i+1]
                    qty        = int(BUYING_POWER * position_size_margin // ep // MAX_POSITIONS)
                    if qty < 1: continue
                    sl         = calculate_stop_loss(ep, "BUY")
                    target     = calculate_target(ep, sl)
                    tsl        = sl; tsl_on = False; half_done = False
                    in_pos     = True; trades_today += 1
        else:
            # Partial exit at RSI > 72
            if partial_exit and not half_done and "rsi" in cc and pd.notna(cc["rsi"]):
                if float(cc["rsi"]) > 72 and qty > 1:
                    half = qty // 2
                    trades.append({"pnl": (close - ep) * half, "exit": "PARTIAL"})
                    qty -= half; half_done = True

            ex = None
            if low <= tsl:
                ex = min(tsl, cc["open"])
            elif high >= target:
                ex = max(target, cc["open"])
            elif ct.hour == 15 and ct.minute >= 15:
                ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if i + 1 < len(df):
                        ex = float(df.iloc[i+1]["open"])

            if ex is None:
                sl_pct = abs(ep - sl) / ep
                thresh = ep + ep * sl_pct * tsl_activation_ratio
                if high >= thresh: tsl_on = True
                if tsl_on:
                    new_tsl = round_to_tick(high * (1 - trailing_sl_percent))
                    if new_tsl > tsl: tsl = new_tsl
            else:
                trades.append({"pnl": (float(ex) - ep) * qty, "exit": "FULL"})
                in_pos = False; qty = 0; half_done = False

    return trades

# ─────────────────────────────────────────────────────────────
# RUN ALL EXPERIMENTS
# ─────────────────────────────────────────────────────────────
def report(label, trades):
    if not trades:
        print(f"  {label:45s}  NO TRADES")
        return
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    wr   = len(wins) / len(pnls) * 100
    ret  = sum(pnls) / CAPITAL * 100
    n    = len(pnls)
    flag = " <<< GOAL MET!" if wr >= 50 and ret >= 250 else ""
    print(f"  {label:45s}  WR={wr:5.2f}%  Ret={ret:8.2f}%  Trades={n}{flag}")

experiments = [
    ("BASELINE (no filter)",          None,              None,              True),
    ("PREV_NIFTY (kal Nifty up)",     prev_nifty_days,   None,              True),
    ("PREV_BREADTH (55% stocks up)",  prev_breadth_days, None,              True),
    ("INTRADAY_NIFTY_VWAP",           None,              nifty_vwap_lookup, True),
    ("MORNING_45 (9:45 breadth)",     morning_bread_days,None,              True),
    # Without partial exit for comparison
    ("PREV_BREADTH (no partial exit)",prev_breadth_days, None,              False),
    ("MORNING_45  (no partial exit)", morning_bread_days,None,              False),
]

for label, allowed, vwap_lk, partial in experiments:
    print(f"\n{'='*70}")
    print(f"{label}")
    print(f"{'='*70}")
    all_trades = []
    for sym, df in stock_dfs.items():
        all_trades.extend(backtest(df, sym,
                                   allowed_dates=allowed,
                                   intraday_vwap_lookup=vwap_lk,
                                   partial_exit=partial))
    report(label, all_trades)

print("\nDone.")
