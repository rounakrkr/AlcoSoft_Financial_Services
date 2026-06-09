import os
import sys
import pandas as pd
import json
import logging

logging.basicConfig(level=logging.INFO)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.strategy import _get_indicator_df, _strategy_set_evaluator, StrategyEvaluationContext

with open("data/session_briefing.json", "r") as f:
    briefing = json.load(f)

stocks = [s["ticker"] for s in briefing.get("approved_stocks", [])] + [s["ticker"] for s in briefing.get("watchlist", [])]

signals = 0
for symbol in stocks:
    try:
        df = _get_indicator_df(symbol)
        if df is None or len(df) < 20:
            continue
        
        pattern_df = df.iloc[-20:]
        
        ctx = StrategyEvaluationContext(
            side="buy",
            indicator_df=df,
            pattern_df=pattern_df,
            ws_count=20
        )
        
        res = _strategy_set_evaluator.evaluate("buy", ctx)
        if res:
            print(f"Signal on {symbol}: {res}")
            signals += 1
    except Exception as e:
        print(f"Error on {symbol}: {e}")

print(f"Total signals generated today: {signals}")
