import sys
import os
import math
import logging
import datetime
import pickle
import argparse
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any

import pandas as pd
import numpy as np
import ta

# Ensure we can load local modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.build_cache import load_cache
from core.strategy_sets import load_strategy_sets, StrategySetDefinition
from core.strategy import StrategySetEvaluator, StrategyEvaluationContext, CONDITION_REGISTRY

# ==============================================================================
# ENTERPRISE LOGGING CONFIGURATION
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("MultiTimeframeRunner")

# ==============================================================================
# ENTERPRISE CONFIGURATION AND CONSTANTS
# ==============================================================================
@dataclass(frozen=True)
class SystemConfig:
    """Core System Settings for the Backtest Engine"""
    capital: float = 100000.0
    margin: float = 5.0
    max_open_positions: int = 1
    stt_percentage: float = 0.00035

    @property
    def buying_power(self) -> float:
        return self.capital * self.margin

    @property
    def capital_per_trade(self) -> float:
        return self.buying_power / self.max_open_positions

@dataclass(frozen=True)
class LongEngineConfig:
    """Long Engine Execution Parameters"""
    market_gap_threshold: float = 0.010       # >= 1.0% Gap Up
    market_breadth_requirement: float = 0.40  # >= 40% of universe
    exclude_gap_threshold: float = -0.008     # Exclude any stock with <= -0.8% Gap Down
    rsi_exit_threshold: float = 72.0          # Exit if RSI(14) >= 72.0
    profit_target_pct: float = 0.015          # Book partial at +1.5%
    partial_booking_fraction: float = 1.00    # 100% qty to cover
    stop_loss_pct: float = 0.008              # Flat 0.8% SL

@dataclass(frozen=True)
class ShortEngineConfig:
    """Short Engine Execution Parameters"""
    market_gap_threshold: float = -0.006      # <= -0.6% Gap Down
    market_breadth_requirement: float = 0.40  # >= 40% of universe
    target_gap_threshold: float = -0.008      # Target stocks with <= -0.8% Gap Down
    rsi_exit_threshold: float = 17.0          # Exit if RSI(16) <= 17.0
    profit_target_pct: float = 0.025          # Book partial at +2.5%
    partial_booking_fraction: float = 1.00    # 100% qty to cover
    stop_loss_pct: float = 0.005              # Flat 0.5% SL

# ==============================================================================
# DATA MODELS
# ==============================================================================
@dataclass
class TradeRecord:
    """Represents a fully executed trade segment (partial or full)"""
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: int
    direction: str  # 'LONG' or 'SHORT'
    exit_reason: str # 'PARTIAL', 'RSI', 'SL', 'EOD', 'DYN_SL'
    
    @property
    def pnl_gross(self) -> float:
        if self.direction == "LONG":
            return (self.exit_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - self.exit_price) * self.quantity

    @property
    def stt_tax(self) -> float:
        turnover = self.exit_price * self.quantity
        return turnover * 0.00035

    @property
    def pnl_net(self) -> float:
        return self.pnl_gross - self.stt_tax

@dataclass
class OpenPosition:
    """Tracks a currently open position in the market"""
    symbol: str
    entry_time: pd.Timestamp
    entry_price: float
    quantity: int
    direction: str
    stop_loss_price: Optional[float] = None
    partial_booked: bool = False

# ==============================================================================
# MARKET REGIME ANALYZER
# ==============================================================================
class MarketRegimeAnalyzer:
    """Calculates daily gap statistics to determine Bull/Bear regimes"""
    
    def __init__(self, stock_dfs: Dict[str, pd.DataFrame]):
        self.stock_dfs = stock_dfs
        self.all_daily_gaps: Dict[Tuple[datetime.date, str], float] = {}
        self.trading_dates: List[datetime.date] = []
        self._compute_gaps()

    def _compute_gaps(self):
        logger.info("Computing daily gaps across the entire universe...")
        first_opens = {}
        last_closes = {}
        
        for sym, df in self.stock_dfs.items():
            if "open" not in df.columns:
                continue
            daily_first = df.groupby(df.index.date).first()
            daily_last = df.groupby(df.index.date).last()
            first_opens[sym] = daily_first["open"]
            last_closes[sym] = daily_last["close"].shift(1)

        last_closes_df = pd.DataFrame(last_closes)
        first_opens_df = pd.DataFrame(first_opens)
        
        for sym in self.stock_dfs:
            if sym in last_closes_df and sym in first_opens_df:
                gaps = (first_opens_df[sym] - last_closes_df[sym]) / last_closes_df[sym]
                for d, gap_val in gaps.items():
                    if pd.notna(gap_val):
                        self.all_daily_gaps[(d, sym)] = float(gap_val)

        self.trading_dates = sorted(list(set(d for d, s in self.all_daily_gaps.keys())))
        logger.info(f"Gap computation complete. Discovered {len(self.trading_dates)} trading days.")

    def get_bull_days(self, config: LongEngineConfig) -> Set[datetime.date]:
        logger.info("Evaluating Bull Days for Long Engine...")
        bull_days = set()
        for curr_d in self.trading_dates:
            gaps = [g for (d, s), g in self.all_daily_gaps.items() if d == curr_d]
            if not gaps:
                continue
            qualified = sum(1 for g in gaps if g >= config.market_gap_threshold)
            ratio = qualified / len(gaps)
            if ratio >= config.market_breadth_requirement:
                bull_days.add(curr_d)
        logger.info(f"Discovered {len(bull_days)} valid Bull Days.")
        return bull_days

    def get_bear_days(self, config: ShortEngineConfig) -> Set[datetime.date]:
        logger.info("Evaluating Bear Days for Short Engine...")
        bear_days = set()
        for curr_d in self.trading_dates:
            gaps = [g for (d, s), g in self.all_daily_gaps.items() if d == curr_d]
            if not gaps:
                continue
            qualified = sum(1 for g in gaps if g <= config.market_gap_threshold)
            ratio = qualified / len(gaps)
            if ratio >= config.market_breadth_requirement:
                bear_days.add(curr_d)
        logger.info(f"Discovered {len(bear_days)} valid Bear Days.")
        return bear_days

    def get_gap(self, date: datetime.date, symbol: str) -> float:
        return self.all_daily_gaps.get((date, symbol), 0.0)

