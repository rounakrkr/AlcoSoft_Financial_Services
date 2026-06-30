import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.strategy import CONDITION_REGISTRY
from core.strategy_sets import load_strategy_sets

config = load_strategy_sets()

new_conds = [
    "streak_close_1_below_vwap_0",
    "streak_ema20_1_below_vwap_0",
    "streak_rsi_1_below_39",
    "streak_close_0_below_period_min_10",
    "close_1_above_ema21"
]
print("=== NEW CONDITIONS IN REGISTRY ===")
for c in new_conds:
    status = "OK" if c in CONDITION_REGISTRY else "MISSING!!"
    print(f"  {c}: {status}")

print()
print("=== ALL STRATEGY SETS ===")
for s in config.buy_sets:
    print(f"  BUY:  {s.name} ({len(s.conditions)} conditions)")
for s in config.sell_sets:
    print(f"  SELL: {s.name} ({len(s.conditions)} conditions)")

print()
print("LONG buy conditions:")
long_buy = next(s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT")
for c in long_buy.conditions: print(f"  + {c}")

print()
print("SHORT entry conditions:")
short_buy = next(s for s in config.buy_sets if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN")
for c in short_buy.conditions: print(f"  - {c}")

print()
print("LONG exit conditions:")
long_sell = next(s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS")
for c in long_sell.conditions: print(f"  x {c}")

print()
print("SHORT cover conditions:")
short_sell = next(s for s in config.sell_sets if s.name == "SHORT_STREAK_MOMENTUM_RECOVERY")
for c in short_sell.conditions: print(f"  ^ {c}")

long_conds = set(long_buy.conditions)
short_conds = set(short_buy.conditions)
overlap = long_conds & short_conds
print()
print("Condition overlap LONG vs SHORT:", overlap if overlap else "NONE - No clash confirmed!")

long_exit = set(long_sell.conditions)
short_exit = set(short_sell.conditions)
exit_overlap = long_exit & short_exit
print("Exit overlap LONG vs SHORT:", exit_overlap if exit_overlap else "NONE - No clash confirmed!")
print()
print(f"Total conditions registered: {len(CONDITION_REGISTRY)}")
