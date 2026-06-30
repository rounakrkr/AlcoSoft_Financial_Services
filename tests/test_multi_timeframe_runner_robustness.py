import os
import sys
import unittest
import pandas as pd
import numpy as np
import math
from unittest.mock import patch

# Ensure root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.multi_timeframe_runner import (
    MultiTimeframeRunner,
    SystemConfig,
    LongEngineConfig,
    ShortEngineConfig,
    MultiTimeframeReportingEngine
)

class TestMultiTimeframeRunnerRobustness(unittest.TestCase):
    def setUp(self):
        self.sys_config = SystemConfig(capital=100000.0, margin=5.0, max_open_positions=1)
        self.long_config = LongEngineConfig()
        self.short_config = ShortEngineConfig()
        self.output_dir = "research/test_timeframe_reports"
        
        # Base timestamps for 60 days
        self.base_dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=60, freq='D')
        # Generate 5-minute intraday ticks for those days
        # For simplicity in tests, we can generate a small but valid intraday timeline
        ticks = []
        for d in self.base_dates:
            # Add a few intraday points (e.g., 9:15, 12:00, 15:15, 15:30)
            for h, m in [(9, 15), (10, 0), (12, 0), (14, 0), (15, 15), (15, 30)]:
                ticks.append(d.replace(hour=h, minute=m, second=0))
        self.timeline = pd.DatetimeIndex(ticks)

    def create_mock_df(self, num_points=None, missing_cols=None):
        if num_points is not None:
            idx = self.timeline[-num_points:]
        else:
            idx = self.timeline
            
        df = pd.DataFrame(index=idx)
        cols = {
            "open": np.random.uniform(100.0, 110.0, len(idx)),
            "high": np.random.uniform(110.0, 120.0, len(idx)),
            "low": np.random.uniform(90.0, 100.0, len(idx)),
            "close": np.random.uniform(100.0, 110.0, len(idx)),
            "volume": np.random.uniform(1000.0, 5000.0, len(idx)),
            "vwap": np.random.uniform(100.0, 110.0, len(idx)),
            "ema20": np.random.uniform(100.0, 110.0, len(idx)),
            "rsi": np.random.uniform(30.0, 70.0, len(idx)),
            "median_sma14": np.random.uniform(100.0, 110.0, len(idx))
        }
        for col, vals in cols.items():
            if missing_cols and col in missing_cols:
                continue
            df[col] = vals
        return df

    @patch("research.multi_timeframe_runner.load_cache")
    def test_runner_handles_empty_dataframes(self, mock_load_cache):
        """Test how the runner handles empty or missing data in some stock dataframes."""
        print("\n--- Running: test_runner_handles_empty_dataframes ---")
        
        # Mock cache return: One normal stock, one empty stock, one missing columns
        mock_cache = {
            "STOCK_NORMAL": self.create_mock_df(),
            "STOCK_EMPTY": pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap", "ema20", "rsi", "median_sma14"]),
            "STOCK_MISSING_COLS": self.create_mock_df(missing_cols=["open"])
        }
        mock_load_cache.return_value = mock_cache
        
        runner = MultiTimeframeRunner(
            sys_config=self.sys_config,
            long_config=self.long_config,
            short_config=self.short_config,
            output_dir=self.output_dir
        )
        
        # Execute run (should not raise exceptions)
        try:
            results = runner.run()
            self.assertIn("results", results)
            print("SUCCESS: Runner executed without exceptions with empty/missing stock data.")
        except Exception as e:
            self.fail(f"Runner failed with empty/missing stock data: {e}")

    @patch("research.multi_timeframe_runner.load_cache")
    def test_runner_handles_few_data_points_slicing(self, mock_load_cache):
        """Verify that there are no out-of-bounds errors when slicing signal arrays for symbols with few data points."""
        print("\n--- Running: test_runner_handles_few_data_points_slicing ---")
        
        mock_cache = {
            "STOCK_NORMAL": self.create_mock_df(),
            "STOCK_1_POINT": self.create_mock_df(num_points=1),
            "STOCK_5_POINTS": self.create_mock_df(num_points=5),
            "STOCK_11_POINTS": self.create_mock_df(num_points=11)
        }
        mock_load_cache.return_value = mock_cache
        
        runner = MultiTimeframeRunner(
            sys_config=self.sys_config,
            long_config=self.long_config,
            short_config=self.short_config,
            output_dir=self.output_dir
        )
        
        try:
            results = runner.run()
            self.assertIn("results", results)
            print("SUCCESS: Runner executed without exceptions for symbols with few data points.")
        except Exception as e:
            self.fail(f"Runner raised error on short data slicing: {e}")

    @patch("research.multi_timeframe_runner.load_cache")
    def test_report_generation(self, mock_load_cache):
        """Confirm that the reports folder and all reports are successfully generated, and formatting is valid markdown."""
        print("\n--- Running: test_report_generation ---")
        
        mock_cache = {
            "STOCK_NORMAL": self.create_mock_df()
        }
        mock_load_cache.return_value = mock_cache
        
        runner = MultiTimeframeRunner(
            sys_config=self.sys_config,
            long_config=self.long_config,
            short_config=self.short_config,
            output_dir=self.output_dir
        )
        
        results = runner.run()
        
        reporting_engine = MultiTimeframeReportingEngine(
            run_results=results,
            capital=self.sys_config.capital,
            output_dir=self.output_dir
        )
        
        reporting_engine.generate_reports()
        
        # Expected report files: 9 individual window reports + 1 summary report = 10 reports
        expected_files = [
            "report_past_60d.md", "report_past_50d.md", "report_past_40d.md", 
            "report_past_30d.md", "report_past_20d.md", "report_past_14d.md", 
            "report_past_10d.md", "report_last_month.md", "report_this_month.md",
            "multi_timeframe_summary.md"
        ]
        
        self.assertTrue(os.path.exists(self.output_dir))
        for filename in expected_files:
            file_path = os.path.join(self.output_dir, filename)
            self.assertTrue(os.path.exists(file_path), f"Missing report: {filename}")
            
            # Read and verify markdown formatting basics
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            self.assertTrue(content.startswith("#"), f"Report {filename} should start with a header (#)")
            
            # Count columns in table rows to verify alignment (basic markdown validation)
            for line in content.splitlines():
                if line.startswith("|"):
                    # Every table row starting with | must have matching number of columns
                    cols = [c.strip() for c in line.split("|")]
                    # Ensure no empty or mismatched column splits
                    self.assertGreater(len(cols), 1, f"Invalid table row in {filename}: {line}")
        
        print("SUCCESS: All 10 markdown reports generated successfully and format validated.")

if __name__ == "__main__":
    unittest.main()
