"""
MEGA OPTIMIZATION SWEEP
========================
Problem: WR=59% but Return=31.9% â†’ many "penny profit" trades wasting STT budget
Goal: FEWER but BIGGER trades â†’ Maximize Return per Trade

Tests:
  A. PROFIT % EXIT â€” if stock gains X% from entry, exit immediately
     Range: 0.5% to 2.5% (step 0.25)
     Combined with RSI>=72 (whichever hits first)
     â†’ Cuts trades that "drag on" with small gains â†’ fewer trades, bigger average

  B. SL % VARIATION â€” tighter or wider stop loss
     Range: 0.4% to 1.5% (step 0.1)
     â†’ Tighter SL: fewer losses but more SL hits; wider SL: fewer hits but bigger losses

  C. PROFIT % + SL COMBINED â€” find best combo
     Top 5 profit% Ã— top 5 SL% combinations

  D. MORNING SCREENER INTEGRATION â€” filter to top N stocks by gap strength
     Only trade stocks with gap > X% (pre-market filter)
     Range: top 10, 15, 20, 30 stocks by gap, or gap>1%, >1.5%
     â†’ Fewer candidates = fewer trades = less STT, hopefully better quality

All tests on STRONG_GAP_40 + max_pos=3 + RSI(0)>=72 base
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
MP = 3  # max positions

config       = load_strategy_sets()
buy_set_def  = next(s for s in config.buy_sets  if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
sell_set_def = next(s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS")
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)
TSL_ACTIVATION = float(cfg("risk", "tsl_activation_ratio", 1.2))
TSL_PCT        = float(cfg("risk", "trailing_sl_percent",  0.002))

# â”€â”€ Data Loading â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

print("Loading 48 stocks...")
stock_dfs = {}
for i, sym in enumerate(NIFTY_50):
    print(f"  {i+1}/{len(NIFTY_50)}: {sym}    ", end="\r")
    df = fetch(sym)
    if not df.empty: stock_dfs[sym] = df
print(f"\n{len(stock_dfs)} stocks loaded.")

# â”€â”€ Gap / Regime Data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

# â”€â”€ Core Backtest â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def backtest(max_pos, allowed_dates,
             sl_pct=0.010,       # stop loss % below entry
             rsi_hi=72,          # RSI(0) overbought exit
             profit_pct=None,    # fixed profit% exit (None = disabled)
             screener_filter=None # set of (date, symbol) pairs allowed to buy
             ):
    per_slot = BUYING_POWER / max_pos
    positions = {}; trades = []

    def calc_sl(ep, pct): return round_to_tick(ep * (1 - pct))
    def calc_tgt(ep, sl): return round_to_tick(ep + abs(ep - sl) * 10.0)  # effectively no fixed target

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

            # RSI high exit
            rsi = cc.get("rsi", np.nan)
            rsi = float(rsi) if pd.notna(rsi) else 50.0
            if rsi >= rsi_hi:
                trades.append({"pnl": (close - ep) * qty, "reason": "RSI"})
                closed.append(sym); continue

            # Profit % exit
            if profit_pct and (close - ep) / ep >= profit_pct:
                trades.append({"pnl": (close - ep) * qty, "reason": "PROFIT_PCT"})
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
            # Morning screener filter
            if screener_filter and (d, sym) not in screener_filter: continue
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
                    sl_p = calc_sl(ep, sl_pct)
                    positions[sym] = {
                        "ep": ep, "qty": qty, "sl": sl_p,
                        "tgt": calc_tgt(ep, sl_p),
                        "tsl": sl_p, "tsl_on": False,
                    }

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "reason": "EOD"})
    return trades

# â”€â”€ Stats â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
results = []
def stats(label, trades):
    if not trades: print(f"  {label:75s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    avg_win  = np.mean([p for p in pnls if p > 0]) if wins else 0
    avg_loss = np.mean([p for p in pnls if p <= 0]) if (len(pnls)-wins) else 0
    ppt = net / len(pnls)  # profit per trade
    flag = " ***" if (ret >= 33 and wr >= 55) else (" <<<" if ret >= 35 else "")
    print(f"  {label:75s} | WR={wr:5.1f}% | Ret={ret:+6.1f}% | T={len(pnls):4d} | AvgWin={avg_win:+7.0f} | AvgLoss={avg_loss:+7.0f} | PnL/T={ppt:+6.0f}{flag}")
    results.append({"label": label, "wr": wr, "net": net, "ret": ret,
                    "t": len(pnls), "ppt": ppt, "avg_win": avg_win})

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
print(f"\n{'='*130}")
print("BASELINE")
print(f"{'='*130}")
stats("BASELINE | SL=1.0% | RSI>=72 | No profit% exit",
      backtest(MP, strong40_days, sl_pct=0.010, profit_pct=None))

# â”€â”€ SECTION A: PROFIT % EXIT â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
print(f"\n{'='*130}")
print("SECTION A: FIXED PROFIT % EXIT (combined with RSI>=72, whichever first)")
print("Goal: Cut 'small winner dragging on' trades early â†’ reduce STT waste")
print(f"{'='*130}")
for pct in [0.005, 0.0075, 0.010, 0.0125, 0.015, 0.0175, 0.020, 0.025, 0.030]:
    t = backtest(MP, strong40_days, sl_pct=0.010, profit_pct=pct)
    stats(f"PROFIT_EXIT >= {pct*100:.2f}% | SL=1.0%", t)

# â”€â”€ SECTION B: SL VARIATION â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
print(f"\n{'='*130}")
print("SECTION B: STOP LOSS % VARIATION | No profit% exit")
print(f"{'='*130}")
for sl in [0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014, 0.015]:
    t = backtest(MP, strong40_days, sl_pct=sl, profit_pct=None)
    stats(f"SL={sl*100:.1f}% | RSI>=72 | No profit% exit", t)

# â”€â”€ SECTION C: SL + PROFIT % COMBO â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
print(f"\n{'='*130}")
print("SECTION C: SL + PROFIT % COMBINED (best combos)")
print(f"{'='*130}")
for sl in [0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012]:
    for pct in [0.008, 0.010, 0.012, 0.015, 0.018, 0.020, 0.025]:
        t = backtest(MP, strong40_days, sl_pct=sl, profit_pct=pct)
        stats(f"SL={sl*100:.1f}% + PROFIT>={pct*100:.2f}%", t)

# â”€â”€ SECTION D: MORNING SCREENER FILTER â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
print(f"\n{'='*130}")
print("SECTION D: MORNING SCREENER FILTER (only trade top N gap stocks per day)")
print("Goal: Reduce total stocks scanned -> fewer trades -> less STT")
print(f"{'='*130}")

def build_screener(min_gap_pct=None, top_n=None):
    """Returns set of (date, symbol) pairs allowed to trade."""
    allowed = set()
    for d in strong40_days:
        # get all stocks with their gap on this day
        day_stocks = [(sym, stock_day_data[sym][d]["gap_pct"])
                      for sym in stock_dfs
                      if d in stock_day_data[sym] and stock_day_data[sym][d]["prev_close"]]
        day_stocks.sort(key=lambda x: x[1], reverse=True)  # sort by gap desc
        if top_n:
            eligible = [sym for sym, _ in day_stocks[:top_n]]
        elif min_gap_pct:
            eligible = [sym for sym, g in day_stocks if g >= min_gap_pct]
        else:
            eligible = [sym for sym, _ in day_stocks]
        for sym in eligible:
            allowed.add((d, sym))
    return allowed

# By top N stocks (highest gap)
for n in [5, 8, 10, 12, 15, 20, 25, 30]:
    filt = build_screener(top_n=n)
    t = backtest(MP, strong40_days, sl_pct=0.010, profit_pct=None,
                 screener_filter=filt)
    stats(f"SCREENER top-{n:2d} gap stocks | SL=1.0% | RSI>=72", t)

# By minimum gap %
for min_g in [0.005, 0.007, 0.010, 0.012, 0.015, 0.020]:
    filt = build_screener(min_gap_pct=min_g)
    t = backtest(MP, strong40_days, sl_pct=0.010, profit_pct=None,
                 screener_filter=filt)
    stats(f"SCREENER gap>={min_g*100:.1f}% | SL=1.0% | RSI>=72", t)

# Best screener + profit% combo
print(f"\n{'='*130}")
print("SECTION D2: SCREENER + PROFIT % COMBO")
print(f"{'='*130}")
for n in [10, 15, 20]:
    filt = build_screener(top_n=n)
    for pct in [0.010, 0.015, 0.020]:
        t = backtest(MP, strong40_days, sl_pct=0.010, profit_pct=pct,
                     screener_filter=filt)
        stats(f"SCREENER top-{n} + PROFIT>={pct*100:.2f}% | SL=1.0%", t)

# â”€â”€ LEADERBOARD â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
print(f"\n{'='*130}")
print(f"LEADERBOARD â€” TOP 20 by RETURN ({len(results)} configs)")
print(f"{'='*130}")
top_ret = sorted(results, key=lambda x: x["ret"], reverse=True)[:20]
for i, rx in enumerate(top_ret, 1):
    crown = " <<<<< KING" if i == 1 else (" ***" if i<=3 else "")
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d} | PnL/T={rx['ppt']:+6.0f}{crown}")

print(f"\n{'='*130}")
print("TOP 15 by PROFIT PER TRADE (fewer, bigger trades = less STT impact)")
print(f"{'='*130}")
top_ppt = sorted([x for x in results if x["ret"] > 15], key=lambda x: x["ppt"], reverse=True)[:15]
for i, rx in enumerate(top_ppt, 1):
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d} | PnL/T={rx['ppt']:+6.0f}")

print(f"\n{'='*130}")
print("SWEET SPOT: WR>=55% AND Return>=28% AND Trades<=350 (quality + efficiency)")
print(f"{'='*130}")
sweet = sorted([x for x in results if x["wr"]>=55 and x["ret"]>=28 and x["t"]<=350],
               key=lambda x: x["ret"], reverse=True)
for i, rx in enumerate(sweet, 1):
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d} | PnL/T={rx['ppt']:+6.0f}")
if not sweet:
    # Relax criteria
    sweet2 = sorted([x for x in results if x["wr"]>=53 and x["ret"]>=25],
                    key=lambda x: x["ppt"], reverse=True)[:10]
    print("  (None found, relaxing WR>=53% AND Ret>=25%:)")
    for i, rx in enumerate(sweet2, 1):
        print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d} | PnL/T={rx['ppt']:+6.0f}")

print(f"\n{'='*130}")
print(f"DONE. Total configs: {len(results)}")

