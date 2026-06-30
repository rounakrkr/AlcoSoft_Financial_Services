"""
SINGLE DAY REPORT — June 12, 2025
===================================
Run strategy on last trading day only.
Show every trade: entry, exit, profit, reason.

With RSI>=72:    Current strategy
Without RSI>=72: What if we held through the RSI signal?
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import datetime
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
MP           = 3
SL_PCT       = 0.010
STT_RATE     = 0.000351

config       = load_strategy_sets()
buy_set_def  = next(s for s in config.buy_sets  if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
sell_set_def = next(s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS")
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)
TSL_ACTIVATION = float(cfg("risk", "tsl_activation_ratio", 1.2))
TSL_PCT        = float(cfg("risk", "trailing_sl_percent",  0.002))

stock_dfs = load_cache()
print(f"{len(stock_dfs)} stocks loaded.")

all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

# ── Find last trading day ──────────────────────────────────────
all_dates = sorted(set(ts.date() for ts in timeline))
target_date = datetime.date(2025, 6, 12)
if target_date not in all_dates:
    # Find closest available
    available = [d for d in all_dates if d <= target_date]
    target_date = available[-1] if available else all_dates[-1]
print(f"Target date: {target_date}")

# ── Check if STRONG_GAP_40 ─────────────────────────────────────
stock_day_data = {}
for sym, df in stock_dfs.items():
    by_day = {}; prev_close = None
    for d, grp in sorted(df.groupby(df.index.date)):
        by_day[d] = {
            "prev_close": prev_close,
            "day_open": float(grp["open"].iloc[0]),
            "gap_pct": (float(grp["open"].iloc[0]) - prev_close) / prev_close if prev_close else 0.0
        }
        prev_close = float(grp["close"].iloc[-1])
    stock_day_data[sym] = by_day

gap_stocks = []
for sym in stock_dfs:
    sd = stock_day_data.get(sym, {}).get(target_date)
    if sd and sd["prev_close"]:
        gap_stocks.append((sym, sd["gap_pct"], sd["day_open"], sd["prev_close"]))
gap_stocks.sort(key=lambda x: x[1], reverse=True)

strong = sum(1 for _, g, _, _ in gap_stocks if g >= 0.005)
total  = len(gap_stocks)
regime = "STRONG_GAP_40 - TRADE DAY" if total > 0 and strong/total >= 0.40 else "NO TRADE DAY"

print(f"\nGap analysis for {target_date}:")
print(f"  Stocks with gap>=0.5%: {strong}/{total} = {strong/total*100:.1f}%")
print(f"  Regime: {regime}")

if "NO TRADE" in regime:
    print("  Strategy would NOT trade today. Showing anyway for reference.")

# ── Run strategy for target_date only ─────────────────────────
def run_day(use_rsi_exit=True):
    per_slot = BUYING_POWER / MP
    positions = {}
    trades = []
    day_ts = [ts for ts in timeline if ts.date() == target_date]

    for ts in day_ts:
        closed = []
        for sym in list(positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 4: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos = positions[sym]
            close = float(cc["close"]); high = float(cc["high"]); low = float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]

            rsi0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0

            # RSI exit (only if enabled)
            if use_rsi_exit and rsi0 >= 72:
                profit_pct = (close - ep) / ep * 100
                stt = close * qty * STT_RATE
                trades.append({
                    "sym": sym, "entry_time": pos["entry_time"],
                    "entry_price": ep, "exit_time": ts,
                    "exit_price": close, "qty": qty,
                    "pnl": (close - ep) * qty,
                    "profit_pct": profit_pct,
                    "stt": stt,
                    "reason": f"RSI_EXIT (RSI={rsi0:.1f})",
                    "rsi_at_exit": rsi0
                })
                closed.append(sym); continue

            # Normal exits
            ex = None; reason = ""
            if low <= pos["tsl"]:
                ex = min(pos["tsl"], float(cc["open"]))
                reason = "STOP_LOSS"
            elif high >= pos["tgt"]:
                ex = max(pos["tgt"], float(cc["open"]))
                reason = "TARGET_HIT"
            elif ts.hour == 15 and ts.minute >= 15:
                ex = close; reason = "EOD_CLOSE_3:15"
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced,
                                                pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if idx+1 < len(df):
                        ex = float(df.iloc[idx+1]["open"])
                        reason = "SELL_SIGNAL"

            if ex:
                profit_pct = (ex - ep) / ep * 100
                stt = ex * qty * STT_RATE
                trades.append({
                    "sym": sym, "entry_time": pos["entry_time"],
                    "entry_price": ep, "exit_time": ts,
                    "exit_price": ex, "qty": qty,
                    "pnl": (ex - ep) * qty,
                    "profit_pct": profit_pct,
                    "stt": stt,
                    "reason": reason,
                    "rsi_at_exit": rsi0
                })
                closed.append(sym)
            else:
                if high >= ep + abs(ep - pos["sl"]) * TSL_ACTIVATION: pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(high * (1 - TSL_PCT))
                    if n > pos["tsl"]: pos["tsl"] = n

        for s in closed: del positions[s]
        if len(positions) >= MP: continue
        if ts.hour >= 15: continue
        if "NO TRADE" in regime and use_rsi_exit:
            pass  # still show signals even on no-trade day

        for sym in stock_dfs:
            if len(positions) >= MP: break
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
                    qty = int(per_slot // ep)
                    if qty < 1: continue
                    sl_p = round_to_tick(ep * (1 - SL_PCT))
                    positions[sym] = {
                        "ep": ep, "qty": qty, "sl": sl_p,
                        "tgt": round_to_tick(ep + abs(ep - sl_p) * 10.0),
                        "tsl": sl_p, "tsl_on": False,
                        "entry_time": ts,
                    }

    # Force close any open at EOD
    for sym, pos in positions.items():
        df = stock_dfs[sym]
        day_df = df[df.index.date == target_date]
        lc = float(day_df["close"].iloc[-1]) if not day_df.empty else pos["ep"]
        lt = day_df.index[-1] if not day_df.empty else ts
        profit_pct = (lc - pos["ep"]) / pos["ep"] * 100
        stt = lc * pos["qty"] * STT_RATE
        trades.append({
            "sym": sym, "entry_time": pos["entry_time"],
            "entry_price": pos["ep"], "exit_time": lt,
            "exit_price": lc, "qty": pos["qty"],
            "pnl": (lc - pos["ep"]) * pos["qty"],
            "profit_pct": profit_pct,
            "stt": stt,
            "reason": "EOD_FORCE_CLOSE",
            "rsi_at_exit": 0
        })
    return trades

# ── RUN BOTH ──────────────────────────────────────────────────
def print_report(trades, title):
    print(f"\n{'='*110}")
    print(f"  {title}")
    print(f"{'='*110}")
    if not trades:
        print("  No trades executed."); return

    print(f"\n  {'Stock':<14} {'Entry Time':<10} {'Entry Rs':>9} {'Exit Time':<10} {'Exit Rs':>9} "
          f"{'Qty':>5} {'Profit%':>8} {'PnL Rs':>9} {'STT Rs':>7} {'NET Rs':>9} {'Reason'}")
    print(f"  {'-'*105}")

    total_pnl = 0; total_stt = 0; wins = 0
    for t in sorted(trades, key=lambda x: x["entry_time"]):
        net = t["pnl"] - t["stt"]
        total_pnl += t["pnl"]; total_stt += t["stt"]
        win_marker = "+" if t["pnl"] > 0 else "-"
        if t["pnl"] > 0: wins += 1
        et = t["entry_time"].strftime("%H:%M")
        xt = t["exit_time"].strftime("%H:%M")
        print(f"  {t['sym']:<14} {et:<10} {t['entry_price']:>9.2f} {xt:<10} {t['exit_price']:>9.2f} "
              f"{t['qty']:>5} {t['profit_pct']:>7.3f}% {t['pnl']:>9.0f} {t['stt']:>7.0f} {net:>9.0f} "
              f"{win_marker} {t['reason']}")

    total_net = total_pnl - total_stt
    wr = wins / len(trades) * 100 if trades else 0
    print(f"\n  {'TOTAL':<14} {'':>10} {'':>9} {'':>10} {'':>9} {'':>5} {'':>8} "
          f"{total_pnl:>9.0f} {total_stt:>7.0f} {total_net:>9.0f}")
    print(f"\n  Trades: {len(trades)} | Win Rate: {wr:.1f}% | "
          f"Gross P&L: Rs.{total_pnl:,.0f} | STT: Rs.{total_stt:,.0f} | "
          f"NET P&L: Rs.{total_net:,.0f} ({total_net/CAPITAL*100:+.2f}% of capital)")

# ── WITH RSI EXIT ─────────────────────────────────────────────
trades_with    = run_day(use_rsi_exit=True)
trades_without = run_day(use_rsi_exit=False)

print_report(trades_with,    "WITH RSI>=72 EXIT (current strategy)")
print_report(trades_without, "WITHOUT RSI>=72 EXIT (hold until SL/Sell Signal/EOD)")

# ── COMPARISON ────────────────────────────────────────────────
print(f"\n{'='*110}")
print("  COMPARISON SUMMARY")
print(f"{'='*110}")

def summary(trades):
    if not trades: return 0, 0, 0, 0
    pnls = [t["pnl"] for t in trades]
    stts = [t["stt"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    return len(trades), wins/len(trades)*100, sum(pnls), sum(stts)

t1, wr1, g1, s1 = summary(trades_with)
t2, wr2, g2, s2 = summary(trades_without)

print(f"\n  {'Metric':<30} {'With RSI Exit':>18} {'Without RSI Exit':>18}")
print(f"  {'-'*66}")
print(f"  {'Trades':<30} {t1:>18} {t2:>18}")
print(f"  {'Win Rate':<30} {wr1:>17.1f}% {wr2:>17.1f}%")
print(f"  {'Gross P&L':<30} Rs.{g1:>15,.0f} Rs.{g2:>15,.0f}")
print(f"  {'STT Cost':<30} Rs.{s1:>15,.0f} Rs.{s2:>15,.0f}")
print(f"  {'NET P&L':<30} Rs.{g1-s1:>15,.0f} Rs.{g2-s2:>15,.0f}")
print(f"  {'NET Return on Capital':<30} {(g1-s1)/CAPITAL*100:>17.2f}% {(g2-s2)/CAPITAL*100:>17.2f}%")

# ── TOP GAP STOCKS on this day ─────────────────────────────────
print(f"\n{'='*110}")
print(f"  TOP 15 GAP STOCKS on {target_date}")
print(f"{'='*110}")
print(f"\n  {'Stock':<14} {'Prev Close':>12} {'Day Open':>12} {'Gap%':>8}")
for sym, gap_pct, day_open, prev_close in gap_stocks[:15]:
    print(f"  {sym:<14} {prev_close:>12.2f} {day_open:>12.2f} {gap_pct*100:>7.2f}%")
