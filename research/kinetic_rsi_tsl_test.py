"""
MEGA KINETIC + RSI-BEYOND-72 TSL TEST
======================================

Two big ideas tested together:

IDEA 1: KINETIC CHECK AT PROFIT THRESHOLD
-----------------------------------------
When stock hits profit% → run "Kinetic Check" on current state:
  - Kinetic PASS -> "Stock still running!" -> HOLD all shares
  - Kinetic FAIL -> "Barely here, protect gains" -> Partial sell 50%

Kinetic Conditions tested (RSI(0) and RSI(1) variants):
  K1:  RSI rising       [RSI(0) > RSI(1)]
  K2:  Price > VWAP(0)
  K3:  Green candle     [close(0) > open(0)]
  K4:  EMA21 rising     [EMA21(0) > EMA21(3)]  -- 3-candle slope
  K5:  Price > EMA21(0)
  K1+K2: RSI rising AND above VWAP
  K1+K3: RSI rising AND green candle
  K1+K4: RSI rising AND EMA21 rising
  K2+K3: Above VWAP AND green candle
  K2+K4: Above VWAP AND EMA21 rising
  K3+K4: Green candle AND EMA21 rising
  K1+K2+K3: RSI rising AND VWAP AND green
  K1+K2+K4: RSI rising AND VWAP AND EMA21 slope
  K1+K3+K4: RSI rising AND green AND EMA21 slope
  K_ALL: All 4 conditions

IDEA 2: RSI>=72 -> TRAILING SL (instead of immediate exit)
-----------------------------------------------------------
Currently: RSI>=72 -> EXIT immediately
New idea:  RSI>=72 -> Activate tight TSL (lock in profit, let it run)
  - At RSI>=72, set TSL at price * (1 - trail%)
  - As price rises, TSL trails up
  - Exit when TSL hit OR EOD
  - Trail %: 0.3%, 0.5%, 0.8%, 1.0%, 1.5%

All on STRONG_GAP_40 + max_pos=3 + SL=1.0% base
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

# ── Load cached data ──────────────────────────────────────────
stock_dfs = load_cache()
print(f"{len(stock_dfs)} stocks loaded from cache.")

# ── Gap / Regime ──────────────────────────────────────────────
stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        by_day[d] = {"day_open": float(grp["open"].iloc[0]),
                     "prev_close": prev_close,
                     "gap_pct": (float(grp["open"].iloc[0]) - prev_close) / prev_close
                                 if prev_close else 0.0}
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

all_dates_set = set(d for days in stock_day_data.values() for d in days.keys())
strong40_days = set()
for d in all_dates_set:
    strong = sum(1 for sym, days in stock_day_data.items()
                 if d in days and days[d]["prev_close"]
                 and days[d]["gap_pct"] >= 0.005)
    total  = sum(1 for sym, days in stock_day_data.items()
                 if d in days and days[d]["prev_close"])
    if total > 0 and strong / total >= 0.40:
        strong40_days.add(d)
print(f"STRONG_GAP_40 days: {len(strong40_days)}")

all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

# ── Kinetic Condition Evaluators ──────────────────────────────
def kinetic_check(sliced, condition_name):
    """
    Returns True if stock has continued momentum (hold), False if weak (partial sell).
    sliced: df up to current candle (inclusive)
    """
    if len(sliced) < 5: return True  # not enough data, give benefit of doubt

    cc  = sliced.iloc[-1]    # current (0)
    pc  = sliced.iloc[-2]    # previous (1)
    ppc = sliced.iloc[-3] if len(sliced) >= 3 else pc  # (2)

    rsi0 = float(cc["rsi"])  if "rsi"   in cc.index and pd.notna(cc["rsi"])   else 50.0
    rsi1 = float(pc["rsi"])  if "rsi"   in pc.index and pd.notna(pc["rsi"])   else 50.0
    vwap = float(cc["vwap"]) if "vwap"  in cc.index and pd.notna(cc["vwap"])  else 0.0
    ema0 = float(cc["ema21"])if "ema21" in cc.index and pd.notna(cc["ema21"]) else 0.0
    ema3 = float(sliced.iloc[-4]["ema21"]) if (len(sliced) >= 4 and
           "ema21" in sliced.iloc[-4].index and pd.notna(sliced.iloc[-4]["ema21"])) else ema0

    close0 = float(cc["close"]); open0  = float(cc["open"])
    close1 = float(pc["close"]); open1  = float(pc["open"])

    rsi_rising   = rsi0 > rsi1                   # RSI accelerating
    above_vwap   = close0 > vwap > 0             # above VWAP
    green_candle = close0 > open0                 # current candle bullish
    green_prev   = close1 > open1                 # previous candle bullish (RSI1 variant)
    ema_rising   = ema0 > ema3                    # EMA21 sloping up (3-candle)
    above_ema    = close0 > ema0 > 0              # price above EMA21

    # RSI(1) variants — use PREVIOUS candle's RSI for comparison
    rsi0_vs_rsi2 = rsi1 > float(ppc["rsi"]) if ("rsi" in ppc.index and pd.notna(ppc["rsi"])) else True
    rsi_rising_1 = rsi0_vs_rsi2  # RSI(1): was RSI rising into THIS candle?

    conds = {
        # Single conditions — RSI(0) based
        "K_RSI_RISING":          rsi_rising,
        "K_ABOVE_VWAP":          above_vwap,
        "K_GREEN_CANDLE":        green_candle,
        "K_EMA_RISING":          ema_rising,
        "K_ABOVE_EMA":           above_ema,

        # Single conditions — RSI(1) / prev candle variants
        "K_RSI_RISING_1":        rsi_rising_1,
        "K_GREEN_PREV":          green_prev,

        # Dual combos (RSI0)
        "K_RSI+VWAP":            rsi_rising and above_vwap,
        "K_RSI+GREEN":           rsi_rising and green_candle,
        "K_RSI+EMA":             rsi_rising and ema_rising,
        "K_RSI+ABVEMA":          rsi_rising and above_ema,
        "K_VWAP+GREEN":          above_vwap and green_candle,
        "K_VWAP+EMA":            above_vwap and ema_rising,
        "K_GREEN+EMA":           green_candle and ema_rising,

        # Dual combos — RSI(1) variants
        "K_RSI1+VWAP":           rsi_rising_1 and above_vwap,
        "K_RSI1+GREEN":          rsi_rising_1 and green_candle,
        "K_RSI1+EMA":            rsi_rising_1 and ema_rising,
        "K_RSI1+ABVEMA":         rsi_rising_1 and above_ema,
        "K_RSI0+GREEN1":         rsi_rising and green_prev,

        # Triple combos
        "K_RSI+VWAP+GREEN":      rsi_rising and above_vwap and green_candle,
        "K_RSI+VWAP+EMA":        rsi_rising and above_vwap and ema_rising,
        "K_RSI+GREEN+EMA":       rsi_rising and green_candle and ema_rising,
        "K_RSI+VWAP+ABVEMA":     rsi_rising and above_vwap and above_ema,
        "K_RSI1+VWAP+GREEN":     rsi_rising_1 and above_vwap and green_candle,
        "K_RSI1+VWAP+EMA":       rsi_rising_1 and above_vwap and ema_rising,

        # All 4
        "K_ALL":                 rsi_rising and above_vwap and green_candle and ema_rising,
        "K_ALL_1":               rsi_rising_1 and above_vwap and green_prev and ema_rising,
    }

    return conds.get(condition_name, True)

# ── Core Backtests ────────────────────────────────────────────

def backtest_kinetic(max_pos, allowed_dates, profit_thr, partial_frac,
                     kinetic_name, kinetic_required_for_hold=True):
    """
    At profit_thr: run kinetic check.
      kinetic=True  -> HOLD (stock is strong, let it run to RSI>=72)
      kinetic=False -> partial sell partial_frac of qty
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
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["remaining_qty"]
            if qty <= 0: closed.append(sym); continue

            rsi0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0

            # KINETIC CHECK at profit threshold (one-time)
            if not pos["kinetic_done"]:
                profit_now = (close - ep) / ep
                if profit_now >= profit_thr:
                    is_strong = kinetic_check(sliced, kinetic_name)
                    pos["kinetic_done"] = True
                    if not is_strong:
                        # Momentum weak -> partial sell
                        sell_qty = max(1, int(qty * partial_frac))
                        if sell_qty >= qty: sell_qty = max(0, qty - 1)
                        if sell_qty > 0:
                            trades.append({"pnl": (close - ep) * sell_qty, "reason": "K_PARTIAL"})
                            pos["remaining_qty"] -= sell_qty
                            qty = pos["remaining_qty"]
                            if qty <= 0: closed.append(sym); continue
                    # else: kinetic strong -> hold all, do nothing

            # RSI high exit (remaining qty)
            if rsi0 >= 72:
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
            if idx < 10: continue
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
                        "ep": ep, "total_qty": total_qty,
                        "remaining_qty": total_qty,
                        "sl": sl_p, "tgt": round_to_tick(ep + abs(ep - sl_p) * 10.0),
                        "tsl": sl_p, "tsl_on": False,
                        "kinetic_done": False,
                    }

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        if pos["remaining_qty"] > 0:
            trades.append({"pnl": (lc - pos["ep"]) * pos["remaining_qty"], "reason": "EOD"})
    return trades


