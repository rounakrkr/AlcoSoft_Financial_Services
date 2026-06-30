import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import warnings, logging
warnings.filterwarnings('ignore')
logging.disable(logging.CRITICAL)

from research.build_cache import load_cache
from core.strategy import _build_indicators, CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext
from core.strategy_sets import load_strategy_sets

stock_dfs = load_cache()
config = load_strategy_sets()
buy_set = next((s for s in config.buy_sets if s.name == 'BUY_STREAK_MOMENTUM_BREAKOUT'), None)
evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

for sym, df in list(stock_dfs.items())[:10]:
    d = df.copy()
    d.columns = [c.lower() for c in d.columns]
    d = _build_indicators(d)
    ctx = StrategyEvaluationContext(side='buy', indicator_df=d, pattern_df=d, ws_count=len(d))
    results = evaluator._evaluate_conditions(buy_set, ctx)
    fired = all(r.get('fired') for r in results)
    print(f'{sym}: {fired}')
    if not fired:
        for r in results:
            if not r.get('fired'):
                print(f"  Failed: {r.get('condition_name')} -> {r.get('reason')}")
