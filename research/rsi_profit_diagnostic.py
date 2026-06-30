"""
DIAGNOSTIC: Profit% Distribution at Each Exit Type
====================================================
Key Question: Jab RSI>=72 exit hota hai, stock kitne % upar tha?
- Agar RSI>=72 exits +0.1%, +0.2% pe ho rahi hain → bahut thin wins!
- Ye explain karta hai kyun 0.5% sirf 40 trades mein RSI se pehle aaya

Then test:
  Idea A: RSI>=72 + Minimum Profit% guard
           "RSI>=72 fire karo ONLY if profit >= X%, warna hold karo"
  Idea B: RSI>=72 → set a floor target (entry + Y%)
           "RSI>=72 aate hi TSL nahi, ek fixed floor target set karo"
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from research.build_cache import load_cache
from core.strategy import (CONDITION_REGISTRY, StrategySetEvaluator,
                            StrategyEvaluationContext)
from core.strategy_sets import load_strategy_sets
from core.order_executor import round_to_tick
from core.trading_settings import get as cfg

CAPITAL = 100000.0; MARGIN = 5.0; BUYING_POWER = CAPITAL * MARGIN
MP = 3; SL_PCT = 0.010

config       = load_strategy_sets()
buy_set_def  = next(s for s in config.buy_sets  if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
sell_set_def = next(s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS")
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)
TSL_ACTIVATION = float(cfg("risk", "tsl_activation_ratio", 1.2))
TSL_PCT        = float(cfg("risk", "trailing_sl_percent",  0.002))

stock_dfs = load_cache()
print(f"{len(stock_dfs)} stocks loaded from cache.")

stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        by_day[d] = {"prev_close": prev_close,
                     "gap_pct": (float(grp["open"].iloc[0]) - prev_close) / prev_close if prev_close else 0.0}
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

all_dates_set = set(d for days in stock_day_data.values() for d in days.keys())
strong40_days = set()
for d in all_dates_set:
    strong = sum(1 for sym, days in stock_day_data.items()
                 if d in days and days[d]["prev_close"] and days[d]["gap_pct"] >= 0.005)
    total  = sum(1 for sym, days in stock_day_data.items()
                 if d in days and days[d]["prev_close"])
    if total > 0 and strong / total >= 0.40: strong40_days.add(d)
print(f"STRONG_GAP_40 days: {len(strong40_days)}")

all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

# ── DIAGNOSTIC BACKTEST ───────────────────────────────────────
def backtest_diagnostic(max_pos, allowed_dates,
                        min_profit_for_rsi_exit=None,  # Idea A
                        floor_target_pct=None):         # Idea B
    """
    min_profit_for_rsi_exit: RSI>=72 fire karo ONLY if profit >= this %
    floor_target_pct:        RSI>=72 aate hi, set target at max(close, ep*(1+floor_target_pct))
    """
    per_slot = BUYING_POWER / max_pos
    positions = {}
    trades = []
    # Diagnostic: exact profit% at each exit
    exit_profits = {"RSI": [], "SL": [], "NRM": [], "EOD": [], "FLOOR": []}
    rsi_blocked = []  # cases where RSI fired but profit was too low → what happened next

    for ts in timeline:
        d = ts.date()
        closed = []

        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 4: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos = positions[sym]
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]
            if qty <= 0: closed.append(sym); continue

            rsi0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0
            profit_pct = (close - ep) / ep

            # ── FLOOR TARGET (Idea B): set floor when RSI>=72 first fires ──
            if floor_target_pct and not pos.get("floor_set") and rsi0 >= 72:
                pos["floor_set"] = True
                pos["floor_price"] = round_to_tick(ep * (1 + floor_target_pct))
                pos["floor_at_profit"] = profit_pct  # record what profit was at RSI>=72

            # ── FLOOR TARGET EXIT ──────────────────────────────────────────
            if floor_target_pct and pos.get("floor_set"):
                if high >= pos["floor_price"]:
                    ex = max(pos["floor_price"], float(cc["open"]))
                    exit_profits["FLOOR"].append((ex - ep) / ep)
                    trades.append({"pnl": (ex - ep) * qty, "reason": "FLOOR"})
                    closed.append(sym); continue
                # EOD force exit
                elif ts.hour == 15 and ts.minute >= 15:
                    exit_profits["EOD"].append(profit_pct)
                    trades.append({"pnl": (close - ep) * qty, "reason": "EOD"})
                    closed.append(sym); continue
                # SL still active
                if low <= pos["tsl"]:
                    ex_p = min(pos["tsl"], float(cc["open"]))
                    exit_profits["SL"].append((ex_p - ep) / ep)
                    trades.append({"pnl": (ex_p - ep) * qty, "reason": "SL"})
                    closed.append(sym); continue
                continue  # floor mode: only exit at floor, SL, or EOD

            # ── RSI EXIT (with optional min_profit guard) ──────────────────
            if rsi0 >= 72:
                if min_profit_for_rsi_exit and profit_pct < min_profit_for_rsi_exit:
                    # RSI fired but profit too low — block it, let it run
                    if not pos.get("rsi_blocked_logged"):
                        rsi_blocked.append(profit_pct)
                        pos["rsi_blocked_logged"] = True
                else:
                    exit_profits["RSI"].append(profit_pct)
                    trades.append({"pnl": (close - ep) * qty, "reason": "RSI"})
                    closed.append(sym); continue

            # ── Normal exits ───────────────────────────────────────────────
            ex = None
            if low <= pos["tsl"]:
                ex = min(pos["tsl"], float(cc["open"]))
                exit_profits["SL"].append((ex - ep) / ep)
            elif high >= pos["tgt"]:
                ex = max(pos["tgt"], float(cc["open"]))
                exit_profits["NRM"].append((high - ep) / ep)
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close
                exit_profits["EOD"].append(profit_pct)
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df):
                        ex = float(df.iloc[idx+1]["open"])
                        exit_profits["NRM"].append((ex - ep) / ep)
            if ex:
                trades.append({"pnl": (ex - ep) * qty, "reason": "NRM" if ex != close else "EOD"})
                closed.append(sym)
            else:
                if high >= ep + abs(ep - pos["sl"]) * TSL_ACTIVATION: pos["tsl_on"] = True
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
            if idx < 4: continue
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced,
                                            pattern_df=sliced, ws_count=0)
            cond = evaluator._evaluate_conditions(buy_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"])
                    qty = int(per_slot // ep)
                    if qty < 1: continue
                    sl_p = round_to_tick(ep * (1 - SL_PCT))
                    positions[sym] = {
                        "ep": ep, "qty": qty, "sl": sl_p,
                        "tgt": round_to_tick(ep + abs(ep - sl_p) * 10.0),
                        "tsl": sl_p, "tsl_on": False,
                        "floor_set": False,
                    }

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        pct = (lc - pos["ep"]) / pos["ep"]
        exit_profits["EOD"].append(pct)
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "reason": "EOD"})

    return trades, exit_profits, rsi_blocked

# ── STEP 1: DIAGNOSTIC — BASELINE ────────────────────────────
print(f"\n{'='*110}")
print("STEP 1: DIAGNOSTIC — Profit% Distribution at each exit type (Baseline)")
print(f"{'='*110}")
trades, exit_profits, _ = backtest_diagnostic(MP, strong40_days)

pnls = [t["pnl"] for t in trades]
wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
ret = sum(pnls) / CAPITAL * 100
print(f"Baseline: WR={wr:.1f}% | Ret={ret:+.1f}% | T={len(trades)}")

print(f"\nPROFIT% DISTRIBUTION AT EACH EXIT TYPE:")
print(f"{'Exit':<8} {'Count':>5} {'Mean%':>7} {'Median%':>8} {'Min%':>7} {'Max%':>7} | Distribution")
for etype, profits in exit_profits.items():
    if not profits: continue
    p = np.array(profits) * 100
    # buckets
    neg     = np.sum(p < 0)
    z_pt2   = np.sum((p >= 0)   & (p < 0.2))
    pt2_pt5 = np.sum((p >= 0.2) & (p < 0.5))
    pt5_1   = np.sum((p >= 0.5) & (p < 1.0))
    one_2   = np.sum((p >= 1.0) & (p < 2.0))
    two_pl  = np.sum(p >= 2.0)
    bar = f"<0%:{neg} | 0-0.2%:{z_pt2} | 0.2-0.5%:{pt2_pt5} | 0.5-1%:{pt5_1} | 1-2%:{one_2} | >2%:{two_pl}"
    print(f"{etype:<8} {len(p):>5} {np.mean(p):>7.2f}% {np.median(p):>8.2f}% {np.min(p):>7.2f}% {np.max(p):>7.2f}% | {bar}")

# ── KEY INSIGHT: How many RSI exits are at < 0.5%? ───────────
rsi_p = np.array(exit_profits["RSI"]) * 100
thin  = np.sum(rsi_p < 0.5)
ok    = np.sum((rsi_p >= 0.5) & (rsi_p < 1.0))
good  = np.sum(rsi_p >= 1.0)
print(f"\n*** RSI EXIT BREAKDOWN (this is the KEY) ***")
print(f"  RSI exits < 0.5% profit  : {thin:3d}  ({thin/len(rsi_p)*100:.1f}%)  <- These are BLOCKING the 0.5% floor!")
print(f"  RSI exits 0.5% - 1.0%    : {ok:3d}  ({ok/len(rsi_p)*100:.1f}%)")
print(f"  RSI exits > 1.0%          : {good:3d}  ({good/len(rsi_p)*100:.1f}%)")
print(f"  Avg RSI exit profit       : {np.mean(rsi_p):.3f}%")
print(f"  Median RSI exit profit    : {np.median(rsi_p):.3f}%")

results = []
def stats(label, trades):
    if not trades: print(f"  {label:70s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    flag = " ***" if ret >= 35 else (" <<<" if ret >= 34 else "")
    print(f"  {label:70s} | WR={wr:5.1f}% | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")
    results.append({"label":label,"wr":wr,"net":net,"ret":ret,"t":len(pnls)})

# ── STEP 2: IDEA A — RSI + MINIMUM PROFIT GUARD ──────────────
print(f"\n{'='*110}")
print("STEP 2: IDEA A — RSI>=72 fires ONLY if profit >= X% (else hold)")
print("RSI se chote profits block karo, stock ko thoda aur jaane do")
print(f"{'='*110}")
stats("BASELINE | RSI>=72 any profit",
      backtest_diagnostic(MP, strong40_days)[0])
for min_p in [0.001, 0.002, 0.003, 0.004, 0.005, 0.007, 0.010, 0.015, 0.020]:
    t, ep_d, blocked = backtest_diagnostic(MP, strong40_days, min_profit_for_rsi_exit=min_p)
    label = f"RSI>=72 only if profit>={min_p*100:.1f}% | {len(blocked)} RSI exits blocked"
    stats(label, t)

# ── STEP 3: IDEA B — RSI>=72 → FLOOR TARGET ──────────────────
print(f"\n{'='*110}")
print("STEP 3: IDEA B — RSI>=72 aate hi FLOOR TARGET set karo (entry + X%)")
print("RSI fires -> don't exit, set a floor profit target instead")
print(f"{'='*110}")
for fp in [0.003, 0.005, 0.007, 0.010, 0.012, 0.015, 0.020, 0.025, 0.030]:
    t, _, _ = backtest_diagnostic(MP, strong40_days, floor_target_pct=fp)
    stats(f"RSI>=72 -> FLOOR target={fp*100:.1f}% from entry", t)

# ── LEADERBOARD ───────────────────────────────────────────────
print(f"\n{'='*110}")
print(f"LEADERBOARD — TOP 15 ({len(results)} configs)")
print(f"{'='*110}")
top = sorted(results, key=lambda x: x["ret"], reverse=True)[:15]
for i, rx in enumerate(top, 1):
    crown = " <<<<< KING" if i == 1 else (" ***" if i <= 3 else "")
    print(f"  #{i:2d} | {rx['label']:70s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{crown}")

print(f"\nDONE. {len(results)} configs.")
