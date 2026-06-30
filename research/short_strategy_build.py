"""
SHORT SELL DIAGNOSTIC + STRATEGY BUILDER
==========================================
Step 1: Diagnose RSI distribution during downmoves
         - What RSI levels does Nifty50 hit intraday when falling?
         - What is the "RSI 72 equivalent" for shorts?

Step 2: Build SHORT_STREAK_MOMENTUM_BREAKDOWN
         - Symmetric to BUY_STREAK_MOMENTUM_BREAKOUT
         - But independently optimized

Step 3: Sweep all short entry + cover RSI combos
         - Find optimal like we did for longs
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from research.build_cache import load_cache
from core.order_executor import round_to_tick
from core.trading_settings import get as cfg

CAPITAL      = 100000.0
MARGIN       = 5.0
BUYING_POWER = CAPITAL * MARGIN
STT_RATE     = 0.000351
TSL_ACTIVATION = float(cfg("risk", "tsl_activation_ratio", 1.2))
TSL_PCT        = float(cfg("risk", "trailing_sl_percent", 0.002))

stock_dfs = load_cache()
print(f"{len(stock_dfs)} stocks loaded.")

# ── Per-day data ───────────────────────────────────────────────
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

all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

# ── STEP 1: RSI DIAGNOSTIC ON DOWNMOVES ───────────────────────
print(f"\n{'='*100}")
print("STEP 1: RSI DISTRIBUTION DURING INTRADAY DOWNMOVES")
print("Collecting RSI values when stocks are falling (local min RSI per candle-series)")
print(f"{'='*100}")

# Collect: for every 5-min candle where close < open (red candle),
# what was the RSI? This tells us how low RSI goes during selling pressure
all_red_rsi   = []   # RSI at every red candle
down_streak_rsi = [] # RSI at end of 3+ consecutive red candles (strong downmove)
local_min_rsi = []   # Local RSI minima (after a downswing, before recovery)

for sym, df in stock_dfs.items():
    if "rsi" not in df.columns: continue
    for i in range(3, len(df)):
        row  = df.iloc[i]
        rsi  = row.get("rsi", np.nan)
        if pd.isna(rsi): continue

        close = float(row["close"]); opn = float(row["open"])
        if close < opn:
            all_red_rsi.append(float(rsi))

        # 3-candle down streak
        prev1 = df.iloc[i-1]; prev2 = df.iloc[i-2]
        if (close < opn and
            float(prev1["close"]) < float(prev1["open"]) and
            float(prev2["close"]) < float(prev2["open"])):
            down_streak_rsi.append(float(rsi))

        # Local RSI minimum: RSI[i] < RSI[i-1] and RSI[i] < RSI[i+1] if exists
        if i+1 < len(df):
            rsi_next = df.iloc[i+1].get("rsi", np.nan)
            rsi_prev = df.iloc[i-1].get("rsi", np.nan)
            if (not pd.isna(rsi_next) and not pd.isna(rsi_prev)
                    and float(rsi) < float(rsi_prev)
                    and float(rsi) < float(rsi_next)):
                local_min_rsi.append(float(rsi))

def dist(label, vals):
    v = np.array(vals)
    print(f"\n  {label}  (n={len(v)})")
    print(f"  Mean={np.mean(v):.1f} | Median={np.median(v):.1f} | "
          f"Min={np.min(v):.1f} | Max={np.max(v):.1f}")
    for pct in [1, 5, 10, 20, 30]:
        print(f"    Bottom {pct:2d}th percentile: RSI = {np.percentile(v, pct):.1f}")
    print(f"  Distribution:")
    buckets = [(0,20),(20,25),(25,28),(28,30),(30,35),(35,40),(40,45),(45,50),(50,60),(60,100)]
    for lo, hi in buckets:
        cnt = np.sum((v >= lo) & (v < hi))
        bar = "#" * (cnt * 50 // len(v)) if len(v) > 0 else ""
        print(f"    RSI {lo:3d}-{hi:3d}: {cnt:5d} ({cnt/len(v)*100:5.1f}%)  {bar}")

dist("ALL RED CANDLE RSI values", all_red_rsi)
dist("3-CANDLE DOWN STREAK RSI", down_streak_rsi)
dist("LOCAL RSI MINIMA", local_min_rsi)

# How often does RSI go below key levels during a trading day?
print(f"\n  HOW OFTEN DOES RSI HIT THESE LEVELS INTRADAY?")
print(f"  (Per stock-day, what % of days did RSI ever go below X?)")
thresholds = [20, 25, 28, 30, 32, 35, 38, 40, 45]
stock_day_pairs = set()
for sym, df in stock_dfs.items():
    for d in set(df.index.date):
        stock_day_pairs.add((sym, d))
total_sd = len(stock_day_pairs)
for thr in thresholds:
    count = 0
    for sym, df in stock_dfs.items():
        if "rsi" not in df.columns: continue
        for d, grp in df.groupby(df.index.date):
            if grp["rsi"].min() <= thr:
                count += 1
    print(f"  RSI <= {thr}: {count}/{total_sd} stock-days = {count/total_sd*100:.1f}% of time")

# ── STEP 2: BUILD SHORT ENTRY CONDITIONS ──────────────────────
print(f"\n{'='*100}")
print("STEP 2: SHORT ENTRY CONDITIONS")
print("Building SHORT_STREAK_MOMENTUM_BREAKDOWN — mirror of BUY but independently designed")
print(f"{'='*100}")

# Short entry condition functions
def short_cond_A(sliced, sym=None, d=None):
    """RSI < 40 AND below VWAP AND red candle — basic breakdown"""
    if len(sliced) < 2: return False
    cc = sliced.iloc[-1]
    rsi  = float(cc.get("rsi",50)) if pd.notna(cc.get("rsi",np.nan)) else 50.0
    vwap = float(cc.get("vwap",0)) if pd.notna(cc.get("vwap",np.nan)) else 0.0
    close= float(cc["close"]); opn = float(cc["open"])
    return rsi < 40 and close < opn and (vwap > 0 and close < vwap)

def short_cond_B(sliced, sym=None, d=None):
    """RSI falling streak: RSI[0]<RSI[1]<RSI[2] AND below EMA AND below VWAP"""
    if len(sliced) < 4: return False
    rsi0 = float(sliced["rsi"].iloc[-1]) if pd.notna(sliced["rsi"].iloc[-1]) else 50.0
    rsi1 = float(sliced["rsi"].iloc[-2]) if pd.notna(sliced["rsi"].iloc[-2]) else 50.0
    rsi2 = float(sliced["rsi"].iloc[-3]) if pd.notna(sliced["rsi"].iloc[-3]) else 50.0
    cc   = sliced.iloc[-1]
    ema  = float(cc.get("ema21",0)) if pd.notna(cc.get("ema21",np.nan)) else 0.0
    vwap = float(cc.get("vwap",0))  if pd.notna(cc.get("vwap",np.nan)) else 0.0
    close= float(cc["close"])
    rsi_falling_streak = rsi0 < rsi1 < rsi2
    below_ema  = ema > 0 and close < ema
    below_vwap = vwap > 0 and close < vwap
    return rsi_falling_streak and below_ema and below_vwap

def short_cond_C(sliced, sym=None, d=None):
    """RSI < 42 AND 3 consecutive red candles AND below VWAP"""
    if len(sliced) < 4: return False
    rsi0 = float(sliced["rsi"].iloc[-1]) if pd.notna(sliced["rsi"].iloc[-1]) else 50.0
    cc   = sliced.iloc[-1]
    pc1  = sliced.iloc[-2]
    pc2  = sliced.iloc[-3]
    vwap = float(cc.get("vwap",0))  if pd.notna(cc.get("vwap",np.nan)) else 0.0
    def red(r): return float(r["close"]) < float(r["open"])
    three_red  = red(cc) and red(pc1) and red(pc2)
    below_vwap = vwap > 0 and float(cc["close"]) < vwap
    return rsi0 < 42 and three_red and below_vwap

def short_cond_D(sliced, sym=None, d=None):
    """Close < Lowest Low of last 10 candles AND RSI < 45 AND below VWAP (mirror of breakout)"""
    if len(sliced) < 12: return False
    cc    = sliced.iloc[-1]
    close = float(cc["close"])
    low10 = float(sliced["low"].iloc[-11:-1].min())
    rsi0  = float(cc.get("rsi",50)) if pd.notna(cc.get("rsi",np.nan)) else 50.0
    vwap  = float(cc.get("vwap",0)) if pd.notna(cc.get("vwap",np.nan)) else 0.0
    breakdown = close < low10
    below_vwap= vwap > 0 and close < vwap
    return breakdown and rsi0 < 45 and below_vwap

def short_cond_E(sliced, sym=None, d=None):
    """RSI streak falling + Close < Low10 + EMA falling (strongest breakdown signal)"""
    if len(sliced) < 12: return False
    rsi0 = float(sliced["rsi"].iloc[-1]) if pd.notna(sliced["rsi"].iloc[-1]) else 50.0
    rsi1 = float(sliced["rsi"].iloc[-2]) if pd.notna(sliced["rsi"].iloc[-2]) else 50.0
    rsi2 = float(sliced["rsi"].iloc[-3]) if pd.notna(sliced["rsi"].iloc[-3]) else 50.0
    cc   = sliced.iloc[-1]
    ema0 = float(cc.get("ema21",0)) if pd.notna(cc.get("ema21",np.nan)) else 0.0
    ema3 = float(sliced.iloc[-4].get("ema21",0)) if pd.notna(sliced.iloc[-4].get("ema21",np.nan)) else ema0
    vwap = float(cc.get("vwap",0))  if pd.notna(cc.get("vwap",np.nan)) else 0.0
    close= float(cc["close"])
    low10= float(sliced["low"].iloc[-11:-1].min())
    rsi_falling = rsi0 < rsi1 < rsi2
    ema_falling = ema3 > ema0 > 0
    below_vwap  = vwap > 0 and close < vwap
    breakdown   = close < low10
    return rsi_falling and ema_falling and below_vwap and breakdown

SHORT_CONDITIONS = {
    "SC_A: RSI<40+Red+BelowVWAP": short_cond_A,
    "SC_B: RSI_streak+BelowEMA+BelowVWAP": short_cond_B,
    "SC_C: RSI<42+3RedCandles+BelowVWAP": short_cond_C,
    "SC_D: Breakdown10+RSI<45+BelowVWAP": short_cond_D,
    "SC_E: RSI_streak+EMA_fall+Breakdown10": short_cond_E,
}

# ── STEP 3: SWEEP SHORT ENTRY + COVER RSI ────────────────────
print(f"\n{'='*100}")
print("STEP 3: SWEEP ALL SHORT CONDITIONS x COVER RSI THRESHOLDS")
print("Finding: which short entry signal + which cover RSI = best NET return?")
print(f"{'='*100}")

# Regime: which days to allow shorts
all_dates_set = set(ts.date() for df in stock_dfs.values() for ts in df.index)

# gap-DOWN regime
short_regime_days = set()
for d in all_dates_set:
    stocks = [(sym, stock_day_data[sym][d]) for sym in stock_day_data
              if d in stock_day_data[sym] and stock_day_data[sym][d]["prev_close"]]
    if not stocks: continue
    down = sum(1 for _, sd in stocks if sd["gap_pct"] <= -0.005)
    if down / len(stocks) >= 0.40: short_regime_days.add(d)
print(f"Short regime days (gap-DOWN 40%): {len(short_regime_days)}")
print(f"All trading days: {len(all_dates_set)}")

def backtest_short(short_cond_fn, allowed_dates, max_pos=3,
                   rsi_cover=35, sl_pct=0.010):
    per_slot = BUYING_POWER / max_pos
    positions = {}
    trades = []
    for ts in timeline:
        d = ts.date()
        closed = []
        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 4: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos   = positions[sym]
            close = float(cc["close"]); high=float(cc["high"]); low=float(cc["low"])
            ep    = pos["ep"]; qty = pos["qty"]
            rsi0  = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0

            # Cover: RSI oversold
            if rsi0 <= rsi_cover:
                stt = close * qty * STT_RATE
                trades.append({"pnl":(ep-close)*qty, "stt":stt, "reason":"RSI_COVER"})
                closed.append(sym); continue

            ex = None
            if high >= pos["tsl"]:  # SL hit (price went up against short)
                ex = max(pos["tsl"], float(cc["open"]))
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close
            if ex:
                stt = ex * qty * STT_RATE
                trades.append({"pnl":(ep-ex)*qty, "stt":stt, "reason":"EXIT"})
                closed.append(sym)
            else:
                # Trail TSL downward
                if low <= ep - abs(ep - pos["sl"]) * TSL_ACTIVATION: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(low*(1+TSL_PCT))
                    if n < pos["tsl"]: pos["tsl"] = n

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
            if short_cond_fn(sliced, sym=sym, d=d):
                if idx+1 < len(df):
                    ep  = float(df.iloc[idx+1]["open"])
                    qty = int(per_slot // ep)
                    if qty < 1: continue
                    sl_p = round_to_tick(ep*(1+sl_pct))
                    positions[sym] = {
                        "ep":ep, "qty":qty, "sl":sl_p,
                        "tsl":sl_p, "tsl_on":False,
                    }
    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        stt = lc * pos["qty"] * STT_RATE
        trades.append({"pnl":(pos["ep"]-lc)*pos["qty"], "stt":stt, "reason":"EOD"})
    return trades

results = []
def stats(label, trades):
    if not trades:
        print(f"  {label:80s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]; stts = [t["stt"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr   = wins/len(pnls)*100
    gross= sum(pnls); tstt=sum(stts); net=gross-tstt
    g_r=gross/CAPITAL*100; n_r=net/CAPITAL*100; s_r=tstt/CAPITAL*100
    flag = " ***" if n_r >= 8 else (" <<<" if n_r >= 5 else "")
    print(f"  {label:80s} | WR={wr:5.1f}% | T={len(pnls):4d} | "
          f"Gross={g_r:+6.1f}% | STT={s_r:4.1f}% | NET={n_r:+6.1f}%{flag}")
    results.append({"label":label,"wr":wr,"t":len(pnls),"gross":g_r,"stt":s_r,"net":n_r})

# Sweep on SHORT REGIME days
print(f"\n-- On GAP-DOWN regime days only --")
for cname, cfn in SHORT_CONDITIONS.items():
    for rsi_cov in [28, 30, 35, 38, 40, 45]:
        t = backtest_short(cfn, short_regime_days, rsi_cover=rsi_cov)
        stats(f"{cname} | cover<={rsi_cov} | DOWN-regime", t)

# Sweep on ALL days
print(f"\n-- On ALL trading days --")
for cname, cfn in SHORT_CONDITIONS.items():
    for rsi_cov in [28, 35, 40, 45]:
        t = backtest_short(cfn, all_dates_set, rsi_cover=rsi_cov)
        stats(f"{cname} | cover<={rsi_cov} | ALL days", t)

print(f"\n{'='*100}")
print(f"LEADERBOARD: TOP 15 SHORT CONFIGS by NET ({len(results)} configs)")
print(f"{'='*100}")
top = sorted(results, key=lambda x: x["net"], reverse=True)[:15]
for i, rx in enumerate(top, 1):
    crown = " <--- KING" if i==1 else (" ***" if i<=3 else "")
    print(f"  #{i:2d} | {rx['label']:80s} | WR={rx['wr']:5.1f}% | T={rx['t']:4d} | "
          f"Gross={rx['gross']:+6.1f}% | STT={rx['stt']:4.1f}% | NET={rx['net']:+6.1f}%{crown}")

print(f"\nDONE. {len(results)} configs.")
