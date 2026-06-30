import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import pandas as pd
import numpy as np
import datetime
from dataclasses import dataclass

# We can import classes and functions from sweep_midcap50_engine
from research.sweep_midcap50_engine import (
    LongEngineConfig,
    SystemConfig,
    LongEngineExecutor,
    SignalGenerator,
    compute_variant_f_signals,
    MarketRegimeAnalyzer,
    TradeRecord
)

class TestM4Features(unittest.TestCase):
    def setUp(self):
        # Create a mock dataframe representing 20 candles of 1 stock
        timestamps = [pd.Timestamp("2026-06-22 09:15:00") + datetime.timedelta(minutes=i) for i in range(25)]
        
        # Populate columns with mock indicator data
        self.df = pd.DataFrame(index=timestamps)
        self.df["open"] = [100.0 + i * 0.5 for i in range(25)]
        self.df["high"] = [101.0 + i * 0.5 for i in range(25)]
        self.df["low"] = [99.0 + i * 0.5 for i in range(25)]
        self.df["close"] = [100.5 + i * 0.5 for i in range(25)]
        self.df["volume"] = [1000 + i * 100 for i in range(25)]
        self.df["avg_vol"] = [1000.0] * 25
        self.df["rsi"] = [50.0 + i * 1.5 for i in range(25)] # rising RSI
        self.df["rsi_14"] = self.df["rsi"]
        self.df["vwap"] = [100.0] * 25
        self.df["ema9"] = [100.0 + i * 0.4 for i in range(25)]
        self.df["ema20"] = [100.0 + i * 0.3 for i in range(25)]
        self.df["ema21"] = [100.0 + i * 0.3 for i in range(25)]
        self.df["ema50"] = [100.0] * 25
        self.df["macd"] = [0.1 * i for i in range(25)]
        self.df["macd_sig"] = [0.05 * i for i in range(25)]
        self.df["sma20"] = [100.0] * 25
        self.df["median_sma14"] = [100.0] * 25
        self.df["obv"] = [10000 + i * 1000 for i in range(25)]
        self.df["bb_lower"] = [95.0] * 25
        self.df["bb_upper"] = [105.0] * 25
        
        self.stock_dfs = {"MOCK_STOCK": self.df}

    def test_signal_generator_variants(self):
        """Verify that all entry variants are precomputed correctly without crash"""
        sig_gen = SignalGenerator(self.stock_dfs)
        
        # Test BASELINE precomputation
        sig_gen.precompute_signals("BASELINE")
        self.assertEqual(len(sig_gen.long_signals["MOCK_STOCK"]), 25)
        
        # Test VARIANT_A precomputation (relaxed RSI > 55)
        sig_gen.precompute_signals("VARIANT_A")
        self.assertEqual(len(sig_gen.long_signals["MOCK_STOCK"]), 25)
        
        # Test VARIANT_B precomputation (relaxed RSI > 50)
        sig_gen.precompute_signals("VARIANT_B")
        self.assertEqual(len(sig_gen.long_signals["MOCK_STOCK"]), 25)
        
        # Test VARIANT_C precomputation (volume spike)
        sig_gen.precompute_signals("VARIANT_C")
        self.assertTrue(len(sig_gen.long_signals["MOCK_STOCK"]) == 25)
        
        # Test VARIANT_D precomputation (MACD > 0)
        sig_gen.precompute_signals("VARIANT_D")
        self.assertTrue(len(sig_gen.long_signals["MOCK_STOCK"]) == 25)
        
        # Test VARIANT_E precomputation (Steady Trend OR Breakout)
        sig_gen.precompute_signals("VARIANT_E")
        self.assertTrue(len(sig_gen.long_signals["MOCK_STOCK"]) == 25)
        
        # Test VARIANT_F precomputation (Draft strategies loaded from strategy.txt)
        sig_gen.precompute_signals("VARIANT_F")
        self.assertTrue(len(sig_gen.long_signals["MOCK_STOCK"]) == 25)

    def test_reentry_cap_filter(self):
        """Verify that re-entry cap blocks entries when trade count for day is reached"""
        sys_config = SystemConfig(capital=100000.0, margin=5.0, max_open_positions=1)
        
        # reentry_cap = 0 (max 1 trade per day per stock)
        config = LongEngineConfig(reentry_cap=0, min_hold_time=0, min_price=0.0, min_expected_pnl=0.0)
        regime = MarketRegimeAnalyzer(self.stock_dfs)
        
        # Create signals
        signals = {"MOCK_STOCK": [False] * 25}
        signals["MOCK_STOCK"][5] = True
        signals["MOCK_STOCK"][15] = True # secondary signal same day
        
        ts_with_signals = {self.df.index[5], self.df.index[15]}
        signals_by_ts = {self.df.index[5]: ["MOCK_STOCK"], self.df.index[15]: ["MOCK_STOCK"]}
        
        executor = LongEngineExecutor(
            sys_config=sys_config,
            long_config=config,
            stock_dfs=self.stock_dfs,
            regime_analyzer=regime,
            signals=signals,
            ts_with_signals=ts_with_signals,
            signals_by_ts=signals_by_ts
        )
        
        # Mock bull day to allow scanner to execute
        executor.bull_days = {self.df.index[0].date()}
        
        # Execute scanner
        # Signal 1 (at index 5)
        executor._scan_for_entries(self.df.index[5], self.df.index[5].date())
        self.assertIn("MOCK_STOCK", executor.open_positions)
        self.assertEqual(executor.daily_entries[(self.df.index[6].date(), "MOCK_STOCK")], 1)
        
        # Simulate exit
        del executor.open_positions["MOCK_STOCK"]
        
        # Signal 2 (at index 15) should be blocked by reentry_cap=0
        executor._scan_for_entries(self.df.index[15], self.df.index[15].date())
        self.assertNotIn("MOCK_STOCK", executor.open_positions)

    def test_min_hold_time_filter(self):
        """Verify that min_hold_time blocks RSI overbought exit when hold time is not met"""
        sys_config = SystemConfig(capital=100000.0, margin=5.0, max_open_positions=1)
        
        # Set min_hold_time to 15 minutes
        config = LongEngineConfig(min_hold_time=15, rsi_exit_threshold=70.0)
        regime = MarketRegimeAnalyzer(self.stock_dfs)
        
        signals = {"MOCK_STOCK": [False] * 25}
        ts_with_signals = set()
        signals_by_ts = {}
        
        executor = LongEngineExecutor(
            sys_config=sys_config,
            long_config=config,
            stock_dfs=self.stock_dfs,
            regime_analyzer=regime,
            signals=signals,
            ts_with_signals=ts_with_signals,
            signals_by_ts=signals_by_ts
        )
        
        # Force position entry at index 5
        entry_time = self.df.index[5]
        executor.open_positions["MOCK_STOCK"] = executor.open_positions.get("MOCK_STOCK") or \
            type('Pos', (object,), {
                "symbol": "MOCK_STOCK",
                "entry_time": entry_time,
                "entry_price": 100.0,
                "quantity": 10,
                "stop_loss_price": 90.0,
                "partial_booked": True
            })()
            
        # At index 10 (5 minutes later), check if RSI exit is allowed
        # RSI at index 10 is > 70.0, but hold time (5 min) < min_hold_time (15 min)
        executor._manage_positions(self.df.index[10])
        # Position should still be open
        self.assertIn("MOCK_STOCK", executor.open_positions)
        
        # At index 22 (17 minutes later), hold time (17 min) >= min_hold_time (15 min)
        executor._manage_positions(self.df.index[22])
        # Position should be closed now
        self.assertNotIn("MOCK_STOCK", executor.open_positions)
        self.assertEqual(len(executor.trades), 1)
        self.assertEqual(executor.trades[0].exit_reason, "RSI_OVERBOUGHT")

if __name__ == "__main__":
    unittest.main()