# ==============================================================================
# INDICATOR PREPROCESSOR
# ==============================================================================
class IndicatorPreprocessor:
    """Precomputes all technical indicators required for the strategies"""
    
    @staticmethod
    def enrich_data(stock_dfs: Dict[str, pd.DataFrame]):
        logger.info("Enriching market data with custom Technical Indicators...")
        for sym, df in stock_dfs.items():
            if len(df) == 0:
                continue
            # RSI 14 for Long
            df["rsi_14"] = ta.momentum.rsi(df["close"], window=14).fillna(50.0)
            # RSI 16 for Short
            df["rsi_16"] = ta.momentum.rsi(df["close"], window=16).fillna(50.0)
            # EMA21 recalculation to match simulation_runner behavior exactly
            df["ema21"] = ta.trend.ema_indicator(df["close"], window=21).bfill()
        logger.info("Indicator enrichment complete.")

# ==============================================================================
# SIGNAL GENERATOR
# ==============================================================================
class SignalGenerator:
    """Evaluates strategy JSON configurations over the entire dataset"""
    
    def __init__(self, stock_dfs: Dict[str, pd.DataFrame]):
        self.stock_dfs = stock_dfs
        self.evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
        
        config = load_strategy_sets()
        self.buy_set_long = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
        self.buy_set_short = next((s for s in config.buy_sets if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN"), None)
        
        if not self.buy_set_long:
            raise ValueError("Critical Error: BUY_STREAK_MOMENTUM_BREAKOUT strategy not found in configuration.")
        if not self.buy_set_short:
            raise ValueError("Critical Error: SHORT_STREAK_MOMENTUM_BREAKDOWN strategy not found in configuration.")
            
        self.long_signals: Dict[str, List[bool]] = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
        self.short_signals: Dict[str, List[bool]] = {sym: [False]*len(df) for sym, df in stock_dfs.items()}

    def precompute_signals(self):
        logger.info("Precomputing Entry Signals via StrategySetEvaluator. This may take a few minutes...")
        total_syms = len(self.stock_dfs)
        for idx, (sym, df) in enumerate(self.stock_dfs.items()):
            if idx % 10 == 0:
                logger.info(f"Processing signals for symbols {idx+1}/{total_syms}...")
                
            for i in range(10, len(df)):
                sliced = df.iloc[:i+1]
                # Evaluate Long
                c_le = self.evaluator._evaluate_conditions(
                    self.buy_set_long, 
                    StrategyEvaluationContext("buy", sliced, sliced, i+1)
                )
                if c_le and all(r.get("fired") for r in c_le):
                    self.long_signals[sym][i] = True
                
                # Evaluate Short
                c_se = self.evaluator._evaluate_conditions(
                    self.buy_set_short, 
                    StrategyEvaluationContext("buy", sliced, sliced, i+1)
                )
                if c_se and all(r.get("fired") for r in c_se):
                    self.short_signals[sym][i] = True
                    
        logger.info("Signal generation complete.")

# ==============================================================================
# LONG ENGINE EXECUTOR
# ==============================================================================
class LongEngineExecutor:
    """Executes trades based on Long Strategy rules"""
    
    def __init__(
        self, 
        sys_config: SystemConfig, 
        long_config: LongEngineConfig,
        stock_dfs: Dict[str, pd.DataFrame],
        regime_analyzer: MarketRegimeAnalyzer,
        signals: Dict[str, List[bool]]
    ):
        self.sys_config = sys_config
        self.config = long_config
        self.stock_dfs = stock_dfs
        self.regime = regime_analyzer
        self.signals = signals
        
        config_obj = load_strategy_sets()
        self.cover_set_def = next((s for s in config_obj.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)
        self.evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
        
        self.bull_days = self.regime.get_bull_days(self.config)
        self.trades: List[TradeRecord] = []
        self.open_positions: Dict[str, OpenPosition] = {}
        
        # Build master timeline
        timeline_set = set()
        for sym, df in stock_dfs.items():
            timeline_set.update(df.index)
        self.timeline = sorted(list(timeline_set))
        self.stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

    def execute(self) -> List[TradeRecord]:
        logger.info("Executing Long Engine Backtest...")
        
        for ts in self.timeline:
            date_only = ts.date()
            is_bull_day = date_only in self.bull_days
            
            # 1. Manage Open Positions
            self._manage_positions(ts)
            
            # 2. Look for New Entries
            if not is_bull_day or ts.hour >= 15:
                continue
                
            if len(self.open_positions) >= self.sys_config.max_open_positions:
                continue
                
            self._scan_for_entries(ts, date_only)
            
        logger.info(f"Long Engine Backtest Complete. Total Trades Generated: {len(self.trades)}")
        return self.trades

    def _manage_positions(self, ts: pd.Timestamp):
        syms_to_close = []
        
        for sym, pos in self.open_positions.items():
            if ts not in self.stock_ts_map.get(sym, {}):
                continue
                
            idx = self.stock_ts_map[sym][ts]
            df = self.stock_dfs[sym]
            current_candle = df.iloc[idx]
            
            cp = float(current_candle["close"])
            lp = float(current_candle["low"])
            op = float(current_candle["open"])
            hp = float(current_candle["high"])
            
            # A. EOD Exit
            if ts.hour == 15 and ts.minute >= 15:
                self.trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                    direction="LONG", exit_reason="EOD_TIME"
                ))
                syms_to_close.append(sym)
                continue
                
            # B. Strict Stop Loss (Flat Stop Loss)
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
                curr_rsi = df["rsi_14"].iloc[idx]
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
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
            cond = self.evaluator._evaluate_conditions(self.cover_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                if idx+1 < len(df): 
                    exit_price = float(df.iloc[idx+1]["open"])
                    exit_ts = df.index[idx+1]
                    self.trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=exit_ts,
                        entry_price=pos.entry_price, exit_price=exit_price, quantity=pos.quantity,
                        direction="LONG", exit_reason="DYN_EXIT"
                    ))
                    syms_to_close.append(sym)
                    continue
                        
        # Clean up closed positions
        for sym in syms_to_close:
            del self.open_positions[sym]

    def _scan_for_entries(self, ts: pd.Timestamp, date_only: datetime.date):
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
            idx = self.stock_ts_map.get(sym, {}).get(ts, -1)
            if idx == -1:
                continue
                
            if not self.signals[sym][idx]:
                continue
                
            # Rule 2: Block entry if cover strategy is firing simultaneously
            df = self.stock_dfs[sym]
            sliced = df.iloc[:idx+1]
            cover_ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
            cover_cond = self.evaluator._evaluate_conditions(self.cover_set_def, cover_ctx)
            if cover_cond and all(r.get("fired") for r in cover_cond):
                continue
                
            # Execute Entry on Next Candle Open
            if idx + 1 < len(df):
                next_candle = df.iloc[idx+1]
                entry_price = float(next_candle["open"])
                entry_time = df.index[idx+1]
                
                # Determine Quantity
                qty = int(self.sys_config.capital_per_trade // entry_price)
                if qty > 0:
                    sl_price = entry_price * (1 - self.config.stop_loss_pct)
                    self.open_positions[sym] = OpenPosition(
                        symbol=sym, entry_time=entry_time, entry_price=entry_price,
                        quantity=qty, direction="LONG", stop_loss_price=sl_price,
                        partial_booked=False
                    )

# ==============================================================================
# SHORT ENGINE EXECUTOR
# ==============================================================================
class ShortEngineExecutor:
    """Executes trades based on Short Strategy rules"""
    
    def __init__(
        self, 
        sys_config: SystemConfig, 
        short_config: ShortEngineConfig,
        stock_dfs: Dict[str, pd.DataFrame],
        regime_analyzer: MarketRegimeAnalyzer,
        signals: Dict[str, List[bool]]
    ):
        self.sys_config = sys_config
        self.config = short_config
        self.stock_dfs = stock_dfs
        self.regime = regime_analyzer
        self.signals = signals
        
        self.bear_days = self.regime.get_bear_days(self.config)
        self.trades: List[TradeRecord] = []
        self.open_positions: Dict[str, OpenPosition] = {}
        
        # Build master timeline
        timeline_set = set()
        for sym, df in stock_dfs.items():
            timeline_set.update(df.index)
        self.timeline = sorted(list(timeline_set))
        self.stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}

    def execute(self) -> List[TradeRecord]:
        logger.info("Executing Short Engine Backtest...")
        
        for ts in self.timeline:
            date_only = ts.date()
            is_bear_day = date_only in self.bear_days
            
            # 1. Manage Open Positions
            self._manage_positions(ts)
            
            # 2. Look for New Entries
            if not is_bear_day or ts.hour >= 15:
                continue
                
            if len(self.open_positions) >= self.sys_config.max_open_positions:
                continue
                
            self._scan_for_entries(ts, date_only)
            
        logger.info(f"Short Engine Backtest Complete. Total Trades Generated: {len(self.trades)}")
        return self.trades

    def _manage_positions(self, ts: pd.Timestamp):
        syms_to_close = []
        
        for sym, pos in self.open_positions.items():
            if ts not in self.stock_ts_map.get(sym, {}):
                continue
                
            idx = self.stock_ts_map[sym][ts]
            df = self.stock_dfs[sym]
            current_candle = df.iloc[idx]
            
            cp = float(current_candle["close"])
            lp = float(current_candle["low"])
            op = float(current_candle["open"])
            hp = float(current_candle["high"])
            
            # A. EOD Exit
            if ts.hour == 15 and ts.minute >= 15:
                self.trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                    direction="SHORT", exit_reason="EOD_TIME"
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
            if ts > pos.entry_time and idx >= 1:
                prev_rsi = df["rsi_16"].iloc[idx-1]
                if prev_rsi <= self.config.rsi_exit_threshold:
                    self.trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=pos.entry_price, exit_price=op, quantity=pos.quantity,
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
                        
        # Clean up closed positions
        for sym in syms_to_close:
            del self.open_positions[sym]

    def _scan_for_entries(self, ts: pd.Timestamp, date_only: datetime.date):
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
            idx = self.stock_ts_map.get(sym, {}).get(ts, -1)
            if idx == -1:
                continue
                
            if not self.signals[sym][idx]:
                continue
                
            df = self.stock_dfs[sym]
            
            # Block entry if the short cover strategy triggers (close > previous high)
            if idx >= 1 and df["close"].iloc[idx] > df["high"].iloc[idx-1]:
                continue
                
            # Execute Entry on Next Candle Open
            if idx + 1 < len(df):
                next_candle = df.iloc[idx+1]
                entry_price = float(next_candle["open"])
                entry_time = df.index[idx+1]
                
                # Determine Quantity
                qty = int(self.sys_config.capital_per_trade // entry_price)
                if qty > 0:
                    sl_price = entry_price * (1 + self.config.stop_loss_pct)
                    self.open_positions[sym] = OpenPosition(
                        symbol=sym, entry_time=entry_time, entry_price=entry_price,
                        quantity=qty, direction="SHORT", stop_loss_price=sl_price,
                        partial_booked=False
                    )

# ==============================================================================
# MULTI-TIMEFRAME COORDINATOR AND RUNNER
# ==============================================================================
class MultiTimeframeRunner:
    """Coordinates and runs backtests over 9 different target historical windows"""
    
    def __init__(
        self,
        sys_config: SystemConfig,
        long_config: LongEngineConfig,
        short_config: ShortEngineConfig,
        output_dir: str
    ):
        self.sys_config = sys_config
        self.long_config = long_config
        self.short_config = short_config
        self.output_dir = output_dir
        
        # Ensure output directory exists
        os.makedirs(output_dir, exist_ok=True)
        
    def run(self) -> Dict[str, Any]:
        logger.info("Initializing AlcoSoft Multi-Timeframe Runner...")
        
        # 1. Load Cache
        logger.info("Loading market data cache...")
        stock_dfs = load_cache()
        if not stock_dfs:
            raise RuntimeError("CRITICAL ERROR: Failed to load market data cache.")

        # Drop empty dataframes — they poison latest_ts (NaN) and downstream slicing
        empty_syms = [sym for sym, df in stock_dfs.items() if df.empty]
        if empty_syms:
            logger.warning(f"Skipping {len(empty_syms)} empty dataframes: {empty_syms}")
            stock_dfs = {sym: df for sym, df in stock_dfs.items() if not df.empty}
        if not stock_dfs:
            raise RuntimeError("CRITICAL ERROR: All cached dataframes are empty.")

        # Determine latest date in the cache
        latest_ts = max(df.index.max() for df in stock_dfs.values())
        latest_date = latest_ts.date()
        logger.info(f"Latest timestamp discovered in cache: {latest_ts} (Date: {latest_date})")
        
        # Get all unique trading days
        all_dates = set()
        for df in stock_dfs.values():
            all_dates.update(df.index.date)
        sorted_trading_days = sorted(list(all_dates))
        
        # Calculate the 9 windows
        windows = self.calculate_historical_windows(sorted_trading_days)
        
        # 2. Pre-process Indicators (on full cache for proper warm-up)
        IndicatorPreprocessor.enrich_data(stock_dfs)
        
        # 3. Analyze Market Regime (on full cache to calculate all gaps correctly)
        regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
        
        # 4. Generate Signals (on full cache)
        signal_gen = SignalGenerator(stock_dfs)
        signal_gen.precompute_signals()
        
        results = {}
        
        # 5. Loop over the target windows
        for window_id, (start_dt, end_dt) in windows.items():
            window_name = self.get_window_display_name(window_id, start_dt, end_dt)
            logger.info(f"========== RUNNING TIMEFRAME: {window_name} ({start_dt} to {end_dt}) ==========")
            
            # Slice the stock dataframes
            start_str = start_dt.strftime("%Y-%m-%d")
            end_str = end_dt.strftime("%Y-%m-%d")
            
            sliced_dfs = {}
            sliced_long_signals = {}
            sliced_short_signals = {}
            
            for sym, df in stock_dfs.items():
                sliced_df = df.loc[start_str:end_str]
                sliced_dfs[sym] = sliced_df
                
                if len(sliced_df) > 0:
                    # Retrieve index locations from the full dataframe to slice lists
                    start_ts_actual = sliced_df.index[0]
                    end_ts_actual = sliced_df.index[-1]
                    
                    start_idx = df.index.get_loc(start_ts_actual)
                    end_idx = df.index.get_loc(end_ts_actual)
                    
                    sliced_long_signals[sym] = signal_gen.long_signals[sym][start_idx:end_idx + 1]
                    sliced_short_signals[sym] = signal_gen.short_signals[sym][start_idx:end_idx + 1]
                else:
                    sliced_long_signals[sym] = []
                    sliced_short_signals[sym] = []
            
            # Execute Long Engine
            long_executor = LongEngineExecutor(
                sys_config=self.sys_config,
                long_config=self.long_config,
                stock_dfs=sliced_dfs,
                regime_analyzer=regime_analyzer,
                signals=sliced_long_signals
            )
            long_trades = long_executor.execute()
            
            # Execute Short Engine
            short_executor = ShortEngineExecutor(
                sys_config=self.sys_config,
                short_config=self.short_config,
                stock_dfs=sliced_dfs,
                regime_analyzer=regime_analyzer,
                signals=sliced_short_signals
            )
            short_trades = short_executor.execute()
            
            # Calculate metrics
            long_metrics = self.calculate_metrics_raw(long_trades, self.sys_config.capital)
            short_metrics = self.calculate_metrics_raw(short_trades, self.sys_config.capital)
            
            combined_trades = long_trades + short_trades
            combined_trades.sort(key=lambda t: t.entry_time)
            combined_metrics = self.calculate_metrics_raw(combined_trades, self.sys_config.capital)
            
            results[window_id] = {
                "display_name": window_name,
                "start_date": start_dt,
                "end_date": end_dt,
                "long_trades": long_trades,
                "short_trades": short_trades,
                "combined_trades": combined_trades,
                "long_metrics": long_metrics,
                "short_metrics": short_metrics,
                "combined_metrics": combined_metrics
            }
            
            logger.info(f"Timeframe {window_id} complete. Combined Trades: {len(combined_trades)} | Net Return: {combined_metrics['net_return_pct']:.2f}%")
            
        return {
            "results": results,
            "latest_date": latest_date,
            "latest_ts": latest_ts,
            "built_at": datetime.datetime.now()
        }
        
    def calculate_historical_windows(self, sorted_trading_days: List[date]) -> Dict[str, Tuple[date, date]]:
        """Calculates the 9 target historical windows relative to the latest date in the cache using Trading Days"""
        windows = {}
        if not sorted_trading_days:
            return windows
            
        latest_dt = sorted_trading_days[-1]
        
        def get_start_date_by_trading_days(days_back: int) -> date:
            if len(sorted_trading_days) <= days_back:
                return sorted_trading_days[0]
            return sorted_trading_days[-(days_back)]
            
        # 1. Past 60 days (Baseline)
        windows['past_60d'] = (get_start_date_by_trading_days(60), latest_dt)
        
        # 2. Past 50 days
        windows['past_50d'] = (get_start_date_by_trading_days(50), latest_dt)
        
        # 3. Past 40 days
        windows['past_40d'] = (get_start_date_by_trading_days(40), latest_dt)
        
        # 4. Past 30 days
        windows['past_30d'] = (get_start_date_by_trading_days(30), latest_dt)
        
        # 5. Past 20 days
        windows['past_20d'] = (get_start_date_by_trading_days(20), latest_dt)
        
        # 6. Past 2 weeks (14 days)
        windows['past_14d'] = (get_start_date_by_trading_days(14), latest_dt)
        
        # 7. Past 10 days
        windows['past_10d'] = (get_start_date_by_trading_days(10), latest_dt)
        
        # 8. Last Month Only (Dynamically determined based on latest_dt)
        if latest_dt.month == 1:
            prev_month = 12
            prev_year = latest_dt.year - 1
        else:
            prev_month = latest_dt.month - 1
            prev_year = latest_dt.year
            
        last_month_start = date(prev_year, prev_month, 1)
        current_month_start = date(latest_dt.year, latest_dt.month, 1)
        last_month_end = current_month_start - timedelta(days=1)
        windows['last_month'] = (last_month_start, last_month_end)
        
        # 9. This Month Only
        this_month_start = date(latest_dt.year, latest_dt.month, 1)
        windows['this_month'] = (this_month_start, latest_dt)
        
        return windows

    def get_window_display_name(self, window_id: str, start_dt: date, end_dt: date) -> str:
        """Helper to get clean user facing window names"""
        names = {
            'past_60d': "Past 60 days (Baseline)",
            'past_50d': "Past 50 days",
            'past_40d': "Past 40 days",
            'past_30d': "Past 30 days",
            'past_20d': "Past 20 days",
            'past_14d': "Past 2 weeks (14 days)",
            'past_10d': "Past 10 days",
            'last_month': f"Last Month Only ({start_dt.strftime('%B %Y')})",
            'this_month': f"This Month Only ({start_dt.strftime('%B %Y')} to Present)"
        }
        return names.get(window_id, f"Custom Timeframe ({start_dt} to {end_dt})")

    @staticmethod
    def calculate_metrics_raw(trades: List[TradeRecord], capital: float) -> Dict[str, Any]:
        """Calculates performance metrics as raw numeric floats for report formatting"""
        if not trades:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "gross_pnl": 0.0,
                "stt_tax": 0.0,
                "net_pnl": 0.0,
                "gross_return_pct": 0.0,
                "stt_impact_pct": 0.0,
                "net_return_pct": 0.0,
                "profit_factor": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "expectancy": 0.0
            }
            
        gross_pnl = sum(t.pnl_gross for t in trades)
        stt_tax = sum(t.stt_tax for t in trades)
        net_pnl = gross_pnl - stt_tax
        
        wins = [t for t in trades if t.pnl_net > 0]
        losses = [t for t in trades if t.pnl_net <= 0]
        
        win_rate = (len(wins) / len(trades)) * 100 if trades else 0.0
        
        avg_win = sum(t.pnl_net for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.pnl_net for t in losses) / len(losses) if losses else 0.0
        
        sum_win_pnl = sum(t.pnl_net for t in wins)
        sum_loss_pnl = sum(t.pnl_net for t in losses)
        
        profit_factor = abs(sum_win_pnl / sum_loss_pnl) if sum_loss_pnl != 0 else float('inf')
        
        expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)
        
        return {
            "total_trades": len(trades),
            "win_rate": win_rate,
            "gross_pnl": gross_pnl,
            "stt_tax": stt_tax,
            "net_pnl": net_pnl,
            "gross_return_pct": (gross_pnl / capital * 100),
            "stt_impact_pct": (-stt_tax / capital * 100),
            "net_return_pct": (net_pnl / capital * 100),
            "profit_factor": profit_factor,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy": expectancy
        }

