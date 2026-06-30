"""
SHORT STRATEGY FIRST BACKTEST
==============================
Using actual SHORT_STREAK_MOMENTUM_BREAKDOWN conditions from registry
(same evaluator as long strategy - proper strictness enforced)

Phase 1: Baseline — how many trades? What's the trade count?
Phase 2: RSI cover sweep — what's the right exit level?
Phase 3: Regime sweep — gap-down only vs all days vs per-stock
Phase 4: Combined long + short
Phase 5: Fine-tuning RSI thresholds (39 -> vary, cover -> vary)
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

CAPITAL      = 100000.0
MARGIN       = 5.0
BUYING_POWER = CAPITAL * MARGIN
SL_PCT       = 0.010
STT_RATE     = 0.000351

config        = load_strategy_sets()
buy_set_def   = next(s for s in config.buy_sets  if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
sell_set_def  = next(s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS")
short_entry   = next(s for s in config.buy_sets  if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN")
short_cover   = next(s for s in config.sell_sets if s.name == "SHORT_STREAK_MOMENTUM_RECOVERY")
evaluator     = StrategySetEvaluator(CONDITION_REGISTRY)
TSL_ACTIVATION = float(cfg("risk", "tsl_activation_ratio", 1.2))
TSL_PCT        = float(cfg("risk", "trailing_sl_percent", 0.002))

stock_dfs = load_cache()
print(f"{len(stock_dfs)} stocks loaded.")

# ── Pre-compute day data ────────────────────────────────────────
stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        by_day[d] = {
            "prev_close": prev_close,
            "day_open":   float(grp["open"].iloc[0]),
            "gap_pct":    (float(grp["open"].iloc[0]) - prev_close) / prev_close if prev_close else 0.0
        }
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

all_dates_set = set(ts.date() for df in stock_dfs.values() for ts in df.index)
all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

def get_regime(gap_dir="up", gap_pct=0.005, frac=0.40):
    days = set()
    for d in all_dates_set:
        stocks = [(sym, stock_day_data[sym][d]) for sym in stock_day_data
                  if d in stock_day_data[sym] and stock_day_data[sym][d]["prev_close"]]
        if not stocks: continue
        cnt = sum(1 for _, sd in stocks
                  if (sd["gap_pct"] >= gap_pct if gap_dir=="up" else sd["gap_pct"] <= -gap_pct))
        if cnt / len(stocks) >= frac: days.add(d)
    return days

long_regime  = get_regime("up",   0.005, 0.40)
short_regime = get_regime("down", 0.005, 0.40)
print(f"Long regime days  (gap-UP 40%):   {len(long_regime)}")
print(f"Short regime days (gap-DOWN 40%): {len(short_regime)}")
print(f"Overlap (both):                   {len(long_regime & short_regime)}")
print(f"Total trading days:               {len(all_dates_set)}")

# ── BACKTEST ENGINE ────────────────────────────────────────────
def backtest(max_pos_long, max_pos_short,
             long_days, short_days,
             rsi_long_exit=72,
             rsi_short_cover=35,
             short_per_stock_gap=None,
             long_per_stock_gap=None):

    per_slot_long  = BUYING_POWER / max(max_pos_long, 1)
    per_slot_short = BUYING_POWER / max(max_pos_short, 1)
    longs = {}; shorts = {}; trades = []

    # Rule 1 tracking: stocks that have completed a full trade today
    # (entry + exit = done for the day, no re-entry)
    completed_today: dict[object, set] = {}  # date → set of symbols

    for ts in timeline:
        d = ts.date()
        if d not in completed_today:
            completed_today[d] = set()
        done_today = completed_today[d]

        closed_l = []; closed_s = []

        # ── Manage LONG positions ──────────────────────────────
        for sym in list(longs.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 4: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos = longs[sym]
            close = float(cc["close"]); high=float(cc["high"]); low=float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]
            rsi0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0

            if rsi0 >= rsi_long_exit:
                stt = close*qty*STT_RATE
                trades.append({"pnl":(close-ep)*qty,"stt":stt,"side":"LONG","reason":"RSI"})
                closed_l.append(sym); done_today.add(sym); continue
            ex = None
            if low <= pos["tsl"]: ex = min(pos["tsl"], float(cc["open"]))
            elif ts.hour==15 and ts.minute>=15: ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell",indicator_df=sliced,pattern_df=sliced,ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df): ex = float(df.iloc[idx+1]["open"])
            if ex:
                stt = ex*qty*STT_RATE
                trades.append({"pnl":(ex-ep)*qty,"stt":stt,"side":"LONG","reason":"NRM"})
                closed_l.append(sym); done_today.add(sym)
            else:
                if high >= ep+abs(ep-pos["sl"])*TSL_ACTIVATION: pos["tsl_on"]=True
                if pos["tsl_on"]:
                    n = round_to_tick(high*(1-TSL_PCT))
                    if n > pos["tsl"]: pos["tsl"]=n

        # ── Manage SHORT positions ─────────────────────────────
        for sym in list(shorts.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 4: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos = shorts[sym]
            close = float(cc["close"]); high=float(cc["high"]); low=float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]
            rsi0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0

            if rsi0 <= rsi_short_cover:
                stt = close*qty*STT_RATE
                trades.append({"pnl":(ep-close)*qty,"stt":stt,"side":"SHORT","reason":"RSI_COVER"})
                closed_s.append(sym); done_today.add(sym); continue

            ex = None
            if high >= pos["tsl"]: ex = max(pos["tsl"], float(cc["open"]))
            elif ts.hour==15 and ts.minute>=15: ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell",indicator_df=sliced,pattern_df=sliced,ws_count=0)
                cond = evaluator._evaluate_conditions(short_cover, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df): ex = float(df.iloc[idx+1]["open"])
            if ex:
                stt = ex*qty*STT_RATE
                trades.append({"pnl":(ep-ex)*qty,"stt":stt,"side":"SHORT","reason":"COVER"})
                closed_s.append(sym); done_today.add(sym)
            else:
                if low <= ep-abs(ep-pos["sl"])*TSL_ACTIVATION: pos["tsl_on"]=True
                if pos["tsl_on"]:
                    n = round_to_tick(low*(1+TSL_PCT))
                    if n < pos["tsl"]: pos["tsl"]=n

        for s in closed_l: del longs[s]
        for s in closed_s: del shorts[s]
        if ts.hour >= 15: continue

        # ── NEW LONG entries ───────────────────────────────────
        if d in long_days and len(longs) < max_pos_long:
            for sym in stock_dfs:
                if len(longs) >= max_pos_long: break
                if sym in longs or sym in shorts: continue
                # Rule 1: Once per day per stock
                if sym in done_today: continue
                if long_per_stock_gap:
                    sd = stock_day_data.get(sym,{}).get(d)
                    if not sd or not sd["prev_close"] or sd["gap_pct"] < long_per_stock_gap: continue
                if ts not in stock_ts_map.get(sym,{}): continue
                df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
                if idx < 4: continue
                sliced = df.iloc[:idx+1]
                ctx = StrategyEvaluationContext(side="buy",indicator_df=sliced,pattern_df=sliced,ws_count=0)
                cond = evaluator._evaluate_conditions(buy_set_def, ctx)
                if not (cond and all(r.get("fired") for r in cond)): continue
                # Rule 2: Skip if exit condition also firing right now
                sell_ctx = StrategyEvaluationContext(side="sell",indicator_df=sliced,pattern_df=sliced,ws_count=0)
                sell_cond = evaluator._evaluate_conditions(sell_set_def, sell_ctx)
                if sell_cond and all(r.get("fired") for r in sell_cond): continue
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"])
                    qty = int(per_slot_long//ep)
                    if qty < 1: continue
                    sl_p = round_to_tick(ep*(1-SL_PCT))
                    longs[sym] = {"ep":ep,"qty":qty,"sl":sl_p,"tsl":sl_p,"tsl_on":False}

        # ── NEW SHORT entries ──────────────────────────────────
        if d in short_days and len(shorts) < max_pos_short:
            for sym in stock_dfs:
                if len(shorts) >= max_pos_short: break
                if sym in shorts or sym in longs: continue
                # Rule 1: Once per day per stock
                if sym in done_today: continue
                if short_per_stock_gap:
                    sd = stock_day_data.get(sym,{}).get(d)
                    if not sd or not sd["prev_close"] or sd["gap_pct"] > -short_per_stock_gap: continue
                if ts not in stock_ts_map.get(sym,{}): continue
                df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
                if idx < 4: continue
                sliced = df.iloc[:idx+1]
                ctx = StrategyEvaluationContext(side="buy",indicator_df=sliced,pattern_df=sliced,ws_count=0)
                cond = evaluator._evaluate_conditions(short_entry, ctx)
                if not (cond and all(r.get("fired") for r in cond)): continue
                # Rule 2: Skip if cover condition also firing simultaneously
                cover_ctx = StrategyEvaluationContext(side="sell",indicator_df=sliced,pattern_df=sliced,ws_count=0)
                cover_cond = evaluator._evaluate_conditions(short_cover, cover_ctx)
                if cover_cond and all(r.get("fired") for r in cover_cond): continue
                if idx+1 < len(df):
                    ep = float(df.iloc[idx+1]["open"])
                    qty = int(per_slot_short//ep)
                    if qty < 1: continue
                    sl_p = round_to_tick(ep*(1+SL_PCT))
                    shorts[sym] = {"ep":ep,"qty":qty,"sl":sl_p,"tsl":sl_p,"tsl_on":False}

    # Force close all open
    for sym, pos in longs.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        stt = lc*pos["qty"]*STT_RATE
        trades.append({"pnl":(lc-pos["ep"])*pos["qty"],"stt":stt,"side":"LONG","reason":"EOD"})
    for sym, pos in shorts.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        stt = lc*pos["qty"]*STT_RATE
        trades.append({"pnl":(pos["ep"]-lc)*pos["qty"],"stt":stt,"side":"SHORT","reason":"EOD"})
    return trades

results = []
def stats(label, trades):
    if not trades:
        print(f"  {label:85s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]; stts=[t["stt"] for t in trades]
    ls = [t for t in trades if t["side"]=="LONG"]
    ss = [t for t in trades if t["side"]=="SHORT"]
    wins = sum(1 for p in pnls if p>0)
    wr   = wins/len(pnls)*100
    gross= sum(pnls); tstt=sum(stts); net=gross-tstt
    g_r=gross/CAPITAL*100; n_r=net/CAPITAL*100; s_r=tstt/CAPITAL*100
    flag = " ***" if n_r>=10 else (" <<<" if n_r>=7 else "")
    print(f"  {label:85s} | WR={wr:5.1f}% | T={len(pnls):4d}(L={len(ls)},S={len(ss)}) | "
          f"Gross={g_r:+6.1f}% | STT={s_r:4.1f}% | NET={n_r:+6.1f}%{flag}")
    results.append({"label":label,"wr":wr,"t":len(pnls),"gross":g_r,"stt":s_r,"net":n_r,
                    "tl":len(ls),"ts":len(ss)})

no_days = set()

# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*145}")
print("PHASE 0: LONG STRATEGY BASELINE (WITH CORRECT RULES)")
print("Checking original Long Strategy performance with Rules 1 & 2 added")
print(f"{'='*145}")

t = backtest(3, 0, long_regime, no_days)
stats(f"LONG only | gap-UP regime | RSI exit>=72", t)

# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*145}")
print("PHASE 1: SHORT STRATEGY BASELINE — Trade count check")
print("Using actual SHORT_STREAK_MOMENTUM_BREAKDOWN from registry (proper strictness)")
print(f"{'='*145}")

for rsi_cov in [28, 30, 32, 35, 38, 40]:
    t = backtest(0, 3, no_days, short_regime, rsi_short_cover=rsi_cov)
    stats(f"SHORT only | gap-DOWN regime | RSI cover<={rsi_cov}", t)

print()
for rsi_cov in [28, 32, 35, 40]:
    t = backtest(0, 3, no_days, all_dates_set, rsi_short_cover=rsi_cov)
    stats(f"SHORT only | ALL days | RSI cover<={rsi_cov}", t)

# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*145}")
print("PHASE 2: PER-STOCK GAP FILTER — Only short stocks that individually gapped DOWN")
print("Strict: each short candidate must have gapped down >= X% today")
print(f"{'='*145}")

for gap_req in [0.003, 0.005, 0.007, 0.010]:
    for rsi_cov in [32, 35, 38]:
        t = backtest(0, 3, no_days, all_dates_set,
                     rsi_short_cover=rsi_cov, short_per_stock_gap=gap_req)
        stats(f"SHORT | all days | stock-gap<=-{gap_req*100:.1f}% | cover<={rsi_cov}", t)

# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*145}")
print("PHASE 3: RSI THRESHOLD FINE-TUNE (entry RSI < X)")
print("Maybe 39 too strict? Or not strict enough? Test 35-45 range")
print("(Note: RSI threshold is baked in condition, so we sweep cover levels broadly)")
print(f"{'='*145}")

# The entry condition streak_rsi_1_below_39 fires at RSI<39
# This might be very strict — let's count how often it fires
print("\n  Counting SHORT_STREAK_MOMENTUM_BREAKDOWN signal frequency...")
signal_count = 0
for ts in timeline:
    d = ts.date()
    if ts.hour >= 15: continue
    for sym in stock_dfs:
        if ts not in stock_ts_map.get(sym, {}): continue
        df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
        if idx < 4: continue
        sliced = df.iloc[:idx+1]
        ctx = StrategyEvaluationContext(side="buy",indicator_df=sliced,pattern_df=sliced,ws_count=0)
        cond = evaluator._evaluate_conditions(short_entry, ctx)
        if cond and all(r.get("fired") for r in cond):
            signal_count += 1
print(f"  Total SHORT signals in 60 days (all days, all stocks): {signal_count}")
print(f"  Avg per day: {signal_count/len(all_dates_set):.1f}")
print(f"  Compare: LONG signals total was ~{426} (from baseline)")

# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*145}")
print("PHASE 4: COMBINED LONG + SHORT (the real goal)")
print("Long on gap-UP days + Short on gap-DOWN days, simultaneously")
print(f"{'='*145}")

for rsi_cov in [28, 32, 35, 38, 40]:
    t = backtest(3, 3, long_regime, short_regime, rsi_short_cover=rsi_cov)
    stats(f"COMBINED | Long(UP regime) + Short(DOWN regime) | cover<={rsi_cov}", t)

print()
# Per-stock filter version
for gap_req in [0.003, 0.005]:
    for rsi_cov in [32, 35, 38]:
        t = backtest(3, 3, long_regime, all_dates_set,
                     rsi_short_cover=rsi_cov, short_per_stock_gap=gap_req)
        stats(f"COMBINED | Long(UP) + Short(all days,stock-gap>={gap_req*100:.1f}%) | cover<={rsi_cov}", t)

# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*145}")
print(f"LEADERBOARD: TOP 15 by NET ({len(results)} configs)")
print(f"{'='*145}")
top = sorted(results, key=lambda x: x["net"], reverse=True)[:15]
for i, rx in enumerate(top, 1):
    crown = " <<<<< KING" if i==1 else (" ***" if i<=3 else "")
    print(f"  #{i:2d} | {rx['label']:85s} | WR={rx['wr']:5.1f}% | T={rx['t']:4d}(L={rx['tl']},S={rx['ts']}) | "
          f"Gross={rx['gross']:+6.1f}% | STT={rx['stt']:4.1f}% | NET={rx['net']:+6.1f}%{crown}")

print(f"\nDONE. {len(results)} configs tested.")
