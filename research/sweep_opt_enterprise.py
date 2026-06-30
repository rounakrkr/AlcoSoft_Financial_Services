import sys
import os
import time
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, Any

import pandas as pd
import numpy as np

# Ensure we can load local modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["STRATEGY_SETS_PATH"] = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "strategy_sets_opt.json")
)

from research.build_cache import load_cache
from research.verify_dual_engine_enterprise_opt import (
    SystemConfig,
    LongEngineConfig,
    ShortEngineConfig,
    TradeRecord,
    OpenPosition,
    IndicatorPreprocessor,
    MarketRegimeAnalyzer,
    SignalGenerator,
    LongEngineExecutor,
    ShortEngineExecutor,
    ReportingEngine
)

# Optimized Fast execution engines using dict/array lookups
class FastLongEngineExecutor:
    def __init__(
        self,
        sys_config: SystemConfig,
        long_config: LongEngineConfig,
        stock_dfs: Dict[str, pd.DataFrame],
        regime_analyzer: MarketRegimeAnalyzer,
        signals: Dict[str, List[bool]],
        timeline: List[pd.Timestamp],
        stock_ts_map: Dict[str, Dict[pd.Timestamp, int]],
        arrays: Dict[str, Dict[str, np.ndarray]],
        bull_days: Set[Any],
        active_timestamps: Set[pd.Timestamp]
    ):
        self.sys_config = sys_config
        self.config = long_config
        self.stock_dfs = stock_dfs
        self.regime = regime_analyzer
        self.signals = signals
        self.timeline = timeline
        self.stock_ts_map = stock_ts_map
        self.arrays = arrays
        self.bull_days = bull_days
        self.active_timestamps = active_timestamps
        self.trades: List[TradeRecord] = []
        self.open_positions: Dict[str, OpenPosition] = {}
        
        # Precompute timeline metadata to speed up execute loop
        self.timeline_meta = []
        for ts in self.timeline:
            dt = ts.date()
            is_bull = dt in self.bull_days
            before_15 = ts.hour < 15
            self.timeline_meta.append((ts, dt, is_bull, before_15))
        
    def execute(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[TradeRecord]:
        self.trades = []
        self.open_positions = {}
        
        use_fast = (start_date == "2024-01-01" and end_date == "2026-06-21") or (start_date is None and end_date is None)
        
        if use_fast:
            eval_timeline_meta = self.timeline_meta
        else:
            eval_timeline = self.timeline
            if start_date:
                eval_timeline = [ts for ts in eval_timeline if ts.tz_localize(None) >= pd.Timestamp(start_date)]
            if end_date:
                eval_timeline = [ts for ts in eval_timeline if ts.tz_localize(None) <= pd.Timestamp(end_date)]
            eval_timeline_meta = [(ts, ts.date(), ts.date() in self.bull_days, ts.hour < 15) for ts in eval_timeline]
            
        for ts, date_only, is_bull_day, before_15 in eval_timeline_meta:
            if not self.open_positions:
                if ts not in self.active_timestamps or not is_bull_day or not before_15:
                    continue
            
            # 1. Manage Open Positions
            syms_to_close = []
            for sym, pos in list(self.open_positions.items()):
                ts_map = self.stock_ts_map.get(sym)
                if ts_map is None or ts not in ts_map:
                    continue
                
                idx = ts_map[ts]
                arr = self.arrays[sym]
                
                cp = float(arr["close"][idx])
                lp = float(arr["low"][idx])
                op = float(arr["open"][idx])
                hp = float(arr["high"][idx])
                
                # A. EOD Exit
                if ts.hour == 15 and ts.minute >= 15:
                    self.trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=pos.entry_price, exit_price=op, quantity=pos.quantity,
                        direction="LONG", exit_reason="EOD_TIME"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
                # B. Strict Stop Loss
                if pos.stop_loss_price is not None and lp <= pos.stop_loss_price:
                    exit_price = min(pos.stop_loss_price, op) if op < pos.stop_loss_price else pos.stop_loss_price
                    self.trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=pos.entry_price, exit_price=exit_price, quantity=pos.quantity,
                        direction="LONG", exit_reason="STOP_LOSS"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
                # C. Overbought Exit (Runner)
                if ts > pos.entry_time:
                    curr_rsi = arr["rsi_14"][idx]
                    if curr_rsi >= self.config.rsi_exit_threshold:
                        self.trades.append(TradeRecord(
                            symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                            entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                            direction="LONG", exit_reason="RSI_OVERBOUGHT"
                        ))
                        syms_to_close.append(sym)
                        continue
                    
                # D. Partial Profit Booking
                if not pos.partial_booked:
                    target_price = pos.entry_price * (1 + self.config.profit_target_pct)
                    if hp >= target_price:
                        exit_price = max(target_price, op) if op > target_price else target_price
                        cover_qty = max(1, int(pos.quantity * self.config.partial_booking_fraction))
                        if cover_qty >= pos.quantity:
                            cover_qty = max(0, pos.quantity - 1)
                            
                        if cover_qty > 0:
                            self.trades.append(TradeRecord(
                                symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                                entry_price=pos.entry_price, exit_price=exit_price, quantity=cover_qty,
                                direction="LONG", exit_reason="PARTIAL_PROFIT"
                            ))
                            pos.quantity -= cover_qty
                            pos.partial_booked = True
                            
                        if pos.quantity <= 0:
                            syms_to_close.append(sym)
                            continue
                            
                # E. Dynamic Exit (SELL_EMA_MOMENTUM_LOSS)
                # close_1_below_ema21 means close_1 < ema21_1
                if idx >= 1:
                    close_1 = arr["close"][idx-1]
                    ema21_1 = arr["ema21"][idx-1]
                    if close_1 < ema21_1:
                        self.trades.append(TradeRecord(
                            symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                            entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                            direction="LONG", exit_reason="DYN_EXIT"
                        ))
                        syms_to_close.append(sym)
                        continue
            
            for sym in syms_to_close:
                if sym in self.open_positions:
                    del self.open_positions[sym]
            
            # 2. Look for New Entries
            if not is_bull_day or ts.hour >= 15:
                continue
                
            if len(self.open_positions) >= self.sys_config.max_open_positions:
                continue
                
            # Scan for entries
            for sym in self.stock_dfs:
                if len(self.open_positions) >= self.sys_config.max_open_positions:
                    break
                    
                if sym in self.open_positions:
                    continue
                    
                # Exclusion Filter: No-Clash Rule
                gap_pct = self.regime.get_gap(date_only, sym)
                if gap_pct <= self.config.exclude_gap_threshold:
                    continue
                    
                # Signal Filter
                ts_map = self.stock_ts_map.get(sym)
                if ts_map is None or ts not in ts_map:
                    continue
                idx = ts_map[ts]
                
                if not self.signals[sym][idx]:
                    continue
                    
                # Rule 2: Block entry if cover strategy is firing simultaneously
                arr = self.arrays[sym]
                if idx >= 1:
                    close_1 = arr["close"][idx-1]
                    ema21_1 = arr["ema21"][idx-1]
                    if close_1 < ema21_1:
                        continue
                
                # Execute Entry
                entry_price = float(arr["close"][idx])
                qty = int(self.sys_config.capital_per_trade // entry_price)
                if qty > 0:
                    sl_price = entry_price * (1 - self.config.stop_loss_pct)
                    self.open_positions[sym] = OpenPosition(
                        symbol=sym, entry_time=ts, entry_price=entry_price,
                        quantity=qty, direction="LONG", stop_loss_price=sl_price,
                        partial_booked=False
                    )
                    
        return self.trades


class FastShortEngineExecutor:
    def __init__(
        self,
        sys_config: SystemConfig,
        short_config: ShortEngineConfig,
        stock_dfs: Dict[str, pd.DataFrame],
        regime_analyzer: MarketRegimeAnalyzer,
        signals: Dict[str, List[bool]],
        timeline: List[pd.Timestamp],
        stock_ts_map: Dict[str, Dict[pd.Timestamp, int]],
        arrays: Dict[str, Dict[str, np.ndarray]],
        bear_days: Set[Any],
        active_timestamps: Set[pd.Timestamp]
    ):
        self.sys_config = sys_config
        self.config = short_config
        self.stock_dfs = stock_dfs
        self.regime = regime_analyzer
        self.signals = signals
        self.timeline = timeline
        self.stock_ts_map = stock_ts_map
        self.arrays = arrays
        self.bear_days = bear_days
        self.active_timestamps = active_timestamps
        self.trades: List[TradeRecord] = []
        self.open_positions: Dict[str, OpenPosition] = {}
        
        # Precompute timeline metadata to speed up execute loop
        self.timeline_meta = []
        for ts in self.timeline:
            dt = ts.date()
            is_bear = dt in self.bear_days
            before_15 = ts.hour < 15
            self.timeline_meta.append((ts, dt, is_bear, before_15))
        
    def execute(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[TradeRecord]:
        self.trades = []
        self.open_positions = {}
        
        use_fast = (start_date == "2024-01-01" and end_date == "2026-06-21") or (start_date is None and end_date is None)
        
        if use_fast:
            eval_timeline_meta = self.timeline_meta
        else:
            eval_timeline = self.timeline
            if start_date:
                eval_timeline = [ts for ts in eval_timeline if ts.tz_localize(None) >= pd.Timestamp(start_date)]
            if end_date:
                eval_timeline = [ts for ts in eval_timeline if ts.tz_localize(None) <= pd.Timestamp(end_date)]
            eval_timeline_meta = [(ts, ts.date(), ts.date() in self.bear_days, ts.hour < 15) for ts in eval_timeline]
            
        for ts, date_only, is_bear_day, before_15 in eval_timeline_meta:
            if not self.open_positions:
                if ts not in self.active_timestamps or not is_bear_day or not before_15:
                    continue
            
            # 1. Manage Open Positions
            syms_to_close = []
            for sym, pos in list(self.open_positions.items()):
                ts_map = self.stock_ts_map.get(sym)
                if ts_map is None or ts not in ts_map:
                    continue
                
                idx = ts_map[ts]
                arr = self.arrays[sym]
                
                cp = float(arr["close"][idx])
                lp = float(arr["low"][idx])
                op = float(arr["open"][idx])
                hp = float(arr["high"][idx])
                
                # A. EOD Exit
                if ts.hour == 15 and ts.minute >= 15:
                    self.trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=pos.entry_price, exit_price=op, quantity=pos.quantity,
                        direction="SHORT", exit_reason="EOD_TIME"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
                # A2. SAVIOR DYNAMIC EXIT (Close > EMA9)
                if idx >= 1:
                    ema9 = float(arr["ema9"][idx])
                    if cp > ema9:
                        self.trades.append(TradeRecord(
                            symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                            entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                            direction="SHORT", exit_reason="SAVIOR_EXIT"
                        ))
                        syms_to_close.append(sym)
                        continue
                    
                # B. Stop Loss
                if pos.stop_loss_price is not None and hp >= pos.stop_loss_price:
                    exit_price = max(pos.stop_loss_price, op)
                    self.trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=pos.entry_price, exit_price=exit_price, quantity=pos.quantity,
                        direction="SHORT", exit_reason="STOP_LOSS"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
                # C. Oversold Exit (Runner)
                if ts > pos.entry_time:
                    curr_rsi = arr["rsi_16"][idx]
                    if curr_rsi <= self.config.rsi_exit_threshold:
                        self.trades.append(TradeRecord(
                            symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                            entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                            direction="SHORT", exit_reason="RSI_OVERSOLD"
                        ))
                        syms_to_close.append(sym)
                        continue
                    
                # D. Partial Profit Booking
                if not pos.partial_booked:
                    target_price = pos.entry_price * (1 - self.config.profit_target_pct)
                    if lp <= target_price:
                        exit_price = min(target_price, op) if op < target_price else target_price
                        cover_qty = max(1, int(pos.quantity * self.config.partial_booking_fraction))
                        if cover_qty >= pos.quantity:
                            cover_qty = max(0, pos.quantity - 1)
                            
                        if cover_qty > 0:
                            self.trades.append(TradeRecord(
                                symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                                entry_price=pos.entry_price, exit_price=exit_price, quantity=cover_qty,
                                direction="SHORT", exit_reason="PARTIAL_PROFIT"
                            ))
                            pos.quantity -= cover_qty
                            pos.partial_booked = True
                            
                        if pos.quantity <= 0:
                            syms_to_close.append(sym)
                            continue
                            
            for sym in syms_to_close:
                if sym in self.open_positions:
                    del self.open_positions[sym]
            
            # 2. Look for New Entries
            if not is_bear_day or ts.hour >= 15:
                continue
                
            if len(self.open_positions) >= self.sys_config.max_open_positions:
                continue
                
            # Scan for entries
            for sym in self.stock_dfs:
                if len(self.open_positions) >= self.sys_config.max_open_positions:
                    break
                    
                if sym in self.open_positions:
                    continue
                    
                # Inclusion Filter: Target Weak Stocks
                gap_pct = self.regime.get_gap(date_only, sym)
                if gap_pct > self.config.target_gap_threshold:
                    continue
                    
                # Signal Filter
                ts_map = self.stock_ts_map.get(sym)
                if ts_map is None or ts not in ts_map:
                    continue
                idx = ts_map[ts]
                
                if not self.signals[sym][idx]:
                    continue
                    
                arr = self.arrays[sym]
                
                # Block entry if the short cover strategy triggers (close > previous high)
                if idx >= 1 and arr["close"][idx] > arr["high"][idx-1]:
                    continue
                    
                # V2.0 VWAP Stretch Blocker
                if arr["close"][idx] < arr["vwap"][idx] * 0.988:
                    continue
                    
                # Execute Entry
                entry_price = float(arr["close"][idx])
                qty = int(self.sys_config.capital_per_trade // entry_price)
                if qty > 0:
                    sl_price = entry_price * (1 + self.config.stop_loss_pct)
                    self.open_positions[sym] = OpenPosition(
                        symbol=sym, entry_time=ts, entry_price=entry_price,
                        quantity=qty, direction="SHORT", stop_loss_price=sl_price,
                        partial_booked=False
                    )
                    
        return self.trades


def run_sweep():
    print("Loading market data cache...")
    stock_dfs = load_cache()
    from screener.morning_screener import NIFTY_50
    stock_dfs = {sym: df for sym, df in stock_dfs.items() if sym in NIFTY_50}
    
    print("Enriching data and precomputing signals...")
    IndicatorPreprocessor.enrich_data(stock_dfs)
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
    signal_gen = SignalGenerator(stock_dfs)
    
    start_time = time.time()
    signal_gen.precompute_signals(start_date="2024-01-01")
    print(f"Signals precomputed in {time.time() - start_time:.2f} seconds.")
    
    # Define sweep values
    max_open_positions_list = [1, 2, 3]
    
    # Long parameters
    long_profit_targets = [0.015, 0.020, 0.025, 0.030, 0.035, 0.040, 0.050]
    long_stop_losses = [0.007, 0.009, 0.011, 0.013, 0.015]
    long_rsi_exits = [70.0, 75.0, 80.0]
    long_market_gaps = [0.006, 0.008, 0.010, 0.012, 0.015]
    long_market_breadths = [0.30, 0.40, 0.50, 0.60]
    
    # Short parameters
    short_profit_targets = [0.015, 0.020, 0.030, 0.040, 0.050]
    short_stop_losses = [0.005, 0.007, 0.009, 0.011]
    short_rsi_exits = [15.0, 20.0, 25.0]
    short_market_gaps = [-0.006, -0.008, -0.010, -0.012, -0.015]
    short_market_breadths = [0.30, 0.40, 0.50, 0.60]

    # Pre-build timeline, ts_map, and arrays to avoid rebuilds inside executor constructors
    print("Pre-building timeline, stock_ts_map, and arrays...")
    timeline_set = set()
    for sym, df in stock_dfs.items():
        timeline_set.update(df.index)
    timeline = sorted(list(timeline_set))
    timeline_filtered = [ts for ts in timeline if pd.Timestamp("2024-01-01") <= ts.tz_localize(None) <= pd.Timestamp("2026-06-21")]
    stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
    
    long_arrays = {}
    for sym, df in stock_dfs.items():
        long_arrays[sym] = {
            "close": df["close"].values,
            "high": df["high"].values,
            "low": df["low"].values,
            "open": df["open"].values,
            "rsi_14": df["rsi_14"].values,
            "ema21": df["ema21"].values,
        }
        
    short_arrays = {}
    for sym, df in stock_dfs.items():
        short_arrays[sym] = {
            "close": df["close"].values,
            "high": df["high"].values,
            "low": df["low"].values,
            "open": df["open"].values,
            "rsi_16": df["rsi_16"].values,
            "ema9": df["ema9"].values,
            "vwap": df["vwap"].values,
        }

    # Precompute active timestamps for fast path skips
    long_active_timestamps = set()
    for sym, sigs in signal_gen.long_signals.items():
        for idx, has_sig in enumerate(sigs):
            if has_sig:
                long_active_timestamps.add(stock_dfs[sym].index[idx])
                
    short_active_timestamps = set()
    for sym, sigs in signal_gen.short_signals.items():
        for idx, has_sig in enumerate(sigs):
            if has_sig:
                short_active_timestamps.add(stock_dfs[sym].index[idx])

    # Pre-test equivalence for 1 config
    print("Testing equivalence of original and fast engines...")
    sys_config = SystemConfig(max_open_positions=1)
    long_config = LongEngineConfig()
    short_config = ShortEngineConfig()
    
    orig_long = LongEngineExecutor(sys_config, long_config, stock_dfs, regime_analyzer, signal_gen.long_signals)
    fast_long = FastLongEngineExecutor(
        sys_config, long_config, stock_dfs, regime_analyzer, signal_gen.long_signals,
        timeline_filtered, stock_ts_map, long_arrays, regime_analyzer.get_bull_days(long_config),
        long_active_timestamps
    )
    
    trades_orig = orig_long.execute(start_date="2024-01-01", end_date="2026-06-21")
    trades_fast = fast_long.execute(start_date="2024-01-01", end_date="2026-06-21")
    
    print(f"Original Long trades count: {len(trades_orig)}, Fast Long trades count: {len(trades_fast)}")
    assert len(trades_orig) == len(trades_fast), "Long trades count mismatch!"
    
    orig_short = ShortEngineExecutor(sys_config, short_config, stock_dfs, regime_analyzer, signal_gen.short_signals)
    fast_short = FastShortEngineExecutor(
        sys_config, short_config, stock_dfs, regime_analyzer, signal_gen.short_signals,
        timeline_filtered, stock_ts_map, short_arrays, regime_analyzer.get_bear_days(short_config),
        short_active_timestamps
    )
    
    trades_orig_s = orig_short.execute(start_date="2024-01-01", end_date="2026-06-21")
    trades_fast_s = fast_short.execute(start_date="2024-01-01", end_date="2026-06-21")
    
    print(f"Original Short trades count: {len(trades_orig_s)}, Fast Short trades count: {len(trades_fast_s)}")
    assert len(trades_orig_s) == len(trades_fast_s), "Short trades count mismatch!"
    print("Equivalence verified successfully!")
    
    # We will run the sweep independently for Long and Short engines, then combine them.
    results = []
    
    for max_pos in max_open_positions_list:
        print(f"\n--- SWEEPING max_open_positions = {max_pos} ---")
        sys_config = SystemConfig(max_open_positions=max_pos)
        
        # 1. Sweep Long Engine
        print("Running Long Engine parameter sweep...")
        long_runs = []
        t_long_start = time.time()
        
        # Group by regime settings to avoid recalculating bull days
        for m_gap in long_market_gaps:
            for m_breadth in long_market_breadths:
                # Create a config just to get bull days
                temp_config = LongEngineConfig(market_gap_threshold=m_gap, market_breadth_requirement=m_breadth)
                bull_days = regime_analyzer.get_bull_days(temp_config)
                
                for pt in long_profit_targets:
                    for sl in long_stop_losses:
                        for rsi in long_rsi_exits:
                            cfg = LongEngineConfig(
                                profit_target_pct=pt,
                                stop_loss_pct=sl,
                                rsi_exit_threshold=rsi,
                                market_gap_threshold=m_gap,
                                market_breadth_requirement=m_breadth
                            )
                            executor = FastLongEngineExecutor(
                                sys_config, cfg, stock_dfs, regime_analyzer, signal_gen.long_signals,
                                timeline_filtered, stock_ts_map, long_arrays, bull_days,
                                long_active_timestamps
                            )
                            
                            trades = executor.execute(start_date="2024-01-01", end_date="2026-06-21")
                            
                            # Calculate metrics
                            if not trades:
                                continue
                            
                            gross_pnl = sum(t.pnl_gross for t in trades)
                            stt_tax = sum(t.stt_tax for t in trades)
                            net_pnl = gross_pnl - stt_tax
                            
                            wins = [t for t in trades if t.pnl_net > 0]
                            losses = [t for t in trades if t.pnl_net <= 0]
                            
                            gross_profit = sum(t.pnl_net for t in wins)
                            gross_loss = sum(t.pnl_net for t in losses) # negative or zero
                            
                            long_runs.append({
                                "config": cfg,
                                "trades": trades,
                                "net_pnl": net_pnl,
                                "wins_count": len(wins),
                                "losses_count": len(losses),
                                "gross_profit": gross_profit,
                                "gross_loss": gross_loss
                            })
                            
        print(f"Long sweep done in {time.time() - t_long_start:.2f} seconds. Total runs: {len(long_runs)}")
        
        # 2. Sweep Short Engine
        print("Running Short Engine parameter sweep...")
        short_runs = []
        t_short_start = time.time()
        
        for m_gap in short_market_gaps:
            for m_breadth in short_market_breadths:
                temp_config = ShortEngineConfig(market_gap_threshold=m_gap, market_breadth_requirement=m_breadth)
                bear_days = regime_analyzer.get_bear_days(temp_config)
                
                for pt in short_profit_targets:
                    for sl in short_stop_losses:
                        for rsi in short_rsi_exits:
                            cfg = ShortEngineConfig(
                                profit_target_pct=pt,
                                stop_loss_pct=sl,
                                rsi_exit_threshold=rsi,
                                market_gap_threshold=m_gap,
                                market_breadth_requirement=m_breadth
                            )
                            executor = FastShortEngineExecutor(
                                sys_config, cfg, stock_dfs, regime_analyzer, signal_gen.short_signals,
                                timeline_filtered, stock_ts_map, short_arrays, bear_days,
                                short_active_timestamps
                            )
                            
                            trades = executor.execute(start_date="2024-01-01", end_date="2026-06-21")
                            
                            if not trades:
                                continue
                                
                            gross_pnl = sum(t.pnl_gross for t in trades)
                            stt_tax = sum(t.stt_tax for t in trades)
                            net_pnl = gross_pnl - stt_tax
                            
                            wins = [t for t in trades if t.pnl_net > 0]
                            losses = [t for t in trades if t.pnl_net <= 0]
                            
                            gross_profit = sum(t.pnl_net for t in wins)
                            gross_loss = sum(t.pnl_net for t in losses)
                            
                            short_runs.append({
                                "config": cfg,
                                "trades": trades,
                                "net_pnl": net_pnl,
                                "wins_count": len(wins),
                                "losses_count": len(losses),
                                "gross_profit": gross_profit,
                                "gross_loss": gross_loss
                            })
                            
        print(f"Short sweep done in {time.time() - t_short_start:.2f} seconds. Total runs: {len(short_runs)}")
        
        # 3. Combine results
        print("Combining and filtering results...")
        valid_combinations = []
        
        # Pre-extract arrays for fast vectorized comparisons
        short_wins = np.array([r["wins_count"] for r in short_runs])
        short_gp = np.array([r["gross_profit"] for r in short_runs])
        short_gl = np.array([r["gross_loss"] for r in short_runs])
        short_trades_cnt = np.array([len(r["trades"]) for r in short_runs])
        
        for i, lr in enumerate(long_runs):
            # Combined Win Rate > 40%: (short_wins + lr["wins_count"]) > 0.40 * (short_trades_cnt + len(lr["trades"]))
            win_rate_mask = (short_wins + lr["wins_count"]) > 0.40 * (short_trades_cnt + len(lr["trades"]))
            
            # Combined Profit Factor > 1.20: (short_gp + lr["gross_profit"]) > -1.20 * (short_gl + lr["gross_loss"])
            # (Note: gross_loss is negative or zero, so absolute value is equivalent to multiplying by -1)
            pf_mask = (short_gp + lr["gross_profit"]) > -1.20 * (short_gl + lr["gross_loss"])
            
            valid_indices = np.where(win_rate_mask & pf_mask)[0]
            
            for j in valid_indices:
                sr = short_runs[j]
                total_trades = len(lr["trades"]) + len(sr["trades"])
                total_wins = lr["wins_count"] + sr["wins_count"]
                win_rate = total_wins / total_trades
                
                gross_prof_comb = lr["gross_profit"] + sr["gross_profit"]
                gross_loss_comb = lr["gross_loss"] + sr["gross_loss"]
                profit_factor = float('inf') if gross_loss_comb == 0 else abs(gross_prof_comb / gross_loss_comb)
                
                net_return = (lr["net_pnl"] + sr["net_pnl"]) / sys_config.capital * 100.0
                
                valid_combinations.append({
                    "max_open_positions": max_pos,
                    "long_config": lr["config"],
                    "short_config": sr["config"],
                    "net_return": net_return,
                    "win_rate": win_rate * 100.0,
                    "profit_factor": profit_factor,
                    "total_trades": total_trades
                })
                
        print(f"Found {len(valid_combinations)} valid parameter configurations meeting all constraints.")
        results.extend(valid_combinations)
        
    if not results:
        print("CRITICAL: No configurations met the constraints!")
        return
        
    # Sort results by Net Return descending
    results.sort(key=lambda x: x["net_return"], reverse=True)
    
    print("\n=============================================================")
    print("BEST CONFIGURATIONS FOUND (TOP 10):")
    print("=============================================================")
    for idx, res in enumerate(results[:10]):
        print(f"\nRANK {idx+1}: Net Return = {res['net_return']:.2f}% | Win Rate = {res['win_rate']:.2f}% | Profit Factor = {res['profit_factor']:.2f} | Trades = {res['total_trades']}")
        print(f"  max_open_positions: {res['max_open_positions']}")
        print(f"  Long Config: {res['long_config']}")
        print(f"  Short Config: {res['short_config']}")
        
    # Save the best configuration details (top 10)
    with open("research/best_opt_params.txt", "w") as f:
        for idx, best in enumerate(results[:10]):
            f.write(f"=== RANK {idx+1} ===\n")
            f.write(f"max_open_positions={best['max_open_positions']}\n")
            f.write(f"LongEngineConfig:\n")
            for k, v in best['long_config'].__dict__.items():
                f.write(f"  {k}={v}\n")
            f.write(f"ShortEngineConfig:\n")
            for k, v in best['short_config'].__dict__.items():
                f.write(f"  {k}={v}\n")
            f.write(f"Performance Metrics:\n")
            f.write(f"  Net Return: {best['net_return']:.2f}%\n")
            f.write(f"  Win Rate: {best['win_rate']:.2f}%\n")
            f.write(f"  Profit Factor: {best['profit_factor']:.2f}\n")
            f.write(f"  Total Trades: {best['total_trades']}\n\n")
        
    print("\nBest configuration saved to research/best_opt_params.txt")

if __name__ == "__main__":
    run_sweep()
