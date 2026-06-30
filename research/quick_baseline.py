import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from research.short_selling_test import backtest, strong_up_days, no_days, stats, CAPITAL

print("Running baseline from short_selling_test.py...")
t = backtest(3, 0, strong_up_days, no_days)
stats("BASELINE: Long only | Strong_UP_40 regime | max_pos=3", t)
