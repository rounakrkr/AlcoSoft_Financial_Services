"""
RSI THRESHOLD DEEP SWEEP
=========================
Best config so far: STRONG_GAP_40 | max_pos=3 | full_exit RSI>72 = +31.9%

Now test: every RSI threshold from 65 to 82
And combos: two-stage exits (first at RSI_A, second at RSI_B)
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

# STRONG_GAP_40 days
stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        by_day[d] = {"day_open": float(grp["open"].iloc[0]), "prev_close": prev_close}
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

all_dates = set(d for days in stock_day_data.values() for d in days.keys())
strong40_days = set()
for d in all_dates:
    strong = sum(1 for sym, days in stock_day_data.items()
                 if d in days and days[d]["prev_close"]
                 and (days[d]["day_open"] - days[d]["prev_close"]) / days[d]["prev_close"] >= 0.005)
    total = sum(1 for sym, days in stock_day_data.items()
                if d in days and days[d]["prev_close"])
    if total > 0 and strong / total >= 0.40:
        strong40_days.add(d)
print(f"STRONG_GAP_40 days: {len(strong40_days)}")

all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

def portfolio_backtest(max_pos, allowed_dates, rsi_full=None, rsi_half=None,
                       rsi_second_full=None):
    """
    rsi_full:        exit 100% of position when RSI >= this value
    rsi_half:        exit 50% of position when RSI >= this value (first stage)
    rsi_second_full: exit remaining 50% when RSI >= this value (second stage)

    Combos:
      Single full exit:     rsi_full=72
      Single half exit:     rsi_half=72 (rest held till SL/target/signal)
      Two-stage:            rsi_half=70, rsi_second_full=75
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
            rsi = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 0

            # Stage 1: half exit
            if rsi_half and not pos["half_done"] and rsi >= rsi_half and qty > 1:
                h = qty // 2
                trades.append({"pnl": (close - ep) * h})
                pos["qty"] -= h; pos["half_done"] = True; qty = pos["qty"]

            # Stage 2: second-stage full exit (of remaining)
            if rsi_second_full and pos.get("half_done") and not pos.get("second_done") and rsi >= rsi_second_full:
                trades.append({"pnl": (close - ep) * qty})
                closed.append(sym); pos["second_done"] = True; continue

            # Single full exit at RSI
            if rsi_full and not pos.get("rsi_exited") and rsi >= rsi_full:
                trades.append({"pnl": (close - ep) * qty})
                closed.append(sym); continue

            # Normal exits
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
                trades.append({"pnl": (ex - ep) * qty}); closed.append(sym)
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
                                      "half_done": False, "second_done": False,
                                      "rsi_exited": False}
    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"]})
    return trades

results = []
def r(label, trades):
    if not trades: print(f"  {label:65s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    flag = " ***" if ret >= 30 else (" <<<" if ret >= 20 else "")
    print(f"  {label:65s} | WR={wr:5.1f}% | Net={net:+10,.0f} | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")
    results.append({"label": label, "wr": wr, "net": net, "ret": ret, "t": len(pnls)})

MP = 3
print(f"\n{'='*115}")
print(f"RSI THRESHOLD SWEEP | STRONG_GAP_40 | max_pos={MP}")
print(f"{'='*115}")

print("\n--- FULL EXIT at various RSI levels ---")
for rsi in range(65, 83):
    r(f"FULL_EXIT RSI>={rsi}",
      portfolio_backtest(MP, strong40_days, rsi_full=rsi))

print("\n--- HALF EXIT (50%) at various RSI levels ---")
for rsi in range(65, 83):
    r(f"HALF_EXIT RSI>={rsi}",
      portfolio_backtest(MP, strong40_days, rsi_half=rsi))

print("\n--- TWO-STAGE: half at RSI_A, full at RSI_B ---")
for rsi_a in [68, 70, 72, 74]:
    for rsi_b in [74, 76, 78, 80]:
        if rsi_b <= rsi_a: continue
        r(f"HALF@{rsi_a} then FULL@{rsi_b}",
          portfolio_backtest(MP, strong40_days, rsi_half=rsi_a, rsi_second_full=rsi_b))

print(f"\n{'='*115}")
print("TOP 15 by RETURN")
print(f"{'='*115}")
top = sorted(results, key=lambda x: x["ret"], reverse=True)[:15]
for i, rx in enumerate(top, 1):
    flag = " *** BEST ***" if i == 1 else ""
    print(f"  #{i:2d} | {rx['label']:65s} | WR={rx['wr']:5.1f}% | Net={rx['net']:+10,.0f} | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{flag}")

print(f"\n{'='*115}")
print("TOP 10 by WIN RATE (with positive return)")
print(f"{'='*115}")
top_wr = sorted([x for x in results if x["ret"] > 0], key=lambda x: x["wr"], reverse=True)[:10]
for i, rx in enumerate(top_wr, 1):
    print(f"  #{i:2d} | {rx['label']:65s} | WR={rx['wr']:5.1f}% | Net={rx['net']:+10,.0f} | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}")

print(f"\nTotal configs: {len(results)} | DONE.")
