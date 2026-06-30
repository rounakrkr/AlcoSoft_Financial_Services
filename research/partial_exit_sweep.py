"""
PARTIAL EXIT AT PROFIT % — SCALE OUT STRATEGY
==============================================
Logic:
  - Buy X shares
  - When profit >= threshold% → sell FRACTION of shares (e.g. 50%)
  - Remaining shares → hold until RSI>=72, SL, or sell signal

Tests:
  Partial fraction: 25%, 33%, 50%, 67%, 75%
  Profit threshold: 0.5%, 0.75%, 1.0%, 1.25%, 1.5%, 1.75%, 2.0%
  = 35 combinations

All on STRONG_GAP_40 + max_pos=3 + SL=1.0% + RSI(0)>=72 for remainder
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
from core.order_executor import round_to_tick
from core.trading_settings import get as cfg

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
MP = 3

config       = load_strategy_sets()
buy_set_def  = next(s for s in config.buy_sets  if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
sell_set_def = next(s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS")
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)
TSL_ACTIVATION = float(cfg("risk", "tsl_activation_ratio", 1.2))
TSL_PCT        = float(cfg("risk", "trailing_sl_percent",  0.002))
SL_PCT         = 0.010  # fixed 1% SL

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
    except Exception: return pd.DataFrame()

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

def backtest_partial(max_pos, allowed_dates,
                     partial_pct,       # profit% threshold to trigger partial exit
                     partial_fraction,  # fraction of remaining qty to sell (e.g. 0.5 = 50%)
                     rsi_hi=72):
    """
    Scale-out strategy:
    1. Buy full qty
    2. At +partial_pct% profit -> sell partial_fraction of qty
    3. Remaining qty -> hold until RSI>=72 / SL / sell signal / EOD
    """
    per_slot = BUYING_POWER / max_pos
    positions = {}
    # Each position: ep, total_qty, remaining_qty, sl, tgt, tsl, tsl_on, partial_done
    trades = []  # list of individual exit events

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
            ep = pos["ep"]; qty = pos["remaining_qty"]
            if qty <= 0:
                closed.append(sym); continue

            rsi = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0

            # PARTIAL EXIT: if profit >= threshold and not yet done
            if not pos["partial_done"]:
                current_profit_pct = (close - ep) / ep
                if current_profit_pct >= partial_pct:
                    sell_qty = max(1, int(qty * partial_fraction))
                    if sell_qty >= qty:
                        sell_qty = qty - 1  # keep at least 1 share
                    if sell_qty > 0:
                        trades.append({
                            "pnl": (close - ep) * sell_qty,
                            "reason": "PARTIAL"
                        })
                        pos["remaining_qty"] -= sell_qty
                        pos["partial_done"] = True
                        qty = pos["remaining_qty"]
                        if qty <= 0:
                            closed.append(sym); continue

            # RSI HIGH EXIT (remaining qty)
            if rsi >= rsi_hi:
                trades.append({"pnl": (close - ep) * qty, "reason": "RSI"})
                closed.append(sym); continue

            # Normal exits
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
                trades.append({"pnl": (ex - ep) * qty, "reason": "NRM"})
                closed.append(sym)
            else:
                if high >= ep + abs(ep - pos["sl"]) * TSL_ACTIVATION:
                    pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high * (1 - TSL_PCT))
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
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"])
                    total_qty = int(per_slot // ep)
                    if total_qty < 2: continue  # need at least 2 shares for partial
                    sl_p = round_to_tick(ep * (1 - SL_PCT))
                    tgt_p = round_to_tick(ep + abs(ep - sl_p) * 10.0)
                    positions[sym] = {
                        "ep": ep,
                        "total_qty": total_qty,
                        "remaining_qty": total_qty,
                        "sl": sl_p, "tgt": tgt_p,
                        "tsl": sl_p, "tsl_on": False,
                        "partial_done": False,
                    }

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        qty = pos["remaining_qty"]
        if qty > 0:
            trades.append({"pnl": (lc - pos["ep"]) * qty, "reason": "EOD"})
    return trades

results = []
def stats(label, trades):
    if not trades: print(f"  {label:75s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    n_partial = sum(1 for t in trades if t.get("reason") == "PARTIAL")
    flag = " ***" if ret >= 33 else (" <<<" if ret >= 28 else "")
    print(f"  {label:75s} | WR={wr:5.1f}% | Ret={ret:+6.1f}% | T={len(pnls):4d} | Partial={n_partial:3d}{flag}")
    results.append({"label": label, "wr": wr, "net": net, "ret": ret,
                    "t": len(pnls), "n_partial": n_partial})

print(f"\n{'='*120}")
print("BASELINE: Full exit only (no partial)")
print(f"{'='*120}")
# Reuse backtest_partial with fraction=0 effectively (high threshold never hit)
from research.mega_optimize import backtest as backtest_full
t0 = backtest_full(MP, strong40_days, sl_pct=SL_PCT, profit_pct=None)
stats("BASELINE | Full exit RSI>=72 only | SL=1.0%", t0)

print(f"\n{'='*120}")
print("PARTIAL EXIT SWEEP: fraction x profit_threshold")
print("Reading: sell FRACTION at PROFIT%, rest runs to RSI>=72")
print(f"{'='*120}")

fractions   = [0.25, 0.33, 0.50, 0.67, 0.75]
thresholds  = [0.005, 0.0075, 0.010, 0.0125, 0.015, 0.0175, 0.020]
frac_labels = {0.25: "25%", 0.33: "33%", 0.50: "50%", 0.67: "67%", 0.75: "75%"}

for frac in fractions:
    print(f"\n  -- Sell {frac_labels[frac]} of position at profit threshold --")
    for thr in thresholds:
        t = backtest_partial(MP, strong40_days, partial_pct=thr, partial_fraction=frac)
        stats(f"PARTIAL sell {frac_labels[frac]} @ +{thr*100:.2f}% | rest -> RSI>=72", t)

print(f"\n{'='*120}")
print(f"LEADERBOARD: TOP 15 by RETURN ({len(results)} configs)")
print(f"{'='*120}")
top = sorted(results, key=lambda x: x["ret"], reverse=True)[:15]
for i, rx in enumerate(top, 1):
    king = " <-- KING" if i == 1 else ""
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{king}")

print(f"\n{'='*120}")
print("SWEET SPOT: WR>=57% AND Ret>=28%")
print(f"{'='*120}")
sweet = sorted([x for x in results if x["wr"]>=57 and x["ret"]>=28],
               key=lambda x: x["ret"], reverse=True)
for i, rx in enumerate(sweet, 1):
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}")
if not sweet:
    print("  (None — relaxing to WR>=55% AND Ret>=25%)")
    sweet2 = sorted([x for x in results if x["wr"]>=55 and x["ret"]>=25],
                    key=lambda x: x["ret"], reverse=True)[:8]
    for i, rx in enumerate(sweet2, 1):
        print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}")

print(f"\nDONE. {len(results)} configs tested.")