# ==============================================================================
# MULTI-TIMEFRAME ENTERPRISE REPORTING ENGINE
# ==============================================================================
class MultiTimeframeReportingEngine:
    """Generates consolidated markdown reports and individual markdown reports per window"""
    
    def __init__(self, run_results: Dict[str, Any], capital: float, output_dir: str):
        self.results = run_results["results"]
        self.latest_date = run_results["latest_date"]
        self.latest_ts = run_results["latest_ts"]
        self.built_at = run_results["built_at"]
        self.capital = capital
        self.output_dir = output_dir
        
    def generate_reports(self):
        logger.info("Generating Multi-Timeframe performance reports...")
        
        # Ensure output directory exists
        os.makedirs(self.output_dir, exist_ok=True)
        
        # 1. Generate individual markdown reports
        for window_id, run_data in self.results.items():
            self._write_individual_report(window_id, run_data)
            
        # 2. Generate consolidated markdown summary report
        self._write_markdown_summary()
        
        # 3. Print a beautiful console comparison table
        self._print_console_summary()

    def _write_individual_report(self, window_id: str, run_data: Dict[str, Any]):
        filename = f"report_{window_id}.md"
        out_path = os.path.join(self.output_dir, filename)
        
        long_trades = run_data["long_trades"]
        short_trades = run_data["short_trades"]
        combined_trades = run_data["combined_trades"]
        
        lm = run_data["long_metrics"]
        sm = run_data["short_metrics"]
        cm = run_data["combined_metrics"]
        
        def fmt_pct(val):
            return f"{val:.2f}%"
        
        def fmt_curr(val):
            if val < 0:
                return f"-₹{abs(val):,.2f}"
            return f"₹{val:,.2f}"
            
        def fmt_pf(val):
            if val == float('inf') or math.isinf(val):
                return "∞"
            return f"{val:.2f}"
            
        def fmt_expectancy(val):
            return f"₹{val:.2f}"

        with open(out_path, "w", encoding="utf-8") as f:
            # Header
            f.write(f"# AlcoSoft Dual-Engine Performance Report - {run_data['display_name']}\n\n")
            
            # Metadata
            f.write("## Metadata\n")
            f.write(f"- **Start Date**: {run_data['start_date'].strftime('%Y-%m-%d')}\n")
            f.write(f"- **End Date**: {run_data['end_date'].strftime('%Y-%m-%d')}\n")
            f.write(f"- **Initial Capital**: ₹{self.capital:,.2f}\n\n")
            
            # Summary Metrics Table
            f.write("## Summary Metrics Table\n\n")
            f.write("| Metric | Combined Portfolio | Long Engine | Short Engine |\n")
            f.write("| :--- | :--- | :--- | :--- |\n")
            
            metrics_keys = [
                ("Total Trades", "total_trades", int, ""),
                ("Win Rate", "win_rate", fmt_pct, ""),
                ("Gross Return %", "gross_return_pct", fmt_pct, ""),
                ("STT Tax Paid", "stt_tax", fmt_curr, ""),
                ("Net Return %", "net_return_pct", fmt_pct, ""),
                ("Profit Factor", "profit_factor", fmt_pf, ""),
                ("Average Win", "avg_win", fmt_curr, ""),
                ("Average Loss", "avg_loss", fmt_curr, ""),
                ("Expectancy", "expectancy", fmt_expectancy, "")
            ]
            
            for label, key, formatter, suffix in metrics_keys:
                if key == "total_trades":
                    val_c = str(cm[key])
                    val_l = str(lm[key])
                    val_s = str(sm[key])
                else:
                    val_c = formatter(cm[key])
                    val_l = formatter(lm[key])
                    val_s = formatter(sm[key])
                f.write(f"| {label} | {val_c} | {val_l} | {val_s} |\n")
                
            f.write("\n")
            
            # Chronological Trade Ledger
            f.write("## Chronological Trade Ledger\n\n")
            f.write("| Symbol | Direction | Entry Time | Exit Time | Quantity | Entry Price | Exit Price | Exit Reason | Net PnL |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
            
            for t in combined_trades:
                entry_time_str = t.entry_time.strftime("%Y-%m-%d %H:%M")
                exit_time_str = t.exit_time.strftime("%Y-%m-%d %H:%M")
                f.write(f"| {t.symbol} | {t.direction} | {entry_time_str} | {exit_time_str} | {t.quantity} | {fmt_curr(t.entry_price)} | {fmt_curr(t.exit_price)} | {t.exit_reason} | {fmt_curr(t.pnl_net)} |\n")
                
        logger.info(f"Individual report written for window {window_id} -> {out_path}")

    def _write_markdown_summary(self):
        out_path = os.path.join(self.output_dir, "multi_timeframe_summary.md")
        
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("# 🏛️ ALCOSOFT FINANCIAL SERVICES - MULTI-TIMEFRAME PERFORMANCE REPORT 🏛️\n\n")
            f.write(f"**Report Generated**: {self.built_at.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**Cache Latest Timestamp**: {self.latest_ts} (Date: {self.latest_date})\n")
            f.write(f"**Initial Capital Allocation**: ₹{self.capital:,.2f}\n\n")
            
            f.write("## 📊 Consolidated Portfolio Performance Summary\n")
            f.write("| Historical Timeframe Window | Start Date | End Date | Net PnL (₹) | Net Return | Win Rate | Total Trades | Profit Factor | Expectancy | Long Trades | Short Trades |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
            
            for w_id in self._ordered_window_ids():
                data = self.results[w_id]
                cm = data["combined_metrics"]
                lm = data["long_metrics"]
                sm = data["short_metrics"]
                pf_str = f"{cm['profit_factor']:.2f}" if cm['profit_factor'] != float('inf') else "N/A"
                
                f.write(f"| **{data['display_name']}** "
                        f"| {data['start_date']} "
                        f"| {data['end_date']} "
                        f"| ₹{cm['net_pnl']:,.2f} "
                        f"| {cm['net_return_pct']:.2f}% "
                        f"| {cm['win_rate']:.2f}% "
                        f"| {cm['total_trades']} "
                        f"| {pf_str} "
                        f"| ₹{cm['expectancy']:.2f} "
                        f"| {lm['total_trades']} "
                        f"| {sm['total_trades']} |\n")
            
            f.write("\n---\n\n")
            
            f.write("## 🟢 Long Engine (Bull Market Assassin) Breakdown\n")
            f.write("| Historical Timeframe Window | Net PnL (₹) | Net Return | Win Rate | Total Trades | Average Win | Average Loss | Expectancy |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
            
            for w_id in self._ordered_window_ids():
                data = self.results[w_id]
                lm = data["long_metrics"]
                f.write(f"| **{data['display_name']}** "
                        f"| ₹{lm['net_pnl']:,.2f} "
                        f"| {lm['net_return_pct']:.2f}% "
                        f"| {lm['win_rate']:.2f}% "
                        f"| {lm['total_trades']} "
                        f"| ₹{lm['avg_win']:.2f} "
                        f"| ₹{lm['avg_loss']:.2f} "
                        f"| ₹{lm['expectancy']:.2f} |\n")
                        
            f.write("\n---\n\n")
            
            f.write("## 🔴 Short Engine (Bear Market Assassin) Breakdown\n")
            f.write("| Historical Timeframe Window | Net PnL (₹) | Net Return | Win Rate | Total Trades | Average Win | Average Loss | Expectancy |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
            
            for w_id in self._ordered_window_ids():
                data = self.results[w_id]
                sm = data["short_metrics"]
                f.write(f"| **{data['display_name']}** "
                        f"| ₹{sm['net_pnl']:,.2f} "
                        f"| {sm['net_return_pct']:.2f}% "
                        f"| {sm['win_rate']:.2f}% "
                        f"| {sm['total_trades']} "
                        f"| ₹{sm['avg_win']:.2f} "
                        f"| ₹{sm['avg_loss']:.2f} "
                        f"| ₹{sm['expectancy']:.2f} |\n")
                        
            f.write("\n---\n\n")
            f.write("## 🔍 Detailed Window Analysis & Observations\n")
            f.write("### 1. Baseline Performance (Past 60 Days)\n")
            f.write("The 60-day baseline represents the maximum lookback period stored in the cache. It covers multiple market regimes and acts as the anchor benchmark for strategy consistency.\n\n")
            f.write("### 2. Sensitivity Analysis (50d down to 10d)\n")
            f.write("Shorter lookbacks demonstrate how the system responds to near-term regime shifts. If the net return drops significantly or goes negative in shorter windows, it indicates the strategy may be struggling in the current market micro-regime.\n\n")
            f.write("### 3. Last Month vs This Month Regime Transition\n")
            f.write("Analyzing **Last Month Only** vs **This Month Only** helps isolate performance across separate calendar regimes, allowing risk managers to identify if changes in volatility or market direction are impacting profit generation.\n")
            
        logger.info(f"Summary markdown written to {out_path}")

    def _print_console_summary(self):
        print("\n" + "="*95)
        print("=== MULTI-TIMEFRAME PORTFOLIO PERFORMANCE SUMMARY COMPARISON ===")
        print("="*95)
        header = f"{'Timeframe Window':<30} | {'Start':<10} | {'End':<10} | {'Net Return':<10} | {'Win Rate':<8} | {'Trades':<6} | {'Profit F.':<9}"
        print(header)
        print("-" * 95)
        
        for w_id in self._ordered_window_ids():
            data = self.results[w_id]
            cm = data["combined_metrics"]
            pf_str = f"{cm['profit_factor']:.2f}" if cm['profit_factor'] != float('inf') else "N/A"
            print(f"{data['display_name']:<30} | {data['start_date']} | {data['end_date']} | {cm['net_return_pct']:>9.2f}% | {cm['win_rate']:>7.2f}% | {cm['total_trades']:>6} | {pf_str:>9}")
        print("="*95 + "\n")

    def _ordered_window_ids(self) -> List[str]:
        return [
            'past_60d', 'past_50d', 'past_40d', 'past_30d', 'past_20d',
            'past_14d', 'past_10d', 'last_month', 'this_month'
        ]

# ==============================================================================
# MAIN EXECUTION ROUTINE
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="AlcoSoft Multi-Timeframe Backtest Runner")
    parser.add_argument("--capital", type=float, default=100000.0, help="Initial portfolio capital")
    parser.add_argument("--margin", type=float, default=5.0, help="Margin leverage multiplier")
    parser.add_argument("--max-positions", type=int, default=1, help="Max simultaneous open positions")
    parser.add_argument("--stt", type=float, default=0.00035, help="STT tax percentage (e.g. 0.00035 for 0.035%)")
    parser.add_argument("--output-dir", type=str, default="research/timeframe_reports", help="Directory to save report outputs")
    
    args = parser.parse_args()
    
    # Initialize Configs
    sys_config = SystemConfig(
        capital=args.capital,
        margin=args.margin,
        max_open_positions=args.max_positions,
        stt_percentage=args.stt
    )
    long_config = LongEngineConfig()
    short_config = ShortEngineConfig()
    
    # Run backtests
    runner = MultiTimeframeRunner(
        sys_config=sys_config,
        long_config=long_config,
        short_config=short_config,
        output_dir=args.output_dir
    )
    
    try:
        run_results = runner.run()
        
        # Report findings
        reporting_engine = MultiTimeframeReportingEngine(
            run_results=run_results,
            capital=args.capital,
            output_dir=args.output_dir
        )
        reporting_engine.generate_reports()
        
    except Exception as e:
        logger.exception(f"Execution failed with error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
