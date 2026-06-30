"""
Nifty VWAP Regime Test (Portfolio-Level)
Uses _fetch_yahoo_history for ^NSEI (chart API — confirmed working with 4281 candles)
"""
import sys, os, time as _time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from collections import defaultdict
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from screener.morning_screener import NIFTY_50, _fetch_yahoo_history, _YAHOO_HISTORY_CACHE
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

# Clear cache for ^NSEI to force fresh fetch
keys_to_clear = [k for k in _YAHOO_HISTORY_CACHE if "NSEI" in str(k).upper()]
for k in keys_to_clear:
    del _YAHOO_HISTORY_CACHE[k]
_time.sleep(2)

print("Loading Nifty50 index via chart API (fresh fetch)...")
nifty_df = fetch("^NSEI")
print(f"Nifty candles: {len(nifty_df)}")

if nifty_df.empty:
    print("ERROR: Nifty data still empty. Trying daily as fallback...")
    try:
        nifty_daily = _fetch_yahoo_history("^NSEI", period="60d", interval="1d")
        if nifty_daily is not None and not nifty_daily.empty:
            nifty_daily.columns = [c.lower() for c in nifty_daily.columns]
            print(f"Got {len(nifty_daily)} daily Nifty candles. Using daily open/close as proxy.")
            # Build daily regime: Nifty above open = bull day
            nifty_bull_days = set()
            for ts, row in nifty_daily.iterrows():
                if row["close"] > row["open"]:
                    nifty_bull_days.add(ts.date() if hasattr(ts, 'date') else ts)
            print(f"  Nifty bull days (close > open): {len(nifty_bull_days)}")
        else:
            nifty_bull_days = None
    except Exception as e:
        print(f"  Daily fallback also failed: {e}")
        nifty_bull_days = None
else:
    # Build VWAP lookup from intraday data
    nifty_vwap_map = {}
    for ts, row in nifty_df.iterrows():
        if pd.notna(row.get("vwap")):
            nifty_vwap_map[ts] = float(row["close"]) >= float(row["vwap"])
    print(f"  Nifty VWAP data points: {len(nifty_vwap_map)}")
    nifty_bull_days = None  # use intraday instead

# Build timeline
all_timestamps = set()
for df in stock_dfs.values():
    all_timestamps.update(df.index.tolist())
timeline = sorted(all_timestamps)
stock_ts_map = {}
for sym, df in stock_dfs.items():
    stock_ts_map[sym] = {ts: idx for idx, ts in enumerate(df.index)}

def nifty_is_bull_at(timestamp):
    if nifty_df.empty:
        return True
    if timestamp in nifty_vwap_map:
        return nifty_vwap_map[timestamp]
    for delta_min in range(0, 11):
        for sign in [1, -1]:
            check = timestamp + pd.Timedelta(minutes=delta_min * sign)
            if check in nifty_vwap_map:
                return nifty_vwap_map[check]
    return True

def portfolio_backtest(max_pos, regime_mode="none", partial_exit=True):
    per_slot = BUYING_POWER * position_size_margin / max_pos
    positions = {}; trades = []; cur_date = None

    for ti, ts in enumerate(timeline):
        d = ts.date()
        if d != cur_date: cur_date = d

        # Manage positions
        closed = []
        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; row_idx = stock_ts_map[sym][ts]
            if row_idx < 10: continue
            sliced = df.iloc[:row_idx+1]; cc = sliced.iloc[-1]
            pos = positions[sym]
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]; sl = pos["sl"]; tsl = pos["tsl"]

            if partial_exit and not pos["half_done"] and "rsi" in cc.index and pd.notna(cc["rsi"]):
                if float(cc["rsi"]) > 72 and qty > 1:
                    half = qty // 2
                    trades.append({"pnl": (close - ep) * half, "exit": "PARTIAL"})
                    pos["qty"] = qty - half; pos["half_done"] = True; qty = pos["qty"]

            ex = None
            if low <= tsl: ex = min(tsl, float(cc["open"]))
            elif high >= pos["target"]: ex = max(pos["target"], float(cc["open"]))
            elif ts.hour == 15 and ts.minute >= 15: ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if row_idx + 1 < len(df): ex = float(df.iloc[row_idx+1]["open"])

            if ex is not None:
                trades.append({"pnl": (ex - ep) * qty, "exit": "FULL"}); closed.append(sym)
            else:
                sl_pct = abs(ep - sl) / ep if ep > 0 else 0
                if high >= ep + ep * sl_pct * tsl_activation_ratio: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    new_tsl = round_to_tick(high * (1 - trailing_sl_percent))
                    if new_tsl > tsl: pos["tsl"] = new_tsl

        for sym in closed: del positions[sym]

        # New entries
        if len(positions) >= max_pos: continue
        if ts.hour >= 15: continue

        # Regime check
        if regime_mode == "nifty_vwap" and not nifty_df.empty:
            if not nifty_is_bull_at(ts): continue
        elif regime_mode == "nifty_daily" and nifty_bull_days is not None:
            if d not in nifty_bull_days: continue

        for sym in stock_dfs:
            if len(positions) >= max_pos: break
            if sym in positions: continue
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; row_idx = stock_ts_map[sym][ts]
            if row_idx < 10: continue
            sliced = df.iloc[:row_idx+1]
            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced, pattern_df=sliced, ws_count=0)
            cond = evaluator._evaluate_conditions(buy_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                if row_idx + 1 < len(df):
                    ep = float(df.iloc[row_idx+1]["open"])
                    qty = int(per_slot // ep)
                    if qty < 1: continue
                    sl_p = calculate_stop_loss(ep, "BUY")
                    positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p,
                                      "target": calculate_target(ep, sl_p),
                                      "tsl": sl_p, "tsl_on": False, "half_done": False}

    for sym, pos in positions.items():
        trades.append({"pnl": (float(stock_dfs[sym]["close"].iloc[-1]) - pos["ep"]) * pos["qty"], "exit": "EOD"})
    return trades

def report(label, trades):
    if not trades:
        print(f"  {label:55s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    flag = " <<<" if wr >= 50 and ret > 0 else ""
    print(f"  {label:55s} | WR={wr:5.1f}% | Net=Rs.{net:+10,.0f} | Ret={ret:+7.1f}% | Trades={len(pnls):4d}{flag}")

print("\n" + "=" * 100)
print("NIFTY REGIME TESTS (Portfolio-Level, max_pos=5)")
print("=" * 100)

configs = [
    ("NO REGIME | partial=ON",           "none",         True),
    ("NO REGIME | partial=OFF",          "none",         False),
]

# Add Nifty VWAP tests if data available
if not nifty_df.empty:
    configs.extend([
        ("NIFTY_INTRADAY_VWAP | partial=ON",  "nifty_vwap",  True),
        ("NIFTY_INTRADAY_VWAP | partial=OFF", "nifty_vwap",  False),
    ])

if nifty_bull_days is not None:
    configs.extend([
        ("NIFTY_DAILY (close>open) | partial=ON",  "nifty_daily", True),
        ("NIFTY_DAILY (close>open) | partial=OFF", "nifty_daily", False),
    ])

for label, regime, pe in configs:
    trades = portfolio_backtest(max_pos=5, regime_mode=regime, partial_exit=pe)
    report(label, trades)

# Also test max=7 and max=10
for mp in [7, 10]:
    print(f"\n--- max_pos={mp} ---")
    for label, regime, pe in configs:
        trades = portfolio_backtest(max_pos=mp, regime_mode=regime, partial_exit=pe)
        report(f"pos={mp} | {label}", trades)

print("\nDONE.")
