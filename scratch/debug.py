import pandas as pd
from research.backtest_runner import BacktestRunner
from screener.morning_screener import _fetch_yahoo_history
from core.strategy import _build_indicators, StrategyEvaluationContext

runner = BacktestRunner(25000, 'BUY_STEADY_MOMENTUM_TREND', 'SELL_EMA_MOMENTUM_LOSS')
df = _fetch_yahoo_history('RELIANCE', period='5d', interval='5m').rename(columns=str.lower)
df['bucket'] = df.index
df = _build_indicators(df).dropna()

print(f"Total valid candles: {len(df)}")
for i in range(10, len(df)):
    sliced = df.iloc[:i+1]
    ctx = StrategyEvaluationContext('buy', sliced, sliced, 0)
    res = runner.evaluator._evaluate_conditions(runner.buy_set_def, ctx)
    fired_conds = [r['name'] for r in res if r['fired']]
    if len(fired_conds) > 0:
        print(f"Candle {i}: {fired_conds}")
        if len(fired_conds) == len(res):
            print("  -> ALL CONDITIONS MET (TRADE!)")
