"""
MULTI-LAYER EXIT SYSTEM
========================
Trade ke andar multiple exit levels:

Layer 1 @ +0.5% (Kinetic):
  Kinetic FAIL → sell L1_frac (75%)
  Kinetic PASS → hold all

Layer 2 @ RSI>=72:
  Currently: sell ALL
  New: sell L2_frac (50%, 75%, 100%)
  Remaining shares continue to Layer 3

Layer 3 (remaining after RSI>=72 partial):
  Mode A: TSL trail (0.5%, 0.8%, 1.0%, 1.5%, 2.0%)
  Mode B: Hard profit% from entry (2%, 3%, 4%, 5%)
  Mode C: EOD only

Best known so far: K_RSI_RISING_1, L1=75%, L2=100% → +34.3%, WR=60%
Goal: Beat +34.3% by letting a portion run after RSI>=72
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
NORMAL_TSL_PCT = float(cfg("risk", "trailing_sl_percent", 0.002))

stock_dfs = load_cache()
print(f"{len(stock_dfs)} stocks from cache.")

stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        by_day[d] = {"prev_close": prev_close,
                     "gap_pct": (float(grp["open"].iloc[0]) - prev_close) / prev_close
                                 if prev_close else 0.0}
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

# ── Kinetic: RSI(1) rising (best from prev test) ──────────────
def kinetic_rsi1_rising(sliced):
    if len(sliced) < 3: return True
    rsi1 = sliced["rsi"].iloc[-2] if pd.notna(sliced["rsi"].iloc[-2]) else 50.0
    rsi2 = sliced["rsi"].iloc[-3] if pd.notna(sliced["rsi"].iloc[-3]) else 50.0
    return float(rsi1) > float(rsi2)

def backtest_multilayer(max_pos, allowed_dates,
                        # Layer 1
                        l1_kinetic=True,     # use kinetic check at +0.5%?
                        l1_frac=0.75,        # sell this much if kinetic FAILS
                        # Layer 2
                        l2_frac=1.0,         # sell this much at RSI>=72 (1.0=full exit)
                        # Layer 3 (only if l2_frac < 1.0)
                        l3_mode=None,        # None, "TSL", "PROFIT_PCT"
                        l3_tsl_pct=0.008,    # TSL % for Layer 3
                        l3_profit_pct=0.03,  # hard profit% from entry for Layer 3
                        ):
    per_slot = BUYING_POWER / max_pos
    positions = {}; trades = []

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

            # ── LAYER 1: Kinetic @ +0.5% ───────────────────────
            if l1_kinetic and not pos["l1_done"]:
                if (close - ep) / ep >= 0.005:
                    pos["l1_done"] = True
                    if not kinetic_rsi1_rising(sliced):
                        # Kinetic FAIL → sell l1_frac
                        sell_qty = max(1, int(qty * l1_frac))
                        if sell_qty >= qty: sell_qty = max(0, qty - 1)
                        if sell_qty > 0:
                            trades.append({"pnl": (close - ep) * sell_qty, "r": "L1_K"})
                            pos["qty"] -= sell_qty
                            qty = pos["qty"]
                            if qty <= 0: closed.append(sym); continue

            # ── LAYER 2: RSI >= 72 ──────────────────────────────
            if not pos["l2_done"] and rsi0 >= 72:
                pos["l2_done"] = True
                sell_qty = max(1, int(qty * l2_frac))
                if sell_qty > qty: sell_qty = qty
                if sell_qty > 0:
                    trades.append({"pnl": (close - ep) * sell_qty, "r": "L2_RSI"})
                    pos["qty"] -= sell_qty
                    qty = pos["qty"]
                # If fully exited, close
                if qty <= 0:
                    closed.append(sym); continue
                # Remaining shares enter Layer 3
                if l3_mode == "TSL":
                    pos["l3_tsl"] = round_to_tick(close * (1 - l3_tsl_pct))
                    pos["l3_active"] = True
                elif l3_mode == "PROFIT_PCT":
                    pos["l3_target_price"] = round_to_tick(ep * (1 + l3_profit_pct))
                    pos["l3_active"] = True

            # ── LAYER 3: Remaining shares ───────────────────────
            if pos.get("l3_active") and qty > 0:
                exited = False
                if l3_mode == "TSL":
                    # Trail TSL up
                    new_tsl = round_to_tick(high * (1 - l3_tsl_pct))
                    if new_tsl > pos["l3_tsl"]: pos["l3_tsl"] = new_tsl
                    if low <= pos["l3_tsl"]:
                        ex = min(pos["l3_tsl"], float(cc["open"]))
                        trades.append({"pnl": (ex - ep) * qty, "r": "L3_TSL"})
                        closed.append(sym); exited = True
                elif l3_mode == "PROFIT_PCT":
                    if high >= pos["l3_target_price"]:
                        ex = max(pos["l3_target_price"], float(cc["open"]))
                        trades.append({"pnl": (ex - ep) * qty, "r": "L3_PROF"})
                        closed.append(sym); exited = True
                if exited: continue

            # ── Normal exits (SL, sell signal, EOD) ────────────
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
                trades.append({"pnl": (ex - ep) * qty, "r": "NRM"})
                closed.append(sym)
            else:
                if high >= ep + abs(ep - pos["sl"]) * TSL_ACTIVATION:
                    pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high * (1 - NORMAL_TSL_PCT))
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
                    total_qty = int(per_slot // ep)
                    if total_qty < 2: continue
                    sl_p = round_to_tick(ep * (1 - SL_PCT))
                    positions[sym] = {
                        "ep": ep, "qty": total_qty,
                        "sl": sl_p, "tgt": round_to_tick(ep + abs(ep-sl_p) * 10.0),
                        "tsl": sl_p, "tsl_on": False,
                        "l1_done": False, "l2_done": False,
                        "l3_active": False, "l3_tsl": 0.0,
                    }
    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        if pos["qty"] > 0:
            trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "r": "EOD"})
    return trades

results = []
def s(label, trades):
    if not trades: print(f"  {label:80s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    flag = " ***" if ret >= 35 else (" <<<" if ret >= 34 else "")
    print(f"  {label:80s} | WR={wr:5.1f}% | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")
    results.append({"label":label,"wr":wr,"net":net,"ret":ret,"t":len(pnls)})

# ────────────────────────────────────────────────────────────────
print(f"\n{'='*130}")
print("BASELINES")
print(f"{'='*130}")
s("BASELINE | L1=no kinetic | L2=100% @ RSI72",
  backtest_multilayer(MP, strong40_days, l1_kinetic=False, l2_frac=1.0))
s("BEST SO FAR | L1=K_RSI1 75% | L2=100% @ RSI72",
  backtest_multilayer(MP, strong40_days, l1_kinetic=True, l1_frac=0.75, l2_frac=1.0))

# ── SECTION A: L2 PARTIAL @ RSI72 only (no L1, no L3) ──────────
print(f"\n{'='*130}")
print("SECTION A: L2 PARTIAL @ RSI>=72 (no kinetic, no L3)")
print("How much to sell at RSI>=72? Let remainder go to EOD")
print(f"{'='*130}")
for l2 in [0.25, 0.33, 0.50, 0.67, 0.75]:
    t = backtest_multilayer(MP, strong40_days, l1_kinetic=False,
                            l2_frac=l2, l3_mode=None)
    s(f"L2={int(l2*100)}% @ RSI72 | rest -> EOD", t)

# ── SECTION B: L2 PARTIAL + L3 TSL ────────────────────────────
print(f"\n{'='*130}")
print("SECTION B: L2 PARTIAL @ RSI72 + L3 TSL (let remaining run with trailing stop)")
print(f"{'='*130}")
for l2 in [0.50, 0.75]:
    for tsl in [0.005, 0.008, 0.010, 0.015, 0.020, 0.025]:
        t = backtest_multilayer(MP, strong40_days, l1_kinetic=False,
                                l2_frac=l2, l3_mode="TSL", l3_tsl_pct=tsl)
        s(f"L2={int(l2*100)}% @ RSI72 | L3=TSL {tsl*100:.1f}%", t)

# ── SECTION C: L2 PARTIAL + L3 HARD PROFIT% ───────────────────
print(f"\n{'='*130}")
print("SECTION C: L2 PARTIAL @ RSI72 + L3 HARD PROFIT% from entry")
print(f"{'='*130}")
for l2 in [0.50, 0.75]:
    for pp in [0.020, 0.025, 0.030, 0.035, 0.040, 0.050]:
        t = backtest_multilayer(MP, strong40_days, l1_kinetic=False,
                                l2_frac=l2, l3_mode="PROFIT_PCT", l3_profit_pct=pp)
        s(f"L2={int(l2*100)}% @ RSI72 | L3=Profit>={pp*100:.1f}% from entry", t)

# ── SECTION D: L1 KINETIC + L2 PARTIAL + L3 TSL ────────────────
print(f"\n{'='*130}")
print("SECTION D: FULL SYSTEM — L1 Kinetic + L2 Partial + L3 TSL")
print(f"{'='*130}")
for l2 in [0.50, 0.75]:
    for tsl in [0.008, 0.010, 0.015, 0.020]:
        t = backtest_multilayer(MP, strong40_days,
                                l1_kinetic=True, l1_frac=0.75,
                                l2_frac=l2, l3_mode="TSL", l3_tsl_pct=tsl)
        s(f"L1=K_RSI1(75%) | L2={int(l2*100)}% @ RSI72 | L3=TSL {tsl*100:.1f}%", t)

# ── SECTION E: L1 KINETIC + L2 PARTIAL + L3 HARD PROFIT ────────
print(f"\n{'='*130}")
print("SECTION E: FULL SYSTEM — L1 Kinetic + L2 Partial + L3 Hard Profit%")
print(f"{'='*130}")
for l2 in [0.50, 0.75]:
    for pp in [0.025, 0.030, 0.035, 0.040, 0.050]:
        t = backtest_multilayer(MP, strong40_days,
                                l1_kinetic=True, l1_frac=0.75,
                                l2_frac=l2, l3_mode="PROFIT_PCT", l3_profit_pct=pp)
        s(f"L1=K_RSI1(75%) | L2={int(l2*100)}% @ RSI72 | L3=Profit>={pp*100:.1f}%", t)

# ── SECTION F: L1 KINETIC ONLY + L2 PARTIAL EOD ─────────────────
print(f"\n{'='*130}")
print("SECTION F: L1 Kinetic(75%) + L2 Partial RSI72 (no L3)")
print(f"{'='*130}")
for l2 in [0.33, 0.50, 0.67, 0.75]:
    t = backtest_multilayer(MP, strong40_days,
                            l1_kinetic=True, l1_frac=0.75,
                            l2_frac=l2, l3_mode=None)
    s(f"L1=K_RSI1(75%) | L2={int(l2*100)}% @ RSI72 | L3=EOD", t)

# ── LEADERBOARD ─────────────────────────────────────────────────
print(f"\n{'='*130}")
print(f"MEGA LEADERBOARD — TOP 20 ({len(results)} configs)")
print(f"{'='*130}")
top = sorted(results, key=lambda x: x["ret"], reverse=True)[:20]
for i, rx in enumerate(top, 1):
    crown = " <<<<< KING" if i==1 else (" ***" if i<=3 else "")
    print(f"  #{i:2d} | {rx['label']:80s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{crown}")

print(f"\n{'='*130}")
print("SWEET SPOT: WR>=57% AND Ret>=34%")
print(f"{'='*130}")
sweet = sorted([x for x in results if x["wr"]>=57 and x["ret"]>=34],
               key=lambda x: x["ret"], reverse=True)
for i, rx in enumerate(sweet, 1):
    print(f"  #{i:2d} | {rx['label']:80s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}")
if not sweet:
    print("  (relaxing to WR>=55% AND Ret>=32%)")
    sweet2 = sorted([x for x in results if x["wr"]>=55 and x["ret"]>=32],
                    key=lambda x: x["ret"], reverse=True)[:8]
    for i, rx in enumerate(sweet2, 1):
        print(f"  #{i:2d} | {rx['label']:80s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}")

print(f"\nDONE. {len(results)} configs tested.")
