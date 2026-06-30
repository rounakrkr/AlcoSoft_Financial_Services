"""
MEGA RSI PERMUTATION SWEEP
===========================
Tests ALL combinations of:
  - High exit (profit booking): RSI thresholds 68-78, RSI(0) and RSI(1)
  - Low exit (panic/momentum loss): RSI thresholds 42-52, RSI(0) and RSI(1)
  - Combined: every high × every low × every lookback combo

Total configs: ~500+
Goal: Find THE BEST exit strategy. No regrets!
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

# Gap data for STRONG_GAP_40
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

def get_rsi(sliced, lookback=0):
    idx = -(1 + lookback)
    if len(sliced) < abs(idx): return None
    v = sliced["rsi"].iloc[idx]
    return float(v) if pd.notna(v) else None

def portfolio_backtest(max_pos, allowed_dates,
                       hi_thr, hi_lb,        # high exit: threshold + lookback (0 or 1)
                       lo_thr=None, lo_lb=0,  # low panic exit: threshold + lookback
                       min_hold=2):           # candles to hold before low panic triggers
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
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]

            rsi_hi = get_rsi(sliced, hi_lb)
            rsi_lo = get_rsi(sliced, lo_lb) if lo_thr else None

            # HIGH EXIT: overbought → full profit booking
            if rsi_hi is not None and rsi_hi >= hi_thr:
                trades.append({"pnl": (close - ep) * qty, "r": "HI"})
                closed.append(sym); continue

            # LOW EXIT: momentum lost → panic exit
            if (lo_thr and rsi_lo is not None
                    and pos["candles_held"] >= min_hold
                    and rsi_lo < lo_thr):
                trades.append({"pnl": (close - ep) * qty, "r": "LO"})
                closed.append(sym); continue

            # Normal exits: TSL, target, sell signal, EOD
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
                sl_pct = abs(ep - pos["sl"]) / ep if ep > 0 else 0
                if high >= ep + ep * sl_pct * tsl_activation_ratio: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high * (1 - trailing_sl_percent))
                    if n > pos["tsl"]: pos["tsl"] = n
                pos["candles_held"] += 1

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
                    ep = float(df.iloc[idx+1]["open"]); qty = int(per_slot // ep)
                    if qty < 1: continue
                    sl_p = calculate_stop_loss(ep, "BUY")
                    positions[sym] = {
                        "ep": ep, "qty": qty, "sl": sl_p,
                        "tgt": calculate_target(ep, sl_p),
                        "tsl": sl_p, "tsl_on": False,
                        "candles_held": 0,
                    }

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "r": "EOD"})
    return trades

results = []
def r(label, trades):
    if not trades: return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    flag = " ***" if ret >= 31 else (" <<<" if ret >= 26 else "")
    print(f"  {label:75s} | WR={wr:5.1f}% | Net={net:+10,.0f} | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")
    results.append({"label": label, "wr": wr, "net": net, "ret": ret, "t": len(pnls)})

MP = 3
HI_THRESHOLDS = list(range(68, 79))   # 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78
LO_THRESHOLDS = list(range(42, 53))   # 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52
LOOKBACKS     = [0, 1]

total = (len(HI_THRESHOLDS) * len(LOOKBACKS))           \
      + (len(HI_THRESHOLDS) * len(LOOKBACKS)             \
         * len(LO_THRESHOLDS) * len(LOOKBACKS))
print(f"\nTotal configs to test: {total}")

# ── SECTION 1: HIGH EXIT ONLY (no panic) ──────────────────────
print(f"\n{'='*120}")
print("SECTION 1: HIGH EXIT ONLY — RSI(0) and RSI(1), thresholds 68-78")
print(f"{'='*120}")
for lb in LOOKBACKS:
    print(f"\n  -- RSI({lb}) --")
    for thr in HI_THRESHOLDS:
        trades = portfolio_backtest(MP, strong40_days, hi_thr=thr, hi_lb=lb)
        r(f"HI_ONLY | FULL@RSI({lb})>={thr}", trades)

# ── SECTION 2: RSI(0) HIGH EXIT + RSI(0) LOW PANIC ───────────
print(f"\n{'='*120}")
print("SECTION 2: HIGH@RSI(0) + PANIC@RSI(0) — all combos")
print(f"{'='*120}")
for hi_thr in HI_THRESHOLDS:
    for lo_thr in LO_THRESHOLDS:
        trades = portfolio_backtest(MP, strong40_days,
                                    hi_thr=hi_thr, hi_lb=0,
                                    lo_thr=lo_thr, lo_lb=0)
        r(f"RSI(0)>=({hi_thr}) HIGH + RSI(0)<({lo_thr}) PANIC", trades)

# ── SECTION 3: RSI(0) HIGH EXIT + RSI(1) LOW PANIC ───────────
print(f"\n{'='*120}")
print("SECTION 3: HIGH@RSI(0) + PANIC@RSI(1) — confirmed candle for panic")
print(f"{'='*120}")
for hi_thr in HI_THRESHOLDS:
    for lo_thr in LO_THRESHOLDS:
        trades = portfolio_backtest(MP, strong40_days,
                                    hi_thr=hi_thr, hi_lb=0,
                                    lo_thr=lo_thr, lo_lb=1)
        r(f"RSI(0)>=({hi_thr}) HIGH + RSI(1)<({lo_thr}) PANIC", trades)

# ── SECTION 4: RSI(1) HIGH EXIT + RSI(0) LOW PANIC ───────────
print(f"\n{'='*120}")
print("SECTION 4: HIGH@RSI(1) + PANIC@RSI(0) — confirmed candle for high exit")
print(f"{'='*120}")
for hi_thr in HI_THRESHOLDS:
    for lo_thr in LO_THRESHOLDS:
        trades = portfolio_backtest(MP, strong40_days,
                                    hi_thr=hi_thr, hi_lb=1,
                                    lo_thr=lo_thr, lo_lb=0)
        r(f"RSI(1)>=({hi_thr}) HIGH + RSI(0)<({lo_thr}) PANIC", trades)

# ── SECTION 5: RSI(1) HIGH + RSI(1) LOW ──────────────────────
print(f"\n{'='*120}")
print("SECTION 5: HIGH@RSI(1) + PANIC@RSI(1) — both confirmed candles")
print(f"{'='*120}")
for hi_thr in HI_THRESHOLDS:
    for lo_thr in LO_THRESHOLDS:
        trades = portfolio_backtest(MP, strong40_days,
                                    hi_thr=hi_thr, hi_lb=1,
                                    lo_thr=lo_thr, lo_lb=1)
        r(f"RSI(1)>=({hi_thr}) HIGH + RSI(1)<({lo_thr}) PANIC", trades)

# ── FINAL LEADERBOARD ─────────────────────────────────────────
print(f"\n{'='*120}")
print(f"MEGA LEADERBOARD — TOP 20 by RETURN ({len(results)} configs tested)")
print(f"{'='*120}")
top = sorted(results, key=lambda x: x["ret"], reverse=True)[:20]
for i, rx in enumerate(top, 1):
    crown = " 👑 KING" if i == 1 else (" ***" if i <= 3 else "")
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | Net={rx['net']:+10,.0f} | Ret={rx['ret']:+6.1f}%{crown}")

print(f"\n{'='*120}")
print("TOP 10 by WIN RATE (positive return only)")
print(f"{'='*120}")
top_wr = sorted([x for x in results if x["ret"] > 0], key=lambda x: x["wr"], reverse=True)[:10]
for i, rx in enumerate(top_wr, 1):
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | Net={rx['net']:+10,.0f} | Ret={rx['ret']:+6.1f}%")

print(f"\n{'='*120}")
print(f"Total configs tested: {len(results)} | DONE.")
