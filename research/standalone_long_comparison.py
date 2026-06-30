import sys, os, time
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.build_cache import load_cache
from core.strategy import (StrategyEvaluationContext, StrategySetEvaluator, CONDITION_REGISTRY)
from core.strategy_sets import load_strategy_sets
import ta

CAPITAL = 100000.0
BUYING_POWER = CAPITAL * 5.0
SL_PCT = 0.010
PROFIT_TARGET = 0.005
PARTIAL_FRAC = 0.75
STT_PCT = 0.00035

def main():
    stock_dfs = load_cache()
    timeline = sorted(list(set(ts for df in stock_dfs.values() for ts in df.index)))
    stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
    
    mega_df = pd.concat([df.assign(symbol=sym, date=df.index.date) for sym, df in stock_dfs.items()])
    first_candles = mega_df.groupby(["date", "symbol"]).first().reset_index()
    
    all_daily_gaps = {}
    dates = sorted(first_candles["date"].unique())
    for i in range(1, len(dates)):
        prev_d, curr_d = dates[i-1], dates[i]
        prev_day = mega_df[mega_df["date"] == prev_d]
        curr_day = first_candles[first_candles["date"] == curr_d]
        if prev_day.empty or curr_day.empty: continue
        
        last_closes = prev_day.groupby("symbol").last()["close"]
        first_opens = curr_day.set_index("symbol")["open"]
        merged = pd.concat([last_closes, first_opens], axis=1, join="inner")
        gaps = (merged["open"] - merged["close"]) / merged["close"]
        for sym, gap_val in gaps.items(): all_daily_gaps[(curr_d, sym)] = float(gap_val)

    # Calculate Bull Days: >= 0.5% gap up, >= 40% breadth (As in live_config_test)
    bull_days = set()
    for i in range(1, len(dates)):
        curr_d = dates[i]
        daily_gaps_vals = [g for (d, s), g in all_daily_gaps.items() if d == curr_d]
        if not daily_gaps_vals: continue
        if sum(1 for g in daily_gaps_vals if g >= 0.005) / len(daily_gaps_vals) >= 0.40:
            bull_days.add(curr_d)

    for sym, df in stock_dfs.items():
        df["rsi_13"] = ta.momentum.rsi(df["close"], window=13).fillna(50.0)
        df["rsi_14"] = ta.momentum.rsi(df["close"], window=14).fillna(50.0)

    config = load_strategy_sets()
    long_set = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
    long_exit_set = next((s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

    pre_le = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}
    pre_lx = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}
    
    for sym, df in stock_dfs.items():
        for idx in range(1, len(df)):
            sliced = df.iloc[:idx+1]
            c_le = evaluator._evaluate_conditions(long_set, StrategyEvaluationContext("buy", sliced, sliced, len(sliced)))
            if c_le and all(r.get("fired") for r in c_le): pre_le[sym][idx] = True
            c_lx = evaluator._evaluate_conditions(long_exit_set, StrategyEvaluationContext("sell", sliced, sliced, 0))
            if c_lx and all(r.get("fired") for r in c_lx): pre_lx[sym][idx] = True

    results = []
    
    for test_mp in [3]:
        per_slot = BUYING_POWER / test_mp
        
        for rsi_mode in ["14_LAG0_72", "13_LAG1_85"]:
            positions = {}; trades = []
            
            for ts in timeline:
                current_date = ts.date()
                
                # Manage Open Positions
                symbols_to_close = []
                for sym, pos in positions.items():
                    if ts not in stock_ts_map.get(sym, {}): continue
                    idx = stock_ts_map[sym][ts]; df = stock_dfs[sym]
                    cc = df.iloc[idx]
                    close_price, low_price, open_price = float(cc["close"]), float(cc["low"]), float(cc["open"])
                    
                    if not pos.get("partial_done", False):
                        profit_now = (close_price - pos["ep"]) / pos["ep"]
                        if profit_now >= PROFIT_TARGET:
                            sell_qty = max(1, int(pos["qty"] * PARTIAL_FRAC))
                            if sell_qty >= pos["qty"]: sell_qty = max(0, pos["qty"] - 1)
                            if sell_qty > 0:
                                trades.append({"sym": sym, "pnl": (close_price - pos["ep"]) * sell_qty, "ep": pos["ep"], "qty": sell_qty, "ex": close_price, "reason": "PARTIAL_PROFIT"})
                                pos["qty"] -= sell_qty
                                pos["partial_done"] = True
                                if pos["qty"] <= 0: symbols_to_close.append(sym); continue
                    
                    if low_price <= pos["sl_price"]:
                        trades.append({"sym": sym, "pnl": (min(pos["sl_price"], open_price) - pos["ep"]) * pos["qty"], "ep": pos["ep"], "qty": pos["qty"], "ex": min(pos["sl_price"], open_price), "reason": "NRM_EXIT"})
                        symbols_to_close.append(sym); continue
                        
                    if ts.hour == 15 and ts.minute >= 15:
                        trades.append({"sym": sym, "pnl": (close_price - pos["ep"]) * pos["qty"], "ep": pos["ep"], "qty": pos["qty"], "ex": close_price, "reason": "NRM_EXIT"})
                        symbols_to_close.append(sym); continue
                        
                    if rsi_mode == "14_LAG0_72":
                        rsi = df["rsi_14"].iloc[idx]
                        if rsi >= 72.0:
                            trades.append({"sym": sym, "pnl": (close_price - pos["ep"]) * pos["qty"], "ep": pos["ep"], "qty": pos["qty"], "ex": close_price, "reason": "RSI_EXIT"})
                            symbols_to_close.append(sym); continue
                    else:
                        rsi = df["rsi_13"].iloc[idx-1] if idx > 0 else 50.0
                        if rsi >= 85.0:
                            trades.append({"sym": sym, "pnl": (close_price - pos["ep"]) * pos["qty"], "ep": pos["ep"], "qty": pos["qty"], "ex": close_price, "reason": "RSI_EXIT"})
                            symbols_to_close.append(sym); continue
                            
                    if pre_lx[sym][idx] and idx+1 < len(df):
                        ex_p = float(df.iloc[idx+1]["open"])
                        trades.append({"sym": sym, "pnl": (ex_p - pos["ep"]) * pos["qty"], "ep": pos["ep"], "qty": pos["qty"], "ex": ex_p, "reason": "NRM_EXIT"})
                        symbols_to_close.append(sym); continue
                        
                for sym in symbols_to_close:
                    if sym in positions: del positions[sym]
                
                if ts.hour >= 15: continue
                if len(positions) >= test_mp: continue
                if current_date not in bull_days: continue
                
                # Scan Entries
                for sym, df in stock_dfs.items():
                    if len(positions) >= test_mp: break
                    if sym in positions: continue
                    # NO INDIVIDUAL GAP CHECK (like live_config_test.py)
                    
                    if ts not in stock_ts_map[sym]: continue
                    idx = stock_ts_map[sym][ts]
                    if pre_le[sym][idx] and not pre_lx[sym][idx] and idx+1 < len(df):
                        ep = float(df.iloc[idx+1]["open"])
                        qty = int(per_slot // ep)
                        if qty > 0:
                            positions[sym] = {"ep": ep, "qty": qty, "sl_price": ep * (1.0 - SL_PCT), "partial_done": False}

            # EOD Square off
            for sym, pos in positions.items():
                trades.append({"sym": sym, "pnl": (float(stock_dfs[sym]["close"].iloc[-1]) - pos["ep"]) * pos["qty"], "ep": pos["ep"], "qty": pos["qty"], "ex": float(stock_dfs[sym]["close"].iloc[-1]), "reason": "EOD"})
                
            df_t = pd.DataFrame(trades)
            if df_t.empty: continue
            
            df_t["stt_tax"] = df_t["ex"] * df_t["qty"] * STT_PCT
            gross = df_t["pnl"].sum() / CAPITAL * 100
            net = (df_t["pnl"].sum() - df_t["stt_tax"].sum()) / CAPITAL * 100
            
            results.append({
                "MP": test_mp,
                "RSI_Mode": rsi_mode,
                "Trades": len(df_t),
                "Gross": gross,
                "Net": net
            })
            
    res_df = pd.DataFrame(results)
    print("\nSTANDALONE LONG ENGINE COMPARISON")
    print(res_df.to_string(index=False))

if __name__ == "__main__":
    main()
