"""
Strategy 13 Master Experiments
Uses the EXACT same engine as strategy_lab.py / BacktestRunner.
Tests:
  1. Market Regime Filter (only trade on bull market days)
  2. Partial Exit (50% at RSI>72, rest on TSL)
  3. Combined (Regime + Partial Exit)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from screener.morning_screener import NIFTY_50, _fetch_yahoo_history
from core.strategy import (
    CONDITION_REGISTRY, StrategySetEvaluator,
    StrategyEvaluationContext, _build_indicators
)
from core.strategy_sets import load_strategy_sets
from core.order_executor import calculate_stop_loss, calculate_target, round_to_tick
from core.trading_settings import get as cfg
import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

BUY_STRATEGY  = "BUY_STREAK_MOMENTUM_BREAKOUT"
SELL_STRATEGY = "SELL_EMA_MOMENTUM_LOSS"
INITIAL_CAPITAL = 5000.0
USE_MARGIN = True

# ─────────────────────────────────────────────
# Load settings from config (same as BacktestRunner)
# ─────────────────────────────────────────────
stop_loss_percent   = max(0.0001, min(0.20, float(cfg("risk", "stop_loss_percent", 0.01))))
tsl_activation_ratio= max(1.0,   min(2.0,  float(cfg("risk", "tsl_activation_ratio", 1.4))))
trailing_sl_percent = max(0.0001, min(0.20, float(cfg("risk", "trailing_sl_percent", 0.008))))
margin_leverage     = max(1.0,   min(5.0,  float(cfg("risk", "margin_leverage", 2.0)))) if USE_MARGIN else 1.0
position_size_margin= max(0.10,  min(1.0,  float(cfg("risk", "position_size_margin", 1.0))))

config = load_strategy_sets()
buy_set_def  = next(s for s in config.buy_sets  if s.name == BUY_STRATEGY)
sell_set_def = next(s for s in config.sell_sets if s.name == SELL_STRATEGY)
evaluator    = StrategySetEvaluator(CONDITION_REGISTRY)

capital_available = INITIAL_CAPITAL * margin_leverage

# ─────────────────────────────────────────────
# DATA LOADING (same as BacktestRunner._fetch_history)
# ─────────────────────────────────────────────
def fetch_and_prep(symbol):
    try:
        df = _fetch_yahoo_history(symbol, period="60d", interval="5m")
        if df.empty: return pd.DataFrame()
        df.columns = [col.lower() for col in df.columns]
        df.dropna(subset=["close"], inplace=True)
        df["bucket"] = df.index
        df = _build_indicators(df)
        df.dropna(subset=["ema21", "macd", "vwap"], inplace=True)
        return df if len(df) >= 20 else pd.DataFrame()
    except Exception:
        return pd.DataFrame()

# ─────────────────────────────────────────────
# CORE BACKTEST ENGINE (identical to BacktestRunner._process_symbol)
# But returns trade list so we can add extra logic
# ─────────────────────────────────────────────
def run_standard(df, symbol, bull_days=None, partial_exit=False):
    """
    bull_days: if given (set of date objects), only enter trades on those days.
    partial_exit: if True, exit 50% of position when RSI(current bar) > 72, 
                  let remaining run on TSL/SELL signal.
    """
    trades = []
    in_position = False
    entry_time = None
    entry_price = 0.0
    quantity = 0
    initial_sl = 0.0
    target = 0.0
    trailing_sl = 0.0
    tsl_activated = False
    partial_done = False
    partial_qty = 0

    trades_today = 0
    current_date = None

    for i in range(10, len(df)):
        sliced_df = df.iloc[:i+1]
        cc = sliced_df.iloc[-1]
        current_time = sliced_df.index[-1]
        close_price = cc["close"]
        high_price  = cc["high"]
        low_price   = cc["low"]

        candle_date = current_time.date()
        if candle_date != current_date:
            current_date = candle_date
            trades_today = 0

        if not in_position:
            if current_time.hour == 15 and current_time.minute >= 0:
                continue
            if trades_today >= 1:
                continue
            # ── Regime filter ──
            if bull_days is not None and candle_date not in bull_days:
                continue

            ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced_df, pattern_df=sliced_df, ws_count=0)
            cond = evaluator._evaluate_conditions(buy_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                if i + 1 < len(df):
                    entry_price = df.iloc[i+1]["open"]
                    entry_time  = df.index[i+1]
                    in_position = True
                    trades_today += 1
                    quantity = int(capital_available * position_size_margin // entry_price)
                    if quantity == 0:
                        in_position = False
                        trades_today -= 1
                        continue
                    initial_sl  = calculate_stop_loss(entry_price, "BUY")
                    target      = calculate_target(entry_price, initial_sl)
                    trailing_sl = initial_sl
                    tsl_activated = False
                    partial_done  = False
                    partial_qty   = 0
        else:
            # ── Partial exit: RSI > 72, not yet done ──
            if partial_exit and not partial_done and "rsi" in cc and cc["rsi"] > 72 and quantity > 1:
                half = quantity // 2
                trades.append({
                    "stock": symbol, "entry_time": entry_time, "exit_time": current_time,
                    "entry_price": entry_price, "exit_price": close_price,
                    "quantity": half, "pnl": (close_price - entry_price) * half,
                    "exit_reason": "PARTIAL_RSI72"
                })
                quantity    -= half
                partial_done = True
                partial_qty  = half

            exit_reason = None
            exit_price  = 0.0

            if low_price <= trailing_sl:
                exit_reason = "STOPLOSS/TRAILING_SL"
                exit_price  = min(trailing_sl, cc["open"])
            elif high_price >= target:
                exit_reason = "TARGET"
                exit_price  = max(target, cc["open"])
            elif current_time.hour == 15 and current_time.minute >= 15:
                exit_reason = "SQUAREOFF"
                exit_price  = close_price
            else:
                ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced_df, pattern_df=sliced_df, ws_count=0)
                cond = evaluator._evaluate_conditions(sell_set_def, ctx)
                if cond and all(r.get("fired") for r in cond):
                    if i + 1 < len(df):
                        exit_reason  = "SELL_STRATEGY"
                        exit_price   = df.iloc[i+1]["open"]
                        current_time = df.index[i+1]

            if not exit_reason:
                if not tsl_activated:
                    sl_pct = abs(entry_price - initial_sl) / entry_price
                    activation_threshold = entry_price + (entry_price * sl_pct * tsl_activation_ratio)
                    if high_price >= activation_threshold:
                        tsl_activated = True
                if tsl_activated:
                    new_tsl = round_to_tick(high_price * (1 - trailing_sl_percent))
                    if new_tsl > trailing_sl:
                        trailing_sl = new_tsl

            if exit_reason:
                trades.append({
                    "stock": symbol, "entry_time": entry_time, "exit_time": current_time,
                    "entry_price": entry_price, "exit_price": exit_price,
                    "quantity": quantity, "pnl": (exit_price - entry_price) * quantity,
                    "exit_reason": exit_reason
                })
                in_position = False
                entry_time  = None; entry_price = 0.0; quantity = 0
                initial_sl  = 0.0; target = 0.0; trailing_sl = 0.0
                tsl_activated = False; partial_done = False; partial_qty = 0

    return trades

# ─────────────────────────────────────────────
# REGIME COMPUTATION
# ─────────────────────────────────────────────
def compute_bull_days(all_dfs):
    """Days where >= 55% of Nifty 50 stocks closed higher than opened."""
    daily_vote = {}
    for sym, df in all_dfs.items():
        by_day = df.groupby(df.index.date)
        for d, grp in by_day:
            if d not in daily_vote:
                daily_vote[d] = [0, 0]
            daily_vote[d][1] += 1
            if grp["close"].iloc[-1] > grp["open"].iloc[0]:
                daily_vote[d][0] += 1
    return {d for d, (bull, total) in daily_vote.items() if total > 0 and bull / total >= 0.55}

# ─────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────
def report(label, trades):
    if not trades:
        print(f"{label:50s}  NO TRADES")
        return
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    wr   = len(wins) / len(pnls) * 100
    ret  = sum(pnls) / INITIAL_CAPITAL * 100
    n    = len(pnls)
    goal = " <<< GOAL MET!" if wr >= 50 and ret >= 250 else ""
    print(f"{label:50s}  WR={wr:5.2f}%  Ret={ret:8.2f}%  Trades={n}{goal}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print("Loading NIFTY 50 data (same as BacktestRunner)...")
    all_dfs = {}
    for idx, sym in enumerate(NIFTY_50):
        print(f"  {idx+1}/{len(NIFTY_50)}: {sym}", end="\r")
        df = fetch_and_prep(sym)
        if not df.empty:
            all_dfs[sym] = df
    print(f"\n{len(all_dfs)} stocks loaded.\n")

    # ── Compute bull days ──
    bull_days = compute_bull_days(all_dfs)
    print(f"Regime: {len(bull_days)} bull days identified out of ~60 trading days.\n")

    print("=" * 70)
    print("BASELINE (original BUY13 + SELL1)")
    print("=" * 70)
    all_baseline = []
    for sym, df in all_dfs.items():
        all_baseline.extend(run_standard(df, sym))
    report("  BASELINE", all_baseline)

    print()
    print("=" * 70)
    print("EXPERIMENT 1: Market Regime Filter")
    print("=" * 70)
    all_regime = []
    for sym, df in all_dfs.items():
        all_regime.extend(run_standard(df, sym, bull_days=bull_days))
    report("  REGIME ONLY (bull days)", all_regime)

    print()
    print("=" * 70)
    print("EXPERIMENT 2: Partial Exit (50% at RSI>72)")
    print("=" * 70)
    all_partial = []
    for sym, df in all_dfs.items():
        all_partial.extend(run_standard(df, sym, partial_exit=True))
    report("  PARTIAL EXIT only", all_partial)

    print()
    print("=" * 70)
    print("EXPERIMENT 3: REGIME + PARTIAL EXIT (The Combo)")
    print("=" * 70)
    all_combo = []
    for sym, df in all_dfs.items():
        all_combo.extend(run_standard(df, sym, bull_days=bull_days, partial_exit=True))
    report("  REGIME + PARTIAL EXIT", all_combo)

    print()
    print("Done.")

if __name__ == "__main__":
    main()
