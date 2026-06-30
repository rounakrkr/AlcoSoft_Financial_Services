"""
SHORT SELLING BACKTEST — BOTH OPTIONS
======================================
Capital: Rs.1L + 5x leverage = Rs.5L buying power
Same 48 stocks, same cache.

OPTION A — MIRROR STRATEGY (Breakdown momentum)
  Short entry conditions (inverse of long):
    RSI falling (RSI[0] < RSI[1])
    Price below EMA21
    Red candle (close < open)
    Price below VWAP
  Short exit: RSI <= rsi_low OR TSL (from above) OR 3:15 PM

OPTION B — GAP FADE (Fade the gap-up reversal)
  Short entry conditions:
    Stock individually gapped UP > gap_min% today
    Current price fell BELOW day open (gap starting to fill)
    RSI > 55 still extended
  Short exit: RSI <= rsi_low OR 0.8% drop from entry OR 3:15 PM

REGIME OPTIONS:
  R1: Per-stock gap-DOWN >= X% required for shorts
  R2: No regime — short anywhere signal fires
  R3: Strong_GAP_40 days — short overextended stocks same day as longs

COMBINED: Run LONGS + SHORTS simultaneously on all days
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

config       = load_strategy_sets()
buy_set_def  = next(s for s in config.buy_sets  if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
sell_set_def = next(s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS")
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)
TSL_ACTIVATION = float(cfg("risk", "tsl_activation_ratio", 1.2))
TSL_PCT        = float(cfg("risk", "trailing_sl_percent", 0.002))

stock_dfs = load_cache()
print(f"{len(stock_dfs)} stocks loaded.")

# ── Pre-compute per-stock per-day gap data ─────────────────────
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

all_dates_set = set(d for days in stock_day_data.values() for d in days.keys())

# Pre-compute regime days
def get_regime_days(gap_dir="up", gap_pct=0.005, frac=0.40):
    """Days where frac% of stocks gapped in direction by gap_pct"""
    days = set()
    for d in all_dates_set:
        stocks_with_data = [(sym, stock_day_data[sym][d])
                            for sym in stock_day_data if d in stock_day_data[sym]
                            and stock_day_data[sym][d]["prev_close"]]
        if not stocks_with_data: continue
        if gap_dir == "up":
            strong = sum(1 for _, sd in stocks_with_data if sd["gap_pct"] >= gap_pct)
        else:
            strong = sum(1 for _, sd in stocks_with_data if sd["gap_pct"] <= -gap_pct)
        if strong / len(stocks_with_data) >= frac:
            days.add(d)
    return days

strong_up_days   = get_regime_days("up",   0.005, 0.40)  # current long regime
strong_down_days = get_regime_days("down", 0.005, 0.40)  # mirror short regime
print(f"Long regime days (gap-UP 40%):   {len(strong_up_days)}")
print(f"Short regime days (gap-DOWN 40%): {len(strong_down_days)}")
overlap = strong_up_days & strong_down_days
print(f"Overlap (both same day):          {len(overlap)}")
all_days = set(ts.date() for df in stock_dfs.values() for ts in df.index)
print(f"Total trading days in cache:      {len(all_days)}")

all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

# ── INDICATOR HELPERS ─────────────────────────────────────────
def get_rsi(sliced, lag=0):
    col = sliced["rsi"]
    idx = -(lag+1)
    if len(col) >= abs(idx) and pd.notna(col.iloc[idx]):
        return float(col.iloc[idx])
    return 50.0

def get_ema(sliced, lag=0):
    col = sliced["ema21"]
    idx = -(lag+1)
    if len(col) >= abs(idx) and pd.notna(col.iloc[idx]):
        return float(col.iloc[idx])
    return 0.0

def get_vwap(sliced):
    if "vwap" in sliced.columns and pd.notna(sliced["vwap"].iloc[-1]):
        return float(sliced["vwap"].iloc[-1])
    return 0.0

# ── OPTION A: MIRROR BREAKDOWN SHORT ENTRY ────────────────────
def short_signal_mirror(sliced):
    """Inverse of long momentum: RSI falling, below EMA, red candle, below VWAP"""
    if len(sliced) < 3: return False
    rsi0  = get_rsi(sliced, 0)
    rsi1  = get_rsi(sliced, 1)
    ema   = get_ema(sliced, 0)
    vwap  = get_vwap(sliced)
    close = float(sliced["close"].iloc[-1])
    opn   = float(sliced["open"].iloc[-1])
    rsi_falling   = rsi0 < rsi1          # momentum dropping
    below_ema     = ema > 0 and close < ema
    red_candle    = close < opn
    below_vwap    = vwap > 0 and close < vwap
    return rsi_falling and below_ema and red_candle and below_vwap

# ── OPTION B: GAP FADE SHORT ENTRY ───────────────────────────
def short_signal_gap_fade(sliced, sym, d, gap_min=0.005):
    """Stock gapped UP but now falling below day open → fade the gap"""
    if len(sliced) < 3: return False
    sd = stock_day_data.get(sym, {}).get(d)
    if not sd or not sd["prev_close"]: return False
    if sd["gap_pct"] < gap_min: return False   # must have gapped UP
    day_open = sd["day_open"]
    close = float(sliced["close"].iloc[-1])
    rsi0  = get_rsi(sliced, 0)
    below_open     = close < day_open        # price fell back below day open
    still_extended = rsi0 > 52              # not yet oversold
    red_candle     = close < float(sliced["open"].iloc[-1])
    return below_open and still_extended and red_candle

# ── MAIN BACKTEST ──────────────────────────────────────────────
def backtest(max_pos_long, max_pos_short,
             long_allowed_dates,   # which days to allow longs
             short_allowed_dates,  # which days to allow shorts
             short_mode="A",       # "A"=mirror, "B"=gap_fade, "BOTH"
             rsi_cover=28,         # short exit RSI level
             short_gap_min=0.005,  # for gap_fade: min gap-up to fade
             short_sl_pct=0.010):  # short stop loss (above entry)

    per_slot_long  = BUYING_POWER / max(max_pos_long, 1)
    per_slot_short = BUYING_POWER / max(max_pos_short, 1)

    long_positions  = {}  # sym -> pos dict
    short_positions = {}  # sym -> pos dict
    trades = []

    for ts in timeline:
        d = ts.date()
        closed_long  = []
        closed_short = []

        # ── Manage LONG positions ────────────────────────────
        for sym in list(long_positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 4: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos  = long_positions[sym]
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep   = pos["ep"]; qty = pos["qty"]
            rsi0 = get_rsi(sliced, 0)

            if rsi0 >= 72:
                stt = close * qty * STT_RATE
                trades.append({"pnl": (close-ep)*qty, "stt": stt, "side": "LONG", "reason": "RSI"})
                closed_long.append(sym); continue

            ex = None
            if low <= pos["tsl"]:
                ex = min(pos["tsl"], float(cc["open"]))
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df): ex = float(df.iloc[idx+1]["open"])
            if ex:
                stt = ex * qty * STT_RATE
                trades.append({"pnl": (ex-ep)*qty, "stt": stt, "side": "LONG", "reason": "NRM"})
                closed_long.append(sym)
            else:
                if high >= ep + abs(ep-pos["sl"]) * TSL_ACTIVATION: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high*(1-TSL_PCT))
                    if n > pos["tsl"]: pos["tsl"] = n

        # ── Manage SHORT positions ───────────────────────────
        for sym in list(short_positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 4: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos   = short_positions[sym]
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep    = pos["ep"]; qty = pos["qty"]
            rsi0  = get_rsi(sliced, 0)

            # SHORT P&L = (entry - exit) * qty (profit when price falls)
            # RSI cover: RSI <= rsi_cover (oversold → cover)
            if rsi0 <= rsi_cover:
                stt = close * qty * STT_RATE
                trades.append({"pnl": (ep-close)*qty, "stt": stt, "side": "SHORT", "reason": "RSI_COVER"})
                closed_short.append(sym); continue

            ex = None
            # Short TSL: trails from LOW downward, stops out if price goes UP past tsl
            if high >= pos["tsl"]:     # stop loss hit (price went up against us)
                ex = max(pos["tsl"], float(cc["open"]))
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close
            if ex:
                stt = ex * qty * STT_RATE
                trades.append({"pnl": (ep-ex)*qty, "stt": stt, "side": "SHORT", "reason": "NRM"})
                closed_short.append(sym)
            else:
                # Trail TSL downward as price falls
                if low <= ep - abs(ep-pos["sl"]) * TSL_ACTIVATION: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(low*(1+TSL_PCT))
                    if n < pos["tsl"]: pos["tsl"] = n

        for s in closed_long:  del long_positions[s]
        for s in closed_short: del short_positions[s]

        if ts.hour >= 15: continue

        # ── NEW LONG entries ─────────────────────────────────
        if d in long_allowed_dates and len(long_positions) < max_pos_long:
            for sym in stock_dfs:
                if len(long_positions) >= max_pos_long: break
                if sym in long_positions or sym in short_positions: continue
                if ts not in stock_ts_map.get(sym, {}): continue
                df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
                if idx < 4: continue
                sliced = df.iloc[:idx+1]
                ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(buy_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df):
                        ep  = float(df.iloc[idx+1]["open"])
                        qty = int(per_slot_long // ep)
                        if qty < 1: continue
                        sl_p = round_to_tick(ep*(1-SL_PCT))
                        long_positions[sym] = {
                            "ep": ep, "qty": qty, "sl": sl_p,
                            "tsl": sl_p, "tsl_on": False,
                        }

        # ── NEW SHORT entries ────────────────────────────────
        if d in short_allowed_dates and len(short_positions) < max_pos_short:
            for sym in stock_dfs:
                if len(short_positions) >= max_pos_short: break
                if sym in short_positions or sym in long_positions: continue
                if ts not in stock_ts_map.get(sym, {}): continue
                df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
                if idx < 4: continue
                sliced = df.iloc[:idx+1]

                fired = False
                if short_mode in ("A", "BOTH"):
                    fired = short_signal_mirror(sliced)
                if not fired and short_mode in ("B", "BOTH"):
                    fired = short_signal_gap_fade(sliced, sym, d, short_gap_min)

                if fired:
                    if idx+1 < len(df):
                        ep  = float(df.iloc[idx+1]["open"])
                        qty = int(per_slot_short // ep)
                        if qty < 1: continue
                        sl_p = round_to_tick(ep*(1+short_sl_pct))  # SL ABOVE entry for shorts
                        short_positions[sym] = {
                            "ep": ep, "qty": qty, "sl": sl_p,
                            "tsl": sl_p, "tsl_on": False,
                        }

    # Force close all open positions
    for sym, pos in long_positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        stt = lc * pos["qty"] * STT_RATE
        trades.append({"pnl": (lc-pos["ep"])*pos["qty"], "stt": stt, "side": "LONG", "reason": "EOD"})
    for sym, pos in short_positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        stt = lc * pos["qty"] * STT_RATE
        trades.append({"pnl": (pos["ep"]-lc)*pos["qty"], "stt": stt, "side": "SHORT", "reason": "EOD"})
    return trades

results = []
def stats(label, trades):
    if not trades:
        print(f"  {label:80s} | NO TRADES"); return
    pnls  = [t["pnl"] for t in trades]
    stts  = [t["stt"] for t in trades]
    longs = [t for t in trades if t["side"] == "LONG"]
    shorts= [t for t in trades if t["side"] == "SHORT"]
    wins  = sum(1 for p in pnls if p > 0)
    wr    = wins/len(pnls)*100
    gross = sum(pnls)
    tstt  = sum(stts)
    net   = gross - tstt
    g_r   = gross/CAPITAL*100
    n_r   = net/CAPITAL*100
    s_r   = tstt/CAPITAL*100
    flag  = " ***" if n_r >= 15 else (" <<<" if n_r >= 10 else "")
    print(f"  {label:80s} | WR={wr:5.1f}% | T={len(pnls):4d}(L={len(longs)},S={len(shorts)}) | "
          f"Gross={g_r:+6.1f}% | STT={s_r:4.1f}% | NET={n_r:+6.1f}%{flag}")
    results.append({"label":label,"wr":wr,"t":len(pnls),"gross":g_r,"stt":s_r,"net":n_r})

all_days_set = set(ts.date() for df in stock_dfs.values() for ts in df.index)

# ────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print("SECTION 0: BASELINES")
print(f"{'='*140}")
no_days = set()
t = backtest(3, 0, strong_up_days, no_days)
stats("BASELINE: Long only | Strong_UP_40 regime | max_pos=3", t)

# ────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print("SECTION 1: OPTION A — MIRROR SHORT (Breakdown momentum)")
print("Short entry: RSI falling + below EMA + red candle + below VWAP")
print(f"{'='*140}")

# A1: Short only on gap-DOWN days
for rsi_cov in [25, 28, 30, 35]:
    t = backtest(0, 3, no_days, strong_down_days,
                 short_mode="A", rsi_cover=rsi_cov)
    stats(f"Option A | SHORT only | gap-DOWN regime | RSI cover<={rsi_cov}", t)

# A2: Short on ALL days
print()
for rsi_cov in [25, 28, 30, 35]:
    t = backtest(0, 3, no_days, all_days_set,
                 short_mode="A", rsi_cover=rsi_cov)
    stats(f"Option A | SHORT only | ALL days | RSI cover<={rsi_cov}", t)

# ────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print("SECTION 2: OPTION B — GAP FADE (Short stocks that gapped UP but reversing)")
print("Short entry: Stock gapped UP > X% but now below day open + RSI > 52")
print(f"{'='*140}")

# B1: Gap fade on gap-UP days (same days as longs!)
for gap_min in [0.003, 0.005, 0.008, 0.010]:
    for rsi_cov in [28, 35]:
        t = backtest(0, 3, no_days, strong_up_days,
                     short_mode="B", rsi_cover=rsi_cov, short_gap_min=gap_min)
        stats(f"Option B | SHORT only | UP days | gap_fade>={gap_min*100:.1f}% | cover<={rsi_cov}", t)

# B2: Gap fade on ALL days
print()
for gap_min in [0.005, 0.010]:
    for rsi_cov in [28, 35]:
        t = backtest(0, 3, no_days, all_days_set,
                     short_mode="B", rsi_cover=rsi_cov, short_gap_min=gap_min)
        stats(f"Option B | SHORT only | ALL days | gap_fade>={gap_min*100:.1f}% | cover<={rsi_cov}", t)

# ────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print("SECTION 3: COMBINED — LONG (up days) + SHORT simultaneously")
print(f"{'='*140}")

# Combined A: Long on up days + Short mirror on down days
for rsi_cov in [28, 35]:
    t = backtest(3, 3, strong_up_days, strong_down_days,
                 short_mode="A", rsi_cover=rsi_cov)
    stats(f"COMBINED A | Long(UP days) + Short mirror(DOWN days) | cover<={rsi_cov}", t)

# Combined B: Long on up days + Gap fade shorts on same up days
print()
for gap_min in [0.005, 0.010]:
    for rsi_cov in [28, 35]:
        t = backtest(3, 2, strong_up_days, strong_up_days,
                     short_mode="B", rsi_cover=rsi_cov, short_gap_min=gap_min)
        stats(f"COMBINED B | Long + Gap-fade short(same UP days) | gap>={gap_min*100:.1f}% | cover<={rsi_cov}", t)

# Combined BOTH: Long up days + Short mirror ALL days
print()
for rsi_cov in [28, 35]:
    t = backtest(3, 3, strong_up_days, all_days_set,
                 short_mode="BOTH", rsi_cover=rsi_cov)
    stats(f"COMBINED BOTH | Long(UP) + Short(ALL days, A+B signals) | cover<={rsi_cov}", t)

# ────────────────────────────────────────────────────────────────
print(f"\n{'='*140}")
print(f"MEGA LEADERBOARD — TOP 15 by NET ({len(results)} configs)")
print(f"{'='*140}")
top = sorted(results, key=lambda x: x["net"], reverse=True)[:15]
for i, rx in enumerate(top, 1):
    crown = " <<<<< KING" if i==1 else (" ***" if i<=3 else "")
    print(f"  #{i:2d} | {rx['label']:80s} | WR={rx['wr']:5.1f}% | T={rx['t']:4d} | "
          f"Gross={rx['gross']:+6.1f}% | STT={rx['stt']:4.1f}% | NET={rx['net']:+6.1f}%{crown}")

print(f"\nDONE. {len(results)} configs.")
