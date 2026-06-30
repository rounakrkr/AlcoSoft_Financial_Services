import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from datetime import time as dtime
from research.build_cache import load_cache
from core.strategy import CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext
from core.strategy_sets import load_strategy_sets
from core.order_executor import round_to_tick
from core.trading_settings import get as cfg

SL_PCT       = 0.010
TSL_ACTIVATION = 1.2
TSL_PCT        = 0.002
rsi_exit = 72

config = load_strategy_sets()
buy_set_def = next(s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

stock_dfs = load_cache()

def get_rsi(sliced, lag=0):
    col = sliced["rsi"]
    idx = -(lag+1)
    if len(col) >= abs(idx) and pd.notna(col.iloc[idx]):
        return float(col.iloc[idx])
    return 50.0

def run_base(gap_min=0.004):
    by_day = {}
    for sym, df in stock_dfs.items():
        prev_close = None
        for d, grp in sorted(df.groupby(df.index.date)):
            by_day.setdefault(d, {})[sym] = {
                "prev_close": prev_close,
                "gap_pct": (float(grp["open"].iloc[0]) - prev_close) / prev_close if prev_close else 0.0
            }
            prev_close = float(grp["close"].iloc[-1])

    strong_up_days = set()
    for d, stocks in by_day.items():
        v = [s for s in stocks.values() if s["prev_close"]]
        if not v: continue
        ups = sum(1 for s in v if s["gap_pct"] >= gap_min)
        if ups / len(v) >= 0.40:
            strong_up_days.add(d)

    print(f"Gap Min {gap_min:.3f}: {len(strong_up_days)} strong up days")

    all_ts = set()
    for df in stock_dfs.values(): all_ts.update(df.index)
    all_ts = sorted(list(all_ts))

    stock_ts_map = {sym: {t: i for i, t in enumerate(df.index)} for sym, df in stock_dfs.items()}

    long_positions = {}
    trades = []
    
    BUYING_POWER = 500000
    per_slot_long = BUYING_POWER / 3

    for ts in all_ts:
        if ts.hour >= 15: continue
        
        # exits
        for sym in list(long_positions.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]
            idx = stock_ts_map[sym][ts]
            sliced = df.iloc[:idx+1]
            cc = sliced.iloc[-1]
            pos = long_positions[sym]
            rsi0 = get_rsi(sliced, 0)
            
            ex = None
            if float(cc["low"]) <= pos["tsl"]:
                ex = min(pos["tsl"], float(cc["open"]))
            else:
                if float(cc["high"]) >= pos["ep"] + abs(pos["ep"]-pos["sl"]) * TSL_ACTIVATION:
                    pos["tsl_on"] = True
                if pos["tsl_on"]:
                    n = round_to_tick(float(cc["high"])*(1-TSL_PCT))
                    if n > pos["tsl"]: pos["tsl"] = n
                    
            if not ex and rsi0 >= rsi_exit:
                ex = float(cc["close"])
                
            if ex:
                trades.append((ex - pos["ep"]) * pos["qty"])
                del long_positions[sym]
                
        # entries
        if ts.date() in strong_up_days:
            for sym, df in stock_dfs.items():
                if len(long_positions) >= 3: break
                if sym in long_positions: continue
                if ts not in stock_ts_map[sym]: continue
                idx = stock_ts_map[sym][ts]
                if idx < 4: continue
                
                sliced = df.iloc[:idx+1]
                ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                cond = evaluator._evaluate_conditions(buy_set_def, ctx)
                if cond and all(c["fired"] for c in cond):
                    if idx+1 < len(df):
                        ep = float(df.iloc[idx+1]["open"])
                        qty = int(per_slot_long // ep)
                        sl_p = round_to_tick(ep*(1-SL_PCT))
                        long_positions[sym] = {"ep": ep, "qty": qty, "sl": sl_p, "tsl": sl_p, "tsl_on": False}
                        
    # force close
    for sym, pos in long_positions.items():
        df = stock_dfs[sym]
        ex = float(df.iloc[-1]["close"])
        trades.append((ex - pos["ep"]) * pos["qty"])

    if not trades: return
    win = sum(1 for t in trades if t > 0)
    loss = len(trades) - win
    gross = sum(trades) / BUYING_POWER * 100
    print(f"Gap={gap_min:.3f} | Trades={len(trades)} | WR={win/len(trades)*100:.1f}% | Gross={gross:.1f}%")

run_base(0.005)
run_base(0.004)
run_base(0.006)
