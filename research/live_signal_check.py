import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import warnings, logging
warnings.filterwarnings('ignore')
logging.disable(logging.CRITICAL)

from core.data_fetcher import get_candle_history
from core.strategy import _build_indicators, CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext
from core.strategy_sets import load_strategy_sets

config = load_strategy_sets()
buy_set = next((s for s in config.buy_sets if s.name == 'BUY_STREAK_MOMENTUM_BREAKOUT'), None)
evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

stocks = [
    'BAJAJFINSV','HINDALCO','SBILIFE','LT','MARUTI','BPCL','TITAN','ADANIPORTS',
    'RELIANCE','TATASTEEL','HDFCBANK','ICICIBANK','BHARTIARTL','SBIN','NTPC','JSWSTEEL',
    'TCS','INFY','HCLTECH','WIPRO','M&M','ITC','SUNPHARMA','EICHERMOT','HEROMOTOCO',
    'GRASIM','BRITANNIA','CIPLA','COALINDIA','DRREDDY'
]

print("LIVE CONDITION CHECK @ " + str(pd.Timestamp.now().strftime('%H:%M:%S')))
print('-'*80)
for sym in stocks:
    try:
        hist = get_candle_history(sym)
        if not hist or len(hist) < 15:
            print(f"{sym}: Not enough data ({len(hist) if hist else 0} candles)")
            continue
        df = pd.DataFrame(hist)
        df.columns = [c.lower() for c in df.columns]
        df = _build_indicators(df)
        ctx = StrategyEvaluationContext(side='buy', indicator_df=df, pattern_df=df, ws_count=len(df))
        results = evaluator._evaluate_conditions(buy_set, ctx)
        fired = all(r.get('fired') for r in results)
        conds = []
        for r in results:
            name = r.get('condition_name','?')
            val = 'Y' if r.get('fired') else 'N'
            conds.append(name + "=" + val)
        cond_str = ' | '.join(conds)
        status = ">>> SIGNAL FIRED <<<"if fired else "No signal"
        print(sym + ": " + status + "  [" + cond_str + "]")
    except Exception as e:
        print(sym + ": ERROR - " + str(e))
