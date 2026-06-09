# Future Enhancements: Trend Strength Filter (ADX)

This document outlines the planned enhancement for integrating ADX (Average Directional Index) into the AlcoSoft trading engine.

## Option 1: ADX (Trend Strength)

### Purpose
ADX measures the pure strength of a trend, regardless of whether it is going up or down. A high ADX (typically > 25) means the market is trending strongly. A low ADX (< 20) means the market is moving sideways or chopping.

By adding ADX as a `STATE` condition, we can completely filter out trades when the market is chopping sideways, significantly reducing fakeouts and stop-loss hits during flat periods.

### Implementation Steps

1. **Indicator Calculation (`core/strategy.py`)**
   In the `_build_indicators` function, add the `ta.trend.ADXIndicator`:
   ```python
   adx_obj = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
   df["adx"] = adx_obj.adx()
   ```

2. **Condition Function (`core/strategy.py`)**
   Create a new condition to verify that the trend is strong:
   ```python
   def condition_adx_strong_trend(ctx: StrategyEvaluationContext) -> dict:
       df = ctx.indicator_df
       if "adx" not in df.columns:
           return _indicator_strategy_result("ADX Strong Trend", False, "ADX not ready")
       
       latest_adx = float(df["adx"].iloc[-1])
       # 25 is the standard threshold for a "strong" trend
       is_strong = latest_adx > 25.0
       return _indicator_strategy_result(
           "ADX Strong Trend",
           is_strong,
           f"ADX={latest_adx:.2f} (Needs > 25)"
       )
   ```

3. **Register Condition**
   Add it to `CONDITION_REGISTRY` in `core/strategy.py`:
   ```python
   "adx_strong_trend": condition_adx_strong_trend,
   ```

4. **Strategy Set Integration (`config/strategy_sets.json`)**
   Add `"adx_strong_trend"` to the `conditions` array of strong trend-following strategies like `BUY_PERFECT_TREND_PULLBACK` or breakout strategies to ensure they only fire when the market has enough momentum to carry the trade to profit.
