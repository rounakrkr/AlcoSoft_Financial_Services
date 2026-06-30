"""
KINETIC @ 0.5% — THE REAL TEST
================================
From partial exit sweep: only 0.5% threshold actually fires (40 trades)
Kinetic at higher thresholds (0.75-1.5%) = RSI>=72 always hits first = kinetic never tested!

So: test kinetic specifically at 0.5% profit threshold
  Kinetic PASS → HOLD all (strong stock, let RSI>=72 run)
  Kinetic FAIL → PARTIAL sell (50% or 75%)

This is the TRUE test: can kinetic differentiate the 40 stocks
that hit +0.5% before RSI>=72?
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
MP = 3; SL_PCT = 0.010; PROFIT_THR = 0.005  # 0.5% — the REAL threshold

config       = load_strategy_sets()
buy_set_def  = next(s for s in config.buy_sets  if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
sell_set_def = next(s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS")
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)
TSL_ACTIVATION = float(cfg("risk", "tsl_activation_ratio", 1.2))
TSL_PCT        = float(cfg("risk", "trailing_sl_percent",  0.002))

stock_dfs = load_cache()
print(f"{len(stock_dfs)} stocks from cache.")

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

def get_vals(sliced):
    cc  = sliced.iloc[-1]
    pc  = sliced.iloc[-2] if len(sliced) >= 2 else cc
    ppc = sliced.iloc[-3] if len(sliced) >= 3 else pc
    p4  = sliced.iloc[-4] if len(sliced) >= 4 else pc

    rsi0  = float(cc.get("rsi",   50)) if pd.notna(cc.get("rsi",  np.nan)) else 50.0
    rsi1  = float(pc.get("rsi",   50)) if pd.notna(pc.get("rsi",  np.nan)) else 50.0
    rsi2  = float(ppc.get("rsi",  50)) if pd.notna(ppc.get("rsi", np.nan)) else 50.0
    vwap  = float(cc.get("vwap",  0))  if pd.notna(cc.get("vwap", np.nan)) else 0.0
    ema0  = float(cc.get("ema21", 0))  if pd.notna(cc.get("ema21",np.nan)) else 0.0
    ema3  = float(p4.get("ema21", 0))  if pd.notna(p4.get("ema21",np.nan)) else ema0
    c0, o0 = float(cc.get("close",0)), float(cc.get("open",0))
    c1, o1 = float(pc.get("close",0)), float(pc.get("open",0))
    return rsi0,rsi1,rsi2,vwap,ema0,ema3,c0,o0,c1,o1

def kinetic(sliced, name):
    if len(sliced) < 4: return True
    rsi0,rsi1,rsi2,vwap,ema0,ema3,c0,o0,c1,o1 = get_vals(sliced)
    K = {
        "K_RSI_RISING":    rsi0 > rsi1,
        "K_RSI_RISING_1":  rsi1 > rsi2,
        "K_ABOVE_VWAP":    c0 > vwap > 0,
        "K_GREEN_CANDLE":  c0 > o0,
        "K_GREEN_PREV":    c1 > o1,
        "K_EMA_RISING":    ema0 > ema3,
        "K_ABOVE_EMA":     c0 > ema0 > 0,
        "K_RSI+VWAP":      rsi0>rsi1 and c0>vwap>0,
        "K_RSI+GREEN":     rsi0>rsi1 and c0>o0,
        "K_RSI+EMA":       rsi0>rsi1 and ema0>ema3,
        "K_RSI+ABVEMA":    rsi0>rsi1 and c0>ema0>0,
        "K_VWAP+GREEN":    c0>vwap>0 and c0>o0,
        "K_VWAP+EMA":      c0>vwap>0 and ema0>ema3,
        "K_GREEN+EMA":     c0>o0 and ema0>ema3,
        "K_RSI1+VWAP":     rsi1>rsi2 and c0>vwap>0,
        "K_RSI1+GREEN":    rsi1>rsi2 and c0>o0,
        "K_RSI1+EMA":      rsi1>rsi2 and ema0>ema3,
        "K_RSI1+ABVEMA":   rsi1>rsi2 and c0>ema0>0,
        "K_RSI0+GREEN1":   rsi0>rsi1 and c1>o1,
        "K_RSI+VWAP+GREEN":rsi0>rsi1 and c0>vwap>0 and c0>o0,
        "K_RSI+VWAP+EMA":  rsi0>rsi1 and c0>vwap>0 and ema0>ema3,
        "K_RSI+GREEN+EMA": rsi0>rsi1 and c0>o0 and ema0>ema3,
        "K_RSI1+VWAP+GREEN":rsi1>rsi2 and c0>vwap>0 and c0>o0,
        "K_RSI1+VWAP+EMA": rsi1>rsi2 and c0>vwap>0 and ema0>ema3,
        "K_ALL":  rsi0>rsi1 and c0>vwap>0 and c0>o0 and ema0>ema3,
        "K_ALL_1":rsi1>rsi2 and c0>vwap>0 and c1>o1 and ema0>ema3,
    }
    return K.get(name, True)

def backtest(max_pos, allowed_dates, kinetic_name=None, partial_frac=0.50,
             blind_partial=False):
    """
    blind_partial=True  → always partial sell at 0.5% (no kinetic, baseline comparison)
    kinetic_name set    → at 0.5%: check kinetic. PASS=hold, FAIL=partial sell
    kinetic_name=None   → baseline (no partial)
    """
    per_slot = BUYING_POWER / max_pos
    positions = {}; trades = []
    kinetic_stats = {"checked":0, "pass":0, "fail":0,
                     "pass_then_rsi":0, "pass_then_sl":0,
                     "fail_then_rsi":0, "fail_then_sl":0}

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
            ep = pos["ep"]; qty = pos["remaining_qty"]
            if qty <= 0: closed.append(sym); continue

            rsi0 = float(cc.get("rsi",50)) if pd.notna(cc.get("rsi",np.nan)) else 50.0

            # KINETIC / BLIND PARTIAL check at 0.5%
            if not pos["partial_done"] and (close - ep) / ep >= PROFIT_THR:
                pos["partial_done"] = True
                do_partial = False
                if blind_partial:
                    do_partial = True
                elif kinetic_name:
                    kinetic_stats["checked"] += 1
                    is_strong = kinetic(sliced, kinetic_name)
                    if is_strong:
                        kinetic_stats["pass"] += 1
                        pos["kinetic_result"] = "pass"
                    else:
                        kinetic_stats["fail"] += 1
                        do_partial = True
                        pos["kinetic_result"] = "fail"

                if do_partial:
                    sell_qty = max(1, int(qty * partial_frac))
                    if sell_qty >= qty: sell_qty = max(0, qty-1)
                    if sell_qty > 0:
                        trades.append({"pnl":(close-ep)*sell_qty,"reason":"PARTIAL"})
                        pos["remaining_qty"] -= sell_qty
                        qty = pos["remaining_qty"]
                        if qty <= 0: closed.append(sym); continue

            # RSI exit
            if rsi0 >= 72:
                if pos.get("kinetic_result") == "pass": kinetic_stats["pass_then_rsi"] += 1
                elif pos.get("kinetic_result") == "fail": kinetic_stats["fail_then_rsi"] += 1
                trades.append({"pnl":(close-ep)*qty,"reason":"RSI"})
                closed.append(sym); continue

            # Normal exits
            ex = None
            if low <= pos["tsl"]:    ex = min(pos["tsl"], float(cc["open"]))
            elif high >= pos["tgt"]: ex = max(pos["tgt"], float(cc["open"]))
            elif ts.hour==15 and ts.minute>=15: ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df): ex = float(df.iloc[idx+1]["open"])
            if ex:
                if pos.get("kinetic_result") == "pass": kinetic_stats["pass_then_sl"] += 1
                elif pos.get("kinetic_result") == "fail": kinetic_stats["fail_then_sl"] += 1
                trades.append({"pnl":(ex-ep)*qty,"reason":"NRM"})
                closed.append(sym)
            else:
                if high >= ep + abs(ep-pos["sl"]) * TSL_ACTIVATION: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high*(1-TSL_PCT))
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
                    sl_p = round_to_tick(ep*(1-SL_PCT))
                    positions[sym] = {
                        "ep":ep, "remaining_qty":total_qty,
                        "sl":sl_p, "tgt":round_to_tick(ep+abs(ep-sl_p)*10.0),
                        "tsl":sl_p, "tsl_on":False,
                        "partial_done":False, "kinetic_result": None,
                    }
    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        if pos["remaining_qty"] > 0:
            trades.append({"pnl":(lc-pos["ep"])*pos["remaining_qty"],"reason":"EOD"})
    return trades, kinetic_stats

results = []
def stats(label, trades, kst=None):
    if not trades: print(f"  {label:75s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    flag = " ***" if ret >= 34 else (" <<<" if ret >= 32 else "")
    kinfo = ""
    if kst and kst["checked"] > 0:
        kinfo = (f" | K_checked={kst['checked']} PASS={kst['pass']} FAIL={kst['fail']}"
                 f" | pass->RSI={kst['pass_then_rsi']} pass->SL={kst['pass_then_sl']}"
                 f" | fail->RSI={kst['fail_then_rsi']} fail->SL={kst['fail_then_sl']}")
    print(f"  {label:75s} | WR={wr:5.1f}% | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}{kinfo}")
    results.append({"label":label,"wr":wr,"net":net,"ret":ret,"t":len(pnls)})

KINETIC_LIST = [
    "K_RSI_RISING","K_RSI_RISING_1","K_ABOVE_VWAP","K_GREEN_CANDLE",
    "K_GREEN_PREV","K_EMA_RISING","K_ABOVE_EMA",
    "K_RSI+VWAP","K_RSI+GREEN","K_RSI+EMA","K_RSI+ABVEMA",
    "K_VWAP+GREEN","K_VWAP+EMA","K_GREEN+EMA",
    "K_RSI1+VWAP","K_RSI1+GREEN","K_RSI1+EMA","K_RSI1+ABVEMA",
    "K_RSI0+GREEN1",
    "K_RSI+VWAP+GREEN","K_RSI+VWAP+EMA","K_RSI+GREEN+EMA",
    "K_RSI1+VWAP+GREEN","K_RSI1+VWAP+EMA",
    "K_ALL","K_ALL_1",
]

print(f"\n{'='*130}")
print("BASELINES")
print(f"{'='*130}")
t,_ = backtest(MP, strong40_days)
stats("BASELINE | no partial | RSI>=72 exit", t)
t,_ = backtest(MP, strong40_days, blind_partial=True, partial_frac=0.50)
stats("BLIND PARTIAL 50% @ +0.5% | no kinetic", t)
t,_ = backtest(MP, strong40_days, blind_partial=True, partial_frac=0.75)
stats("BLIND PARTIAL 75% @ +0.5% | no kinetic", t)

print(f"\n{'='*130}")
print("KINETIC @ 0.5% | sell=50% if WEAK, HOLD if STRONG")
print("(Showing kinetic stats: how many checked, passed, failed, and their outcomes)")
print(f"{'='*130}")
for k in KINETIC_LIST:
    t, kst = backtest(MP, strong40_days, kinetic_name=k, partial_frac=0.50)
    stats(f"K={k:25s} sell=50% if weak", t, kst)

print(f"\n{'='*130}")
print("KINETIC @ 0.5% | sell=75% if WEAK")
print(f"{'='*130}")
for k in KINETIC_LIST:
    t, kst = backtest(MP, strong40_days, kinetic_name=k, partial_frac=0.75)
    stats(f"K={k:25s} sell=75% if weak", t, kst)

print(f"\n{'='*130}")
print(f"LEADERBOARD — TOP 15 ({len(results)} configs)")
print(f"{'='*130}")
top = sorted(results, key=lambda x: x["ret"], reverse=True)[:15]
for i, rx in enumerate(top, 1):
    crown = " <-- KING" if i==1 else (" ***" if i<=3 else "")
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{crown}")

print(f"\nDONE. {len(results)} configs.")