def backtest_rsi_tsl(max_pos, allowed_dates, rsi_tsl_trail_pct):
    """
    RSI >= 72 -> DON'T exit, instead activate TIGHT trailing SL
    As price climbs further, TSL trails up
    Exit when TSL hit OR EOD
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
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]

            rsi0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0

            # RSI >= 72 -> activate TIGHT trailing SL (if not already active)
            if rsi0 >= 72 and not pos.get("rsi_tsl_on"):
                pos["rsi_tsl_on"] = True
                # Set TSL just below current close
                pos["rsi_tsl"] = round_to_tick(close * (1 - rsi_tsl_trail_pct))

            # If RSI-TSL is active, trail it up and check for hit
            if pos.get("rsi_tsl_on"):
                # Update TSL to new high
                new_tsl = round_to_tick(high * (1 - rsi_tsl_trail_pct))
                if new_tsl > pos["rsi_tsl"]: pos["rsi_tsl"] = new_tsl
                # Check if low hit the RSI-TSL
                if low <= pos["rsi_tsl"]:
                    ex = min(pos["rsi_tsl"], float(cc["open"]))
                    trades.append({"pnl": (ex - ep) * qty, "reason": "RSI_TSL"})
                    closed.append(sym); continue

            # Normal TSL
            if not pos.get("rsi_tsl_on"):
                if high >= ep + abs(ep - pos["sl"]) * TSL_ACTIVATION: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high * (1 - TSL_PCT))
                    if n > pos["tsl"]: pos["tsl"] = n

            # Normal exits
            ex = None
            if low <= pos["tsl"] and not pos.get("rsi_tsl_on"):
                ex = min(pos["tsl"], float(cc["open"]))
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
                    qty = int(per_slot // ep)
                    if qty < 1: continue
                    sl_p = round_to_tick(ep * (1 - SL_PCT))
                    positions[sym] = {
                        "ep": ep, "qty": qty, "sl": sl_p,
                        "tgt": round_to_tick(ep + abs(ep - sl_p) * 10.0),
                        "tsl": sl_p, "tsl_on": False,
                        "rsi_tsl_on": False, "rsi_tsl": 0.0,
                    }

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "reason": "EOD"})
    return trades


# ── Stats + Leaderboard ───────────────────────────────────────
results = []
def stats(label, trades):
    if not trades: print(f"  {label:80s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    flag = " ***" if ret >= 35 else (" <<<" if ret >= 32 else "")
    print(f"  {label:80s} | WR={wr:5.1f}% | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")
    results.append({"label": label, "wr": wr, "net": net, "ret": ret, "t": len(pnls)})

# ── BASELINE ──────────────────────────────────────────────────
print(f"\n{'='*125}")
print("BASELINE: RSI(0)>=72 full exit")
print(f"{'='*125}")
# Quick baseline using rsi_tsl with very tight trail (effectively same as exit)
def backtest_base(max_pos, allowed_dates):
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
            rsi0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0
            if rsi0 >= 72:
                trades.append({"pnl": (close - ep) * qty, "reason": "RSI"})
                closed.append(sym); continue
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
                trades.append({"pnl": (ex - ep) * qty, "reason": "NRM"})
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
            if idx < 10: continue
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced,
                                            pattern_df=sliced, ws_count=0)
            cond = evaluator._evaluate_conditions(buy_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"]); qty = int(per_slot // ep)
                    if qty < 1: continue
                    sl_p = round_to_tick(ep * (1 - SL_PCT))
                    positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p,
                                      "tgt": round_to_tick(ep + abs(ep - sl_p) * 10.0),
                                      "tsl": sl_p, "tsl_on": False}
    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "reason": "EOD"})
    return trades

stats("BASELINE | RSI(0)>=72 full exit | SL=1.0%", backtest_base(MP, strong40_days))

# ── SECTION A: RSI>=72 -> TRAILING SL ─────────────────────────
print(f"\n{'='*125}")
print("SECTION A: RSI>=72 -> ACTIVATE TIGHT TRAILING SL (let profits run beyond 72!)")
print(f"{'='*125}")
for trail in [0.003, 0.005, 0.007, 0.008, 0.010, 0.012, 0.015, 0.020]:
    t = backtest_rsi_tsl(MP, strong40_days, rsi_tsl_trail_pct=trail)
    stats(f"RSI>=72 -> TSL trail={trail*100:.1f}% | SL=1.0%", t)

# ── SECTION B: KINETIC CHECK (all kinetic strategies) ─────────
KINETIC_STRATEGIES = [
    "K_RSI_RISING", "K_ABOVE_VWAP", "K_GREEN_CANDLE",
    "K_EMA_RISING", "K_ABOVE_EMA",
    "K_RSI_RISING_1", "K_GREEN_PREV",
    "K_RSI+VWAP", "K_RSI+GREEN", "K_RSI+EMA", "K_RSI+ABVEMA",
    "K_VWAP+GREEN", "K_VWAP+EMA", "K_GREEN+EMA",
    "K_RSI1+VWAP", "K_RSI1+GREEN", "K_RSI1+EMA", "K_RSI1+ABVEMA",
    "K_RSI0+GREEN1",
    "K_RSI+VWAP+GREEN", "K_RSI+VWAP+EMA", "K_RSI+GREEN+EMA",
    "K_RSI+VWAP+ABVEMA",
    "K_RSI1+VWAP+GREEN", "K_RSI1+VWAP+EMA",
    "K_ALL", "K_ALL_1",
]

PROFIT_THRESHOLDS = [0.0075, 0.010, 0.0125, 0.015]
PARTIAL_FRACS = [0.33, 0.50]

print(f"\n{'='*125}")
print("SECTION B: KINETIC CHECK at profit threshold")
print("  Kinetic PASS = stock strong -> HOLD all -> RSI>=72")
print("  Kinetic FAIL = stock weak   -> PARTIAL SELL 50%")
print(f"{'='*125}")

for kinetic in KINETIC_STRATEGIES:
    print(f"\n  -- Kinetic: {kinetic} --")
    for thr in PROFIT_THRESHOLDS:
        for frac in PARTIAL_FRACS:
            t = backtest_kinetic(MP, strong40_days,
                                 profit_thr=thr, partial_frac=frac,
                                 kinetic_name=kinetic)
            stats(f"  {kinetic:25s} | thr={thr*100:.2f}% | sell={frac*100:.0f}% if weak", t)

# ── SECTION C: RSI-TSL + KINETIC COMBINED ─────────────────────
print(f"\n{'='*125}")
print("SECTION C: BEST OF BOTH -- RSI>=72 -> TSL + Kinetic partial at threshold")
print(f"{'='*125}")
# Take best TSL trail from section A, combine with best kinetic from section B
# Quick test with most promising combos
for trail in [0.005, 0.008, 0.010]:
    t = backtest_rsi_tsl(MP, strong40_days, rsi_tsl_trail_pct=trail)
    stats(f"RSI>=72->TSL {trail*100:.1f}% (no kinetic)", t)

# ── LEADERBOARD ────────────────────────────────────────────────
print(f"\n{'='*125}")
print(f"MEGA LEADERBOARD -- TOP 20 by RETURN ({len(results)} configs)")
print(f"{'='*125}")
top = sorted(results, key=lambda x: x["ret"], reverse=True)[:20]
for i, rx in enumerate(top, 1):
    king = " <-- KING" if i == 1 else (" ***" if i<=3 else "")
    print(f"  #{i:2d} | {rx['label']:80s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{king}")

print(f"\n{'='*125}")
print("SWEET SPOT: WR>=55% AND Ret>=32%")
print(f"{'='*125}")
sweet = sorted([x for x in results if x["wr"]>=55 and x["ret"]>=32],
               key=lambda x: (x["ret"], x["wr"]), reverse=True)
for i, rx in enumerate(sweet, 1):
    print(f"  #{i:2d} | {rx['label']:80s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}")
if not sweet:
    print("  (None found with WR>=55% AND Ret>=32%)")

print(f"\nDONE. {len(results)} configs tested.")
