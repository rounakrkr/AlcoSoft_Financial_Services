import os
import sys
import unittest
import pandas as pd
import numpy as np
import ta

# Ensure we can load local modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.production_engine import (
    IndicatorPreprocessor,
    ShortEngineExecutor,
    SystemConfig,
    ShortEngineConfig,
    OpenPosition,
    MarketRegimeAnalyzer
)

class TestProductionEngine(unittest.TestCase):
    def setUp(self):
        # Create a mock dataframe for testing
        dates = pd.date_range(start="2026-06-17 09:15:00", periods=20, freq="5min")
        self.mock_df = pd.DataFrame(
            {
                "open": [100.0] * 20,
                "high": [102.0] * 20,
                "low": [98.0] * 20,
                "close": [101.0] * 20,
                "volume": [1000] * 20,
            },
            index=dates
        )
        self.stock_dfs = {"TEST": self.mock_df}

    def test_indicator_preprocessor_calculates_ema9_and_vwap(self):
        # Verify that ema9 is calculated and that vwap is dynamically generated if missing
        IndicatorPreprocessor.enrich_data(self.stock_dfs)
        df = self.stock_dfs["TEST"]
        
        self.assertIn("ema9", df.columns)
        self.assertIn("vwap", df.columns)
        self.assertFalse(df["ema9"].isna().any())
        self.assertFalse(df["vwap"].isna().any())

    def test_short_engine_executor_savior_exit_triggered(self):
        # If prev_close > prev_ema9, Savior Exit should trigger
        IndicatorPreprocessor.enrich_data(self.stock_dfs)
        df = self.stock_dfs["TEST"]
        
        # Manually force close > ema9 for candle index 1
        # Close at index 1 is 105, ema9 is 101
        df.loc[df.index[1], "close"] = 105.0
        df.loc[df.index[1], "ema9"] = 101.0
        
        sys_cfg = SystemConfig()
        short_cfg = ShortEngineConfig()
        regime = MarketRegimeAnalyzer(self.stock_dfs)
        
        # Instantiate executor
        signals = {"TEST": [False] * 20}
        executor = ShortEngineExecutor(
            sys_config=sys_cfg,
            short_config=short_cfg,
            stock_dfs=self.stock_dfs,
            regime_analyzer=regime,
            signals=signals
        )
        
        # Setup an open position
        entry_time = df.index[0]
        executor.open_positions["TEST"] = OpenPosition(
            symbol="TEST",
            entry_time=entry_time,
            entry_price=100.0,
            quantity=10,
            direction="SHORT",
            stop_loss_price=105.0
        )
        
        # Manage positions at ts index 2 (which is after entry)
        ts_index_2 = df.index[2]
        executor._manage_positions(ts_index_2)
        
        # Position should have closed via SAVIOR_EXIT
        self.assertNotIn("TEST", executor.open_positions)
        self.assertEqual(len(executor.trades), 1)
        self.assertEqual(executor.trades[0].exit_reason, "SAVIOR_EXIT")
        self.assertEqual(executor.trades[0].exit_price, df.loc[ts_index_2, "open"])

    def test_short_engine_executor_blocks_entry_below_vwap_filter(self):
        # If close < vwap * 0.988, entry is blocked
        IndicatorPreprocessor.enrich_data(self.stock_dfs)
        df = self.stock_dfs["TEST"]
        
        # For entry at index 1: vwap = 100, close = 98 (which is < 100 * 0.988)
        df.loc[df.index[1], "vwap"] = 100.0
        df.loc[df.index[1], "close"] = 98.0
        
        sys_cfg = SystemConfig()
        short_cfg = ShortEngineConfig()
        regime = MarketRegimeAnalyzer(self.stock_dfs)
        
        # Mock bear day gap
        regime.all_daily_gaps[(df.index[1].date(), "TEST")] = -0.010 # qualified gap
        
        # Mock signal to fire at index 1
        signals = {"TEST": [False] * 20}
        signals["TEST"][1] = True
        
        executor = ShortEngineExecutor(
            sys_config=sys_cfg,
            short_config=short_cfg,
            stock_dfs=self.stock_dfs,
            regime_analyzer=regime,
            signals=signals
        )
        
        # Scan for entries at index 1
        ts_index_1 = df.index[1]
        executor._scan_for_entries(ts_index_1, ts_index_1.date())
        
        # Entry should be blocked (no open positions)
        self.assertNotIn("TEST", executor.open_positions)

if __name__ == "__main__":
    unittest.main()
