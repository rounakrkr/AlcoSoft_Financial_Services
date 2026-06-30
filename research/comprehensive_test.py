"""
PRACTICAL Portfolio-Level Backtest
==================================
Simulates REAL trading:
  - 1 Lakh capital, 5x margin = 5L buying power
  - Global max_open_positions enforced across ALL stocks
  - When all slots full → NO new trades until one closes
  - Time-synchronized: all stocks scanned candle-by-candle
  - No brokerage (as requested)
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

CAPITAL = 100000.0
MARGIN  = 5.0
BUYING_POWER = CAPITAL * MARGIN

config       = load_strategy_sets()
buy_set_def  = next(s for s in config.buy_sets  if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
sell_set_def = next(s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS")
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)

tsl_activation_ratio = float(cfg("risk", "tsl_activation_ratio", 1.4))
trailing_sl_percent  = float(cfg("risk", "trailing_sl_percent",  0.008))
position_size_margin = float(cfg("risk", "position_size_margin", 1.0))

# ═══════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════
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

print("=" * 100)
print("PRACTICAL PORTFOLIO BACKTEST")
print("Capital: Rs.1,00,000 | Margin: 5x | Buying Power: Rs.5,00,000")
print("Global max_open_positions enforced — just like real trading")
print("=" * 100)

print("\nLoading stocks...")
stock_dfs = {}
for i, sym in enumerate(NIFTY_50):
    print(f"  {i+1}/{len(NIFTY_50)}: {sym}    ", end="\r")
    df = fetch(sym)
    if not df.empty:
        stock_dfs[sym] = df
print(f"\n{len(stock_dfs)} stocks loaded.")

# ═══════════════════════════════════════════════════════════
# REGIME
# ═══════════════════════════════════════════════════════════
def compute_prev_breadth_days(all_dfs, threshold=0.55):
    daily_bull  = defaultdict(int)
    daily_total = defaultdict(int)
    for sym, df in all_dfs.items():
        for d, grp in df.groupby(df.index.date):
            daily_total[d] += 1
            if grp["close"].iloc[-1] > grp["open"].iloc[0]:
                daily_bull[d] += 1
    dates = sorted(daily_total.keys())
    result = set()
    for i in range(1, len(dates)):
        prev = dates[i-1]
        if daily_total[prev] > 0 and daily_bull[prev]/daily_total[prev] >= threshold:
            result.add(dates[i])
    return result

print("Computing regime filter...")
prev_breadth = compute_prev_breadth_days(stock_dfs)
print(f"  PREV_BREADTH: {len(prev_breadth)} bull days")

# ═══════════════════════════════════════════════════════════
# Build unified timeline (all unique timestamps sorted)
# ═══════════════════════════════════════════════════════════
print("Building unified timeline...")
all_timestamps = set()
for df in stock_dfs.values():
    all_timestamps.update(df.index.tolist())
timeline = sorted(all_timestamps)
print(f"  {len(timeline)} unique candle timestamps across {len(set(t.date() for t in timeline))} days")

# Pre-index: for each stock, map timestamp -> row index for O(1) lookup
stock_ts_map = {}
for sym, df in stock_dfs.items():
    stock_ts_map[sym] = {ts: idx for idx, ts in enumerate(df.index)}

# ═══════════════════════════════════════════════════════════
# PORTFOLIO BACKTESTER
# ═══════════════════════════════════════════════════════════
def portfolio_backtest(max_pos, allowed_dates=None, partial_exit=True):
    """
    TRUE portfolio simulation:
    - Walk through every candle timestamp in chronological order
    - At each timestamp, check all stocks simultaneously
    - Enforce global max_open_positions
    - Track P&L properly
    """
    per_slot = BUYING_POWER * position_size_margin / max_pos

    # Open positions: {symbol: {ep, qty, sl, target, tsl, tsl_on, half_done}}
    positions = {}
    trades = []      # completed trades
    cur_date = None

    for ti, ts in enumerate(timeline):
        d = ts.date()
        if d != cur_date:
            cur_date = d

        # ─── STEP 1: Manage existing positions ───
        closed_syms = []
        for sym in list(positions.keys()):
            if sym not in stock_ts_map or ts not in stock_ts_map[sym]:
                continue
            df = stock_dfs[sym]
            row_idx = stock_ts_map[sym][ts]
            if row_idx < 10:
                continue

            sliced = df.iloc[:row_idx+1]
            cc = sliced.iloc[-1]
            pos = positions[sym]
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]; sl = pos["sl"]
            target = pos["target"]; tsl = pos["tsl"]

            # Partial exit
            if partial_exit and not pos["half_done"] and "rsi" in cc.index and pd.notna(cc["rsi"]):
                if float(cc["rsi"]) > 72 and qty > 1:
                    half = qty // 2
                    trades.append({"sym": sym, "pnl": (close - ep) * half, "exit": "PARTIAL"})
                    pos["qty"] = qty - half
                    pos["half_done"] = True
                    qty = pos["qty"]

            # Check exits
            ex = None
            if low <= tsl:
                ex = min(tsl, float(cc["open"]))
            elif high >= target:
                ex = max(target, float(cc["open"]))
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    # Use next candle open if available
                    if row_idx + 1 < len(df):
                        ex = float(df.iloc[row_idx+1]["open"])

            if ex is not None:
                trades.append({"sym": sym, "pnl": (ex - ep) * qty, "exit": "FULL"})
                closed_syms.append(sym)
            else:
                # TSL update
                sl_pct = abs(ep - sl) / ep if ep > 0 else 0
                thresh = ep + ep * sl_pct * tsl_activation_ratio
                if high >= thresh:
                    pos["tsl_on"] = True
                if pos["tsl_on"]:
                    new_tsl = round_to_tick(high * (1 - trailing_sl_percent))
                    if new_tsl > tsl:
                        pos["tsl"] = new_tsl

        for sym in closed_syms:
            del positions[sym]

        # ─── STEP 2: Look for new entries (only if slots available) ───
        if len(positions) >= max_pos:
            continue
        if ts.hour >= 15:
            continue

        # Regime filter
        if allowed_dates is not None and d not in allowed_dates:
            continue

        # Scan all stocks for buy signals
        for sym in stock_dfs:
            if len(positions) >= max_pos:
                break
            if sym in positions:
                continue  # already holding

            if ts not in stock_ts_map.get(sym, {}):
                continue
            df = stock_dfs[sym]
            row_idx = stock_ts_map[sym][ts]
            if row_idx < 10:
                continue

            sliced = df.iloc[:row_idx+1]
            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced,
                                            pattern_df=sliced, ws_count=0)
            cond = evaluator._evaluate_conditions(buy_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                # Enter on next candle open
                if row_idx + 1 < len(df):
                    entry_price = float(df.iloc[row_idx+1]["open"])
                    qty = int(per_slot // entry_price)
                    if qty < 1:
                        continue
                    sl_price = calculate_stop_loss(entry_price, "BUY")
                    tgt      = calculate_target(entry_price, sl_price)
                    positions[sym] = {
                        "ep": entry_price, "qty": qty,
                        "sl": sl_price, "target": tgt,
                        "tsl": sl_price, "tsl_on": False,
                        "half_done": False,
                    }

    # Force close any remaining positions at last known price
    for sym, pos in positions.items():
        df = stock_dfs[sym]
        last_close = float(df["close"].iloc[-1])
        trades.append({"sym": sym, "pnl": (last_close - pos["ep"]) * pos["qty"], "exit": "EOD"})

    return trades

# ═══════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════
def report(label, trades):
    if not trades:
        print(f"  {label:55s} | NO TRADES")
        return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr   = wins / len(pnls) * 100
    net  = sum(pnls)
    ret  = net / CAPITAL * 100
    n    = len(pnls)
    avg  = net / n
    # Daily breakdown
    by_exit = defaultdict(int)
    for t in trades:
        by_exit[t["exit"]] += 1
    exit_str = " | ".join(f"{k}={v}" for k,v in sorted(by_exit.items()))
    flag = " <<<" if wr >= 50 and ret > 0 else ""
    print(f"  {label:55s} | WR={wr:5.1f}% | Net=Rs.{net:+10,.0f} | Ret={ret:+7.1f}% | "
          f"Trades={n:3d} | Avg=Rs.{avg:+7,.0f}{flag}")
    print(f"  {'':55s} | Exits: {exit_str}")

# ═══════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════
configs = [
    # (label, max_pos, regime, partial)
    ("max=5  | no_regime | partial=ON",   5,  None,          True),
    ("max=5  | no_regime | partial=OFF",  5,  None,          False),
    ("max=5  | PREV_BREADTH | partial=ON",5,  prev_breadth,  True),
    ("max=5  | PREV_BREADTH | partial=OFF",5, prev_breadth,  False),
    ("max=7  | no_regime | partial=ON",   7,  None,          True),
    ("max=7  | no_regime | partial=OFF",  7,  None,          False),
    ("max=7  | PREV_BREADTH | partial=ON",7,  prev_breadth,  True),
    ("max=7  | PREV_BREADTH | partial=OFF",7, prev_breadth,  False),
    ("max=10 | no_regime | partial=ON",   10, None,          True),
    ("max=10 | no_regime | partial=OFF",  10, None,          False),
    ("max=10 | PREV_BREADTH | partial=ON",10, prev_breadth,  True),
    ("max=10 | PREV_BREADTH | partial=OFF",10,prev_breadth,  False),
]

for label, mp, regime, pe in configs:
    print(f"\n{'-'*100}")
    trades = portfolio_backtest(max_pos=mp, allowed_dates=regime, partial_exit=pe)
    report(label, trades)

print(f"\n{'='*100}")
print("DONE. These are REAL numbers — global position limits enforced.")
print(f"{'='*100}")
