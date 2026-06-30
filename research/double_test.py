"""
DOUBLE TEST:
  Test 1: Baseline re-confirmation (STRONG_GAP_40 + max_pos=3 + FULL EXIT RSI>=72)
  Test 2: RSI LOW EXIT - if RSI drops below X after buying, sell immediately (45-55 sweep)
          Logic: Buy hone ke baad agar RSI < threshold -> market weak hai -> exit fast
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

print("Loading 48 Nifty50 stocks...")
stock_dfs = {}
for i, sym in enumerate(NIFTY_50):
    print(f"  {i+1}/{len(NIFTY_50)}: {sym}    ", end="\r")
    df = fetch(sym)
    if not df.empty: stock_dfs[sym] = df
print(f"\n{len(stock_dfs)} stocks loaded.")

# Build gap data
stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        by_day[d] = {"day_open": float(grp["open"].iloc[0]), "prev_close": prev_close}
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

# STRONG_GAP_40 days
all_dates = set(d for days in stock_day_data.values() for d in days.keys())
strong40_days = set()
for d in all_dates:
    strong = sum(1 for sym, days in stock_day_data.items()
                 if d in days and days[d]["prev_close"]
                 and (days[d]["day_open"] - days[d]["prev_close"]) / days[d]["prev_close"] >= 0.005)
    total  = sum(1 for sym, days in stock_day_data.items()
                 if d in days and days[d]["prev_close"])
    if total > 0 and strong / total >= 0.40:
        strong40_days.add(d)
print(f"STRONG_GAP_40 trading days: {len(strong40_days)}")

all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

def portfolio_backtest(max_pos, allowed_dates,
                       rsi_high_exit=72,      # FULL EXIT when RSI >= this (72 confirmed best)
                       rsi_low_exit=None):    # PANIC EXIT when RSI < this (new test)
    """
    rsi_high_exit: Full exit when RSI overbought (>= threshold) - profit booking
    rsi_low_exit:  Full exit when RSI drops below threshold AFTER buy - panic/momentum cut
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
            rsi = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50

            # RSI HIGH EXIT: overbought → full profit booking
            if rsi_high_exit and not pos.get("rsi_high_done") and rsi >= rsi_high_exit:
                trades.append({"pnl": (close - ep) * qty, "reason": "RSI_HIGH"})
                closed.append(sym); continue

            # RSI LOW EXIT: momentum lost → panic exit
            # Only triggers if RSI goes BELOW threshold AFTER buying
            # (not at entry time - we wait at least 2 candles after buy)
            if rsi_low_exit and pos.get("candles_held", 0) >= 2 and rsi < rsi_low_exit:
                trades.append({"pnl": (close - ep) * qty, "reason": "RSI_LOW"})
                closed.append(sym); continue

            # Normal exits: TSL, target, sell signal, EOD
            ex = None
            if low <= pos["tsl"]:   ex = min(pos["tsl"], float(cc["open"]))
            elif high >= pos["tgt"]: ex = max(pos["tgt"], float(cc["open"]))
            elif ts.hour == 15 and ts.minute >= 15: ex = close
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df): ex = float(df.iloc[idx+1]["open"])
            if ex:
                trades.append({"pnl": (ex - ep) * qty, "reason": "NORMAL"})
                closed.append(sym)
            else:
                sl_pct = abs(ep - pos["sl"]) / ep if ep > 0 else 0
                if high >= ep + ep * sl_pct * tsl_activation_ratio: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high * (1 - trailing_sl_percent))
                    if n > pos["tsl"]: pos["tsl"] = n
                pos["candles_held"] = pos.get("candles_held", 0) + 1

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
                        "rsi_high_done": False,
                        "candles_held": 0,
                    }

    for sym, pos in positions.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        trades.append({"pnl": (lc - pos["ep"]) * pos["qty"], "reason": "EOD"})
    return trades

results = []
def r(label, trades, show_breakdown=False):
    if not trades: print(f"  {label:70s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins/len(pnls)*100; net = sum(pnls); ret = net/CAPITAL*100
    flag = " ***" if ret >= 30 else (" <<<" if ret >= 22 else "")
    if show_breakdown:
        by_reason = {}
        for t in trades:
            rr = t.get("reason","?")
            by_reason[rr] = by_reason.get(rr, 0) + 1
        breakdown = " | ".join(f"{k}={v}" for k,v in sorted(by_reason.items()))
        print(f"  {label:70s} | WR={wr:5.1f}% | Net={net:+10,.0f} | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")
        print(f"  {'':70s}   Exits: {breakdown}")
    else:
        print(f"  {label:70s} | WR={wr:5.1f}% | Net={net:+10,.0f} | Ret={ret:+6.1f}% | T={len(pnls):4d}{flag}")
    results.append({"label": label, "wr": wr, "net": net, "ret": ret, "t": len(pnls)})

MP = 3

print(f"\n{'='*115}")
print("TEST 1: BASELINE RE-CONFIRMATION")
print(f"{'='*115}")
r("BASELINE | STRONG_GAP_40 | max=3 | FULL EXIT RSI>=72 [CONFIRMED BEST]",
  portfolio_backtest(MP, strong40_days, rsi_high_exit=72, rsi_low_exit=None),
  show_breakdown=True)
r("BASELINE | no regime | max=3 | no RSI exit",
  portfolio_backtest(MP, allowed_dates=all_dates, rsi_high_exit=None, rsi_low_exit=None))
r("BASELINE | STRONG_GAP_40 | max=3 | no RSI exit",
  portfolio_backtest(MP, strong40_days, rsi_high_exit=None, rsi_low_exit=None))

print(f"\n{'='*115}")
print("TEST 2: RSI LOW PANIC EXIT (after buy, RSI drops below threshold -> sell fast)")
print("        Combined with FULL EXIT RSI>=72 for profit booking")
print(f"{'='*115}")

for low_thr in range(45, 56):
    trades = portfolio_backtest(MP, strong40_days, rsi_high_exit=72, rsi_low_exit=low_thr)
    r(f"STRONG_GAP_40 | FULL@RSI72 + PANIC@RSI<{low_thr}", trades, show_breakdown=False)

print(f"\n{'='*115}")
print("TEST 2b: PANIC EXIT ONLY (no RSI high exit) - just panic low exit")
print(f"{'='*115}")
for low_thr in range(45, 56):
    trades = portfolio_backtest(MP, strong40_days, rsi_high_exit=None, rsi_low_exit=low_thr)
    r(f"PANIC ONLY | RSI<{low_thr} exit", trades)

print(f"\n{'='*115}")
print("TOP 10 by RETURN")
print(f"{'='*115}")
top = sorted(results, key=lambda x: x["ret"], reverse=True)[:10]
for i, rx in enumerate(top, 1):
    flag = " *** BEST ***" if i == 1 else ""
    print(f"  #{i:2d} | {rx['label']:70s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}%{flag}")

print(f"\nTotal configs: {len(results)} | DONE.")
