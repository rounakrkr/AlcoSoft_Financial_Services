"""
TEST: KINETIC TSL AT PROFIT TARGET
====================================
- No Rule 1 (allow multiple trades per stock per day)
- No RSI >= 72 exit
- Target: +0.35% to +0.45% (parameterized)
- If target reached:
   - Check kinetic momentum
   - If kinetic is strong: Lock TSL at the target level and let it run
   - If kinetic is weak: Book full profit at target
- Max positions: 3, 5, 7, 10
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from research.build_cache import load_cache
from core.strategy import CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext
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
evaluator     = StrategySetEvaluator(CONDITION_REGISTRY)

TSL_ACTIVATION = float(cfg("risk", "tsl_activation_ratio", 1.2))
TSL_PCT        = float(cfg("risk", "trailing_sl_percent", 0.002))

stock_dfs = load_cache()
print(f"{len(stock_dfs)} stocks loaded.")

all_dates_set = set(ts.date() for df in stock_dfs.values() for ts in df.index)
all_ts = set()
for df in stock_dfs.values(): all_ts.update(df.index.tolist())
timeline = sorted(all_ts)
stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

# ── Kinetic Check Logic ──
def kinetic_check(sliced, condition_name):
    if len(sliced) < 5: return True
    cc  = sliced.iloc[-1]
    pc  = sliced.iloc[-2]
    ppc = sliced.iloc[-3] if len(sliced) >= 3 else pc

    rsi0 = float(cc["rsi"])  if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0
    rsi1 = float(pc["rsi"])  if "rsi" in pc.index and pd.notna(pc["rsi"]) else 50.0
    vwap = float(cc["vwap"]) if "vwap" in cc.index and pd.notna(cc["vwap"]) else 0.0
    ema0 = float(cc["ema21"])if "ema21" in cc.index and pd.notna(cc["ema21"]) else 0.0
    
    close0 = float(cc["close"]); open0 = float(cc["open"])
    close1 = float(pc["close"]); open1 = float(pc["open"])

    rsi_rising   = rsi0 > rsi1
    above_vwap   = close0 > vwap > 0
    green_candle = close0 > open0
    
    # Check if original buy conditions are still valid (momentum active)
    conds = {
        "K_RSI_RISING":          rsi_rising,
        "K_ABOVE_VWAP":          above_vwap,
        "K_RSI+VWAP":            rsi_rising and above_vwap,
        "K_RSI+GREEN":           rsi_rising and green_candle,
        "K_ALL":                 rsi_rising and above_vwap and green_candle
    }
    return conds.get(condition_name, True)

# ── Backtest Engine ──
def backtest(max_pos, profit_target, kinetic_name, partial_fraction=0.0):
    per_slot  = BUYING_POWER / max(max_pos, 1)
    longs = {}; trades = []

    for ts in timeline:
        d = ts.date()
        closed_l = []

        # Manage LONG
        for sym in list(longs.keys()):
            if ts not in stock_ts_map.get(sym, {}): continue
            df = stock_dfs[sym]; idx = stock_ts_map[sym][ts]
            if idx < 4: continue
            sliced = df.iloc[:idx+1]; cc = sliced.iloc[-1]
            pos = longs[sym]
            close = float(cc["close"]); high=float(cc["high"]); low=float(cc["low"])
            ep = pos["ep"]; qty = pos["qty"]

            ex = None
            if low <= pos["tsl"]: 
                ex = min(pos["tsl"], float(cc["open"]))
                reason = "TSL" if pos["tsl"] > pos["sl"] else "SL"
            elif ts.hour==15 and ts.minute>=15: 
                ex = close
                reason = "EOD"
            elif not pos.get("kinetic_checked"):
                target_price = round_to_tick(ep * (1 + profit_target))
                if high >= target_price:
                    # Target hit! Run kinetic check
                    is_strong = kinetic_check(sliced, kinetic_name)
                    pos["kinetic_checked"] = True
                    if is_strong:
                        if partial_fraction > 0.0:
                            # Partial book
                            book_qty = int(qty * partial_fraction)
                            if book_qty > 0:
                                rem_qty = qty - book_qty
                                stt = target_price * book_qty * STT_RATE
                                trades.append({"pnl": (target_price - ep)*book_qty, "stt": stt, "side": "LONG", "reason": f"PARTIAL_{profit_target*100:.2f}%"})
                                pos["qty"] = rem_qty
                                qty = rem_qty

                        pos["kinetic_passed"] = True
                        pos["tsl"] = max(pos["tsl"], target_price) # Lock TSL at target
                        pos["tsl_on"] = True
                    else:
                        ex = target_price
                        reason = f"TARGET_{profit_target*100:.2f}%"
            
            if ex:
                stt = ex*qty*STT_RATE
                trades.append({"pnl":(ex-ep)*qty,"stt":stt,"side":"LONG","reason":reason})
                closed_l.append(sym)
            else:
                # Normal TSL trailing
                if high >= ep+abs(ep-pos["sl"])*TSL_ACTIVATION: pos["tsl_on"]=True
                if pos["tsl_on"]:
                    n = round_to_tick(high*(1-TSL_PCT))
                    if n > pos["tsl"]: pos["tsl"]=n

        for s in closed_l: del longs[s]
        if ts.hour >= 15: continue

        # New LONG entries (No Rule 1! Multiple trades per stock allowed)
        if len(longs) < max_pos:
            for sym in stock_dfs:
                if len(longs) >= max_pos: break
                if sym in longs: continue
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
                    qty = int(per_slot//ep)
                    if qty < 1: continue
                    sl_p = round_to_tick(ep*(1-SL_PCT))
                    longs[sym] = {"ep":ep,"qty":qty,"sl":sl_p,"tsl":sl_p,"tsl_on":False, 
                                  "kinetic_checked":False, "kinetic_passed":False}

    # Force close all open
    for sym, pos in longs.items():
        lc = float(stock_dfs[sym]["close"].iloc[-1])
        stt = lc*pos["qty"]*STT_RATE
        trades.append({"pnl":(lc-pos["ep"])*pos["qty"],"stt":stt,"side":"LONG","reason":"EOD"})
    return trades

results = []
def stats(label, trades):
    if not trades:
        print(f"  {label:85s} | NO TRADES"); return
    pnls = [t["pnl"] for t in trades]; stts=[t["stt"] for t in trades]
    wins = sum(1 for p in pnls if p>0)
    wr   = wins/len(pnls)*100
    gross= sum(pnls); tstt=sum(stts); net=gross-tstt
    g_r=gross/CAPITAL*100; n_r=net/CAPITAL*100; s_r=tstt/CAPITAL*100
    crown = " <<<<< KING" if n_r>=20 else (" ***" if n_r>=15 else "")
    print(f"  {label:75s} | WR={wr:5.1f}% | T={len(pnls):4d} | "
          f"Gross={g_r:+6.1f}% | STT={s_r:4.1f}% | NET={n_r:+6.1f}%{crown}")
    results.append({"label":label,"wr":wr,"t":len(pnls),"net":n_r})


print(f"\n{'='*130}")
print("TEST: KINETIC TSL AT PROFIT TARGET (LONG ONLY)")
print("No Rule 1. No RSI Exit. Lock TSL at target if kinetic is strong.")
print(f"{'='*130}")

profit_targets = [0.0035, 0.0040, 0.0045]
max_positions = [3, 5, 7, 10]
kinetics = ["K_RSI_RISING", "K_RSI+VWAP"]

for max_pos in max_positions:
    print(f"\n--- MAX POSITIONS: {max_pos} ---")
    
    # Baseline with no kinetic logic (just hard target)
    for pt in profit_targets:
        # Pass a bogus kinetic name that returns False (always exits at target)
        t = backtest(max_pos, pt, "ALWAYS_FALSE")
        stats(f"Blind Target {pt*100:.2f}% (No Kinetic - Exit Every Time)", t)
        
    print()
    
    # Kinetic testing (No partial vs 50% partial)
    for pt in profit_targets:
        for k in kinetics:
            t = backtest(max_pos, pt, k, partial_fraction=0.0)
            stats(f"Target {pt*100:.2f}% -> Kinetic {k} (TSL lock 100%)", t)
            
            t_partial = backtest(max_pos, pt, k, partial_fraction=0.5)
            stats(f"Target {pt*100:.2f}% -> Kinetic {k} (Book 50%, TSL 50%)", t_partial)

print(f"\n{'='*130}")
print(f"TOP 15 CONFIGURATIONS")
print(f"{'='*130}")
top = sorted(results, key=lambda x: x["net"], reverse=True)[:15]
for i, rx in enumerate(top, 1):
    print(f"  #{i:2d} | {rx['label']:75s} | WR={rx['wr']:5.1f}% | T={rx['t']:4d} | NET={rx['net']:+6.1f}%")
