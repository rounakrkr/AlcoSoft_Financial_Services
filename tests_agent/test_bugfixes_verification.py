"""
Verification tests for the 4 bug fixes reported by the main agent.

Bugs:
1. reflection.cognitive_agents.should_run_cognitive_cycle timings
2. core.strategy.condition_macd_hist_rejection_bounce uses macd_sig
3. core.strategy.condition_streak_close_0_above_period_max_10 window/duplicate
4. research.multi_timeframe_runner run() with empty DataFrames (covered elsewhere)

Run:
    TZ=Asia/Kolkata python -m pytest tests_agent/ -q
"""

import os
import sys
import time as _time
import subprocess
from datetime import datetime, time as dt_time
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

# Ensure Asia/Kolkata TZ (matches main.py behaviour)
os.environ["TZ"] = "Asia/Kolkata"
try:
    _time.tzset()
except Exception:
    pass

# Add project root to path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ─────────────────────────────────────────────────────────────
#  BUG FIX 1: cognitive cycle scheduling window
# ─────────────────────────────────────────────────────────────
class TestBug1CognitiveCycleScheduling:
    """Verify LAST_COGNITION_TIME=15:15 and EXECUTION_STOP_TIME=15:00."""

    def test_constants_are_pm(self):
        from reflection import cognitive_agents as ca
        assert ca.LAST_COGNITION_TIME == dt_time(15, 15), (
            f"LAST_COGNITION_TIME should be 15:15 PM IST, got {ca.LAST_COGNITION_TIME}"
        )
        assert ca.EXECUTION_STOP_TIME == dt_time(15, 0), (
            f"EXECUTION_STOP_TIME should be 15:00 PM IST, got {ca.EXECUTION_STOP_TIME}"
        )

    def test_should_run_at_10_00_am_returns_true(self):
        from reflection import cognitive_agents as ca

        fake_now = datetime(2026, 1, 15, 10, 0, 0)  # 10:00 AM IST => 30 min after 9:30 first cycle => cycle 2 => "C"
        with patch.object(ca, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            # Preserve the .time() call on the returned object
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            should_run, agent = ca.should_run_cognitive_cycle()
        assert should_run is True, f"Expected True at 10:00 AM, got {should_run}"
        assert agent in ca.AGENT_ROTATION, f"Agent {agent} not in rotation"

    def test_should_run_at_09_30_am_returns_true_first_cycle(self):
        from reflection import cognitive_agents as ca
        fake_now = datetime(2026, 1, 15, 9, 30, 0)
        with patch.object(ca, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            should_run, agent = ca.should_run_cognitive_cycle()
        assert should_run is True
        assert agent == ca.AGENT_ROTATION[0], f"First cycle should be agent A, got {agent}"

    def test_should_not_run_at_15_20_pm(self):
        from reflection import cognitive_agents as ca
        fake_now = datetime(2026, 1, 15, 15, 20, 0)
        with patch.object(ca, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            should_run, agent = ca.should_run_cognitive_cycle()
        assert should_run is False
        assert agent is None

    def test_should_not_run_at_09_00_am(self):
        from reflection import cognitive_agents as ca
        fake_now = datetime(2026, 1, 15, 9, 0, 0)
        with patch.object(ca, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            should_run, agent = ca.should_run_cognitive_cycle()
        assert should_run is False
        assert agent is None

    def test_should_run_at_15_15_last_cycle(self):
        from reflection import cognitive_agents as ca
        fake_now = datetime(2026, 1, 15, 15, 15, 0)
        with patch.object(ca, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            should_run, agent = ca.should_run_cognitive_cycle()
        # 15:15 == LAST_COGNITION_TIME. Condition is `> LAST_COGNITION_TIME`, so 15:15 is allowed.
        # And 15:15 is exactly at a 15-min boundary from 9:30 (345 min => 23 cycles => agent D)
        assert should_run is True
        assert agent in ca.AGENT_ROTATION


# ─────────────────────────────────────────────────────────────
#  BUG FIX 2 & 3: strategy condition fixes
# ─────────────────────────────────────────────────────────────
def _build_synthetic_ohlcv(n=60, seed=42):
    rng = np.random.default_rng(seed)
    # generate a realistic trending series with intraday timestamps
    idx = pd.date_range("2026-01-05 09:15", periods=n, freq="5min")
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    open_ = close + rng.normal(0, 0.2, n)
    high = np.maximum(open_, close) + rng.uniform(0, 0.4, n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.4, n)
    vol = rng.uniform(1000, 5000, n)
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )
    return df


class TestBug2MacdHistBounce:
    """Verify condition_macd_hist_rejection_bounce evaluates real values, not 'Not ready'."""

    def test_condition_runs_after_build_indicators(self):
        from core import strategy
        df = _build_synthetic_ohlcv(60)
        df_ind = strategy._build_indicators(df)
        assert "macd" in df_ind.columns
        assert "macd_sig" in df_ind.columns, "Indicator column should be 'macd_sig'"
        # Drop early NaNs so MACD is real
        df_ind = df_ind.dropna(subset=["macd", "macd_sig"]).reset_index(drop=True)
        assert len(df_ind) >= 4

        ctx = strategy.StrategyEvaluationContext(side="long", indicator_df=df_ind)
        result = strategy.condition_macd_hist_rejection_bounce(ctx)
        assert isinstance(result, dict)
        assert result.get("name") == "MACD Hist Reject"
        assert result.get("reason") != "Not ready", (
            f"Condition still reporting 'Not ready' - macd_sig column not detected. "
            f"Got: {result}"
        )
        # Reason should mention Bounced and >0
        assert "Bounced" in result["reason"] and ">0" in result["reason"]


class TestBug3StreakClose10:
    """Verify only one definition exists AND window uses iloc[-11:-1] (includes previous candle)."""

    def test_only_one_definition_exists(self):
        strategy_path = os.path.join(ROOT, "core", "strategy.py")
        result = subprocess.run(
            ["grep", "-c", "def condition_streak_close_0_above_period_max_10",
             strategy_path],
            capture_output=True, text=True
        )
        count = int(result.stdout.strip())
        assert count == 1, f"Expected exactly 1 definition, found {count}"

    def test_condition_does_not_fire_when_prev_candle_is_high(self):
        """
        Craft: 12 rows. Previous candle (iloc[-2]) has the HIGHEST high of last 10 candles.
        Current close > all other highs but < iloc[-2].high => must NOT fire.
        With the buggy iloc[-12:-2] window, iloc[-2] was excluded => it WOULD fire.
        Fix uses iloc[-11:-1] which INCLUDES iloc[-2] => correctly does NOT fire.
        """
        from core import strategy

        # Build 12 candles: highs = 100 for candles [-11 .. -3] and [-1], but iloc[-2] high = 200
        highs = [100.0] * 12
        highs[-2] = 200.0  # previous candle is the peak
        lows = [90.0] * 12
        closes = [95.0] * 11 + [150.0]  # current close = 150 > all other highs (100) but < 200
        opens = [95.0] * 12
        vols = [1000.0] * 12
        idx = pd.date_range("2026-01-05 09:15", periods=12, freq="5min")
        df = pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
            index=idx,
        )
        ctx = strategy.StrategyEvaluationContext(side="long", indicator_df=df)
        result = strategy.condition_streak_close_0_above_period_max_10(ctx)
        assert result["fired"] is False, (
            f"Bug: condition fired when prev candle was the 10-window max. Result: {result}"
        )
        # Verify max was correctly computed as 200 (includes prev candle)
        assert "200" in result["reason"], f"Max should reflect prev candle=200. Got: {result['reason']}"

    def test_condition_fires_when_current_close_is_true_breakout(self):
        """Sanity: when current close breaks above the entire 10-candle prior window, should fire."""
        from core import strategy

        highs = [100.0] * 11 + [105.0]  # only last candle can exceed
        lows = [90.0] * 12
        closes = [95.0] * 11 + [110.0]  # 110 > max(100) prior window
        opens = [95.0] * 12
        vols = [1000.0] * 12
        idx = pd.date_range("2026-01-05 09:15", periods=12, freq="5min")
        df = pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
            index=idx,
        )
        ctx = strategy.StrategyEvaluationContext(side="long", indicator_df=df)
        result = strategy.condition_streak_close_0_above_period_max_10(ctx)
        assert result["fired"] is True, f"Should fire on a true breakout. Result: {result}"


# ─────────────────────────────────────────────────────────────
#  SANITY: py_compile
# ─────────────────────────────────────────────────────────────
class TestSanityCompile:
    def test_all_modules_compile(self):
        import glob, py_compile
        targets = ["main.py"]
        for d in ("core", "screener", "reflection", "dashboard"):
            targets += glob.glob(os.path.join(ROOT, d, "*.py"))
        # Ensure main.py absolute path
        targets = [t if os.path.isabs(t) else os.path.join(ROOT, t) for t in targets]
        failures = []
        for t in targets:
            try:
                py_compile.compile(t, doraise=True)
            except py_compile.PyCompileError as e:
                failures.append(f"{t}: {e}")
        assert not failures, f"Compile failures: {failures}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
