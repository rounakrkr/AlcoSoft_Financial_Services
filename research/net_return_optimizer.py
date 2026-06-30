"""
NET RETURN OPTIMIZER — FULL REBUILD
=====================================
Tabula rasa. Sab fresh.

Capital: Rs.1L + 5x leverage = Rs.5L buying power
STT per trade: 0.025% sell side + exchange + stamp = ~0.0351% of position
               (Kotak algo = no brokerage, just govt charges)

Goal: Find optimal max_pos AND trade strategy for MAX NET return after STT

Tests:
  A. max_pos sweep (1 to 6) — find optimal position count
  B. Time filter (morning only) — fewer trades = less STT
  C. Individual stock gap filter — quality over quantity
  D. Max daily trade cap — hard limit on daily trades

ALL results show:
  - Gross Return (backtest)
  - Trade Count
  - Total STT Cost
  - NET Return (what actually lands in your pocket)
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

CAPITAL = 100000.0
MARGIN  = 5.0
BUYING_POWER = CAPITAL * MARGIN  # Rs.5,00,000
SL_PCT  = 0.010

# STT + Exchange + Stamp + SEBI (no brokerage - Kotak algo free)
# Approximate: 0.025% STT (sell) + 0.00345% exchange both sides + 0.003% stamp (buy) + GST
# Total per trade: ~0.0351% of position value (sell side dominant)
STT_RATE = 0.000351  # applied to sell-side position value

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
        by_day[d] = {
            "prev_close": prev_close,
            "gap_pct": (float(grp["open"].iloc[0]) - prev_close) / prev_close if prev_close else 0.0,
            "day_open": float(grp["open"].iloc[0])
        }
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

def backtest(max_pos, allowed_dates,
             buy_before_hour=15,   # only buy before this hour (default: all day)
             stock_min_gap=None,   # individual stock must have gapped >= this % today
             max_trades_per_day=None,  # hard cap on new positions per day
             rsi_hi=72):
    per_slot = BUYING_POWER / max_pos
    positions = {}
    trades = []
    daily_trade_count = {}

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

            if rsi0 >= rsi_hi:
                sell_val = close * qty
                stt = sell_val * STT_RATE
                trades.append({"pnl": (close - ep) * qty, "stt": stt, "reason": "RSI"})
                closed.append(sym); continue

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
                sell_val = ex * qty
                stt = sell_val * STT_RATE
                trades.append({"pnl": (ex - ep) * qty, "stt": stt, "reason": "NRM"})
                closed.append(sym)
            else:
                if high >= ep + abs(ep - pos["sl"]) * TSL_ACTIVATION: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high * (1 - TSL_PCT))
                    if n > pos["tsl"]: pos["tsl"] = n

        for s in closed: del positions[s]
        if len(positions) >= max_pos: continue
        if ts.hour >= buy_before_hour: continue
        if d not in allowed_dates: continue

        # Daily trade cap
        day_trades = daily_trade_count.get(d, 0)
        if max_trades_per_day and day_trades >= max_trades_per_day: continue

        for sym in stock_dfs:
            if len(positions) >= max_pos: break
            if sym in positions: continue

            # Individual stock gap filter
            if stock_min_gap:
                sd = stock_day_data.get(sym, {}).get(d)
                if not sd or not sd["prev_close"] or sd["gap_pct"] < stock_min_gap:
                    continue

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
                    }
                    daily_trade_count[d] = daily_trade_count.get(d, 0) + 1

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        sell_val = lc * pos["qty"]
        stt = sell_val * STT_RATE
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "stt": stt, "reason": "EOD"})
    return trades

results = []
def stats(label, trades, max_pos_used=3):
    if not trades:
        print(f"  {label:80s} | NO TRADES"); return
    per_slot = BUYING_POWER / max_pos_used
    pnls     = [t["pnl"] for t in trades]
    stts     = [t.get("stt", per_slot * STT_RATE) for t in trades]
    wins     = sum(1 for p in pnls if p > 0)
    wr       = wins / len(pnls) * 100
    gross    = sum(pnls)
    total_stt= sum(stts)
    net      = gross - total_stt
    gross_r  = gross / CAPITAL * 100
    net_r    = net   / CAPITAL * 100
    stt_r    = total_stt / CAPITAL * 100
    flag     = " ***" if net_r >= 18 else (" <<<" if net_r >= 15 else "")
    print(f"  {label:75s} | WR={wr:5.1f}% | T={len(pnls):4d} | Gross={gross_r:+6.1f}% | STT={stt_r:5.1f}% | NET={net_r:+6.1f}%{flag}")
    results.append({"label": label, "wr": wr, "t": len(pnls),
                    "gross": gross_r, "stt": stt_r, "net": net_r, "mp": max_pos_used})

# ────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print("SECTION A: MAX_POS SWEEP (1 to 6) — Find optimal position count")
print("Per-slot size changes with max_pos. STT per trade changes accordingly.")
print(f"{'='*140}")
for mp in [1, 2, 3, 4, 5, 6]:
    slot = BUYING_POWER / mp
    stt_per_trade = slot * STT_RATE
    t = backtest(mp, strong40_days)
    label = f"max_pos={mp} | slot=Rs.{slot/1000:.0f}K | STT/trade~Rs.{stt_per_trade:.0f}"
    stats(label, t, max_pos_used=mp)

# ────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print("SECTION B: TIME FILTER — Only buy in morning hours (fewer trades)")
print("max_pos=3 base. Only accept buy signals before X:00")
print(f"{'='*140}")
for hour in [10, 11, 12, 13, 14]:
    t = backtest(3, strong40_days, buy_before_hour=hour)
    stats(f"max_pos=3 | Buy only before {hour:02d}:00", t, max_pos_used=3)

# ────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print("SECTION C: INDIVIDUAL STOCK GAP FILTER — Quality buy signals")
print("max_pos=3 base. Stock must have gapped up >= X% today to be tradeable")
print(f"{'='*140}")
for min_gap in [0.003, 0.005, 0.007, 0.010, 0.012, 0.015, 0.020]:
    t = backtest(3, strong40_days, stock_min_gap=min_gap)
    stats(f"max_pos=3 | Stock gap>={min_gap*100:.1f}% today required", t, max_pos_used=3)

# ────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print("SECTION D: MAX TRADES PER DAY CAP")
print("Hard limit on new positions per day. After N signals, stop buying.")
print(f"{'='*140}")
for cap in [1, 2, 3, 4, 5, 6, 8, 10]:
    t = backtest(3, strong40_days, max_trades_per_day=cap)
    stats(f"max_pos=3 | Max {cap} new trades/day", t, max_pos_used=3)

# ────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print("SECTION E: BEST COMBOS — max_pos + time filter + gap filter")
print(f"{'='*140}")
combos = [
    (1, 13, None,  None,  "max_pos=1 | all day | no gap filter"),
    (2, 13, None,  None,  "max_pos=2 | all day | no gap filter"),
    (1, 11, None,  None,  "max_pos=1 | buy<11 | no gap filter"),
    (2, 11, None,  None,  "max_pos=2 | buy<11 | no gap filter"),
    (1, 12, 0.005, None,  "max_pos=1 | buy<12 | gap>=0.5%"),
    (2, 12, 0.005, None,  "max_pos=2 | buy<12 | gap>=0.5%"),
    (2, 12, 0.007, None,  "max_pos=2 | buy<12 | gap>=0.7%"),
    (2, 13, 0.005, None,  "max_pos=2 | buy<13 | gap>=0.5%"),
    (3, 11, 0.005, None,  "max_pos=3 | buy<11 | gap>=0.5%"),
    (3, 12, 0.005, None,  "max_pos=3 | buy<12 | gap>=0.5%"),
    (3, 12, 0.007, None,  "max_pos=3 | buy<12 | gap>=0.7%"),
    (2, 12, 0.005, 4,     "max_pos=2 | buy<12 | gap>=0.5% | max4/day"),
    (2, 13, 0.005, 5,     "max_pos=2 | buy<13 | gap>=0.5% | max5/day"),
    (1, 15, 0.010, None,  "max_pos=1 | all day | gap>=1.0%"),
    (2, 15, 0.010, None,  "max_pos=2 | all day | gap>=1.0%"),
]
for mp, hr, gap, cap, label in combos:
    t = backtest(mp, strong40_days, buy_before_hour=hr,
                 stock_min_gap=gap, max_trades_per_day=cap)
    stats(label, t, max_pos_used=mp)

# ────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print(f"MEGA LEADERBOARD — TOP 20 by NET RETURN ({len(results)} configs)")
print(f"{'='*140}")
top = sorted(results, key=lambda x: x["net"], reverse=True)[:20]
for i, rx in enumerate(top, 1):
    crown = " <<<<< KING" if i == 1 else (" ***" if i <= 3 else "")
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | T={rx['t']:4d} | "
          f"Gross={rx['gross']:+6.1f}% | STT={rx['stt']:5.1f}% | NET={rx['net']:+6.1f}%{crown}")

print(f"\n{'='*140}")
print("SWEET SPOT: WR>=55% AND NET>=12% (realistic target after STT)")
print(f"{'='*140}")
sweet = sorted([x for x in results if x["wr"] >= 55 and x["net"] >= 12],
               key=lambda x: x["net"], reverse=True)
for i, rx in enumerate(sweet, 1):
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | T={rx['t']:4d} | NET={rx['net']:+6.1f}%")
if not sweet:
    print("  (Relaxing to WR>=50% AND NET>=8%)")
    sweet2 = sorted([x for x in results if x["wr"] >= 50 and x["net"] >= 8],
                    key=lambda x: x["net"], reverse=True)[:10]
    for i, rx in enumerate(sweet2, 1):
        print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | T={rx['t']:4d} | NET={rx['net']:+6.1f}%")

print(f"\nDONE. {len(results)} configs. STT rate used: {STT_RATE*100:.4f}% of sell value")
