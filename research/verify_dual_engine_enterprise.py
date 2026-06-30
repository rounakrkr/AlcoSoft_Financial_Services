import sys
import os
import argparse
import math
import logging
import datetime
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
logger = logging.getLogger("DualEngineVerifier")

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
        # For intraday equity, STT is applied on the sell side turnover at 0.035%
        # Technically in Short it's on entry, in Long it's on exit, but we simplify to 1 side turnover.
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
            df["rsi"] = df["rsi_14"]  # Entry conditions expect 'rsi'
            # RSI 16 for Short
            df["rsi_16"] = ta.momentum.rsi(df["close"], window=16).fillna(50.0)
            # EMA calculations
            df["ema21"] = ta.trend.ema_indicator(df["close"], window=21).ffill().fillna(0)
            df["ema20"] = ta.trend.ema_indicator(df["close"], window=20).ffill().fillna(0)
            df["ema9"] = ta.trend.ema_indicator(df["close"], window=9).ffill().fillna(0)
            
            # Intraday VWAP
            typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
            df["vwap"] = (typical_price * df["volume"]).groupby(df.index.date).cumsum() / df["volume"].groupby(df.index.date).cumsum()
            df["vwap"] = df["vwap"].ffill().fillna(0)
            
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

    def precompute_signals(self, start_date=None):
        logger.info("Precomputing Entry Signals via StrategySetEvaluator. This may take a few minutes...")
        total_syms = len(self.stock_dfs)
        
        target_start = None
        if start_date:
            target_start = pd.to_datetime(start_date).date()
            
        for idx, (sym, df) in enumerate(self.stock_dfs.items()):
            if idx % 10 == 0:
                logger.info(f"Processing symbols {idx+1}/{total_syms}...")
                
            loop_start = 10
            if target_start:
                future_dates = df.index.date >= target_start
                if future_dates.any():
                    loop_start = max(10, future_dates.argmax())
                else:
                    continue
                    
            for i in range(loop_start, len(df)):
                # Fix O(N^2) bottleneck by capping the slice to max 100 candles (strategy lookback is small)
                start_idx = max(0, i-100)
                sliced = df.iloc[start_idx:i+1]
                
                # Evaluate Long
                c_le = self.evaluator._evaluate_conditions(
                    self.buy_set_long, 
                    StrategyEvaluationContext("buy", sliced, sliced, len(sliced))
                )
                if c_le and all(r.get("fired") for r in c_le):
                    self.long_signals[sym][i] = True
                
                # Evaluate Short
                c_se = self.evaluator._evaluate_conditions(
                    self.buy_set_short, 
                    StrategyEvaluationContext("buy", sliced, sliced, len(sliced))
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

    def execute(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[TradeRecord]:
        logger.info("Executing Long Engine Backtest...")
        
        # Filter timeline
        eval_timeline = self.timeline
        if start_date:
            eval_timeline = [ts for ts in eval_timeline if ts.tz_localize(None) >= pd.Timestamp(start_date)]
        if end_date:
            eval_timeline = [ts for ts in eval_timeline if ts.tz_localize(None) <= pd.Timestamp(end_date)]
            
        for ts in eval_timeline:
            date_only = ts.date()
            is_bull_day = date_only in self.bull_days
            
            # 1. Manage Open Positions
            self._manage_positions(ts)
            
            # 2. Look for New Entries (only on Bull Days)
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
                    entry_price=pos.entry_price, exit_price=op, quantity=pos.quantity,
                    direction="LONG", exit_reason="EOD_TIME"
                ))
                syms_to_close.append(sym)
                continue
                
            # B. Strict Stop Loss (Flat 1.0%)
            if pos.stop_loss_price is not None and lp <= pos.stop_loss_price:
                # Execution price is the worse of Open or Stop Loss Level
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
            # Live system places sell order immediately at current LTP when signal fires
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
            cond = self.evaluator._evaluate_conditions(self.cover_set_def, ctx)
            if cond and all(r.get("fired") for r in cond):
                exit_price = cp  # current candle close — matches live system LTP at signal fire
                self.trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
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
                continue # Handed over to Short Engine
                
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
                continue # Blocked by Rule 2
                
            # Execute Entry at Current Candle Close (matches live system: entry at WebSocket LTP when signal fires)
            df = self.stock_dfs[sym]
            current_candle = df.iloc[idx]
            entry_price = float(current_candle["close"])
            entry_time = ts
            
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

    def execute(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[TradeRecord]:
        logger.info("Executing Short Engine Backtest...")
        
        # Filter timeline
        eval_timeline = self.timeline
        if start_date:
            eval_timeline = [ts for ts in eval_timeline if ts.tz_localize(None) >= pd.Timestamp(start_date)]
        if end_date:
            eval_timeline = [ts for ts in eval_timeline if ts.tz_localize(None) <= pd.Timestamp(end_date)]
            
        for ts in eval_timeline:
            date_only = ts.date()
            is_bear_day = date_only in self.bear_days
            
            # 1. Manage Open Positions
            self._manage_positions(ts)
            
            # 2. Look for New Entries (only on Bear Days)
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
                    entry_price=pos.entry_price, exit_price=op, quantity=pos.quantity,
                    direction="SHORT", exit_reason="EOD_TIME"
                ))
                syms_to_close.append(sym)
                continue
                
            # A2. SAVIOR DYNAMIC EXIT (Close > EMA9)
            if idx >= 1:
                ema9 = float(df["ema9"].iloc[idx])
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
                curr_rsi = df["rsi_16"].iloc[idx]
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
                continue # Not weak enough
                
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
                
            # V2.0 VWAP Stretch Blocker: Block entry if price is dangerously extended (> 1.2%) below VWAP
            if df["close"].iloc[idx] < df["vwap"].iloc[idx] * 0.988:
                continue
                
            # Execute Entry at Current Candle Close (matches live system: entry at WebSocket LTP when signal fires)
            df = self.stock_dfs[sym]
            current_candle = df.iloc[idx]
            entry_price = float(current_candle["close"])
            entry_time = ts
            
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
# ENTERPRISE REPORTING ENGINE
# ==============================================================================
class ReportingEngine:
    """Generates detailed enterprise-level performance metrics and tearsheets"""
    
    @staticmethod
    def _calculate_advanced_metrics(trades: List[TradeRecord], stock_dfs: Dict[str, pd.DataFrame]) -> List[Dict]:
        if not trades or not stock_dfs:
            return []
        
        grouped = {}
        for t in trades:
            if t.exit_reason not in grouped:
                grouped[t.exit_reason] = []
            grouped[t.exit_reason].append(t)
            
        metrics = []
        for reason, g_trades in grouped.items():
            count = len(g_trades)
            wins = len([t for t in g_trades if t.pnl_net > 0])
            win_rate = (wins / count) * 100
            net_pnl = sum(t.pnl_net for t in g_trades)
            
            missed_profits = []
            saved_losses = []
            
            for t in g_trades:
                df = stock_dfs.get(t.symbol)
                if df is None: continue
                try:
                    # Filter same day, strictly AFTER exit_time to avoid counting intra-candle noise
                    day_data = df.loc[(df.index.date == t.exit_time.date()) & (df.index > t.exit_time)]
                    if day_data.empty: continue
                    
                    max_high = day_data['high'].max()
                    min_low = day_data['low'].min()
                    
                    if t.direction == "LONG":
                        missed_profit = max_high - t.exit_price
                        saved_loss = t.exit_price - min_low
                    else:
                        missed_profit = t.exit_price - min_low
                        saved_loss = max_high - t.exit_price
                        
                    missed_profits.append(max(0, missed_profit))
                    saved_losses.append(max(0, saved_loss))
                except Exception:
                    continue
                    
            avg_missed = sum(missed_profits)/len(missed_profits) if missed_profits else 0.0
            avg_saved = sum(saved_losses)/len(saved_losses) if saved_losses else 0.0
            
            metrics.append({
                "Reason": reason,
                "Count": count,
                "Win Rate": f"{win_rate:.1f}%",
                "Net PnL": f"₹{net_pnl:.2f}",
                "Avg Missed (Fav)": f"₹{avg_missed:.2f}",
                "Avg Saved (Adv)": f"₹{avg_saved:.2f}"
            })
            
        metrics.sort(key=lambda x: float(x["Net PnL"].replace("₹", "")), reverse=True)
        return metrics

    @staticmethod
    def _calculate_metrics(trades: List[TradeRecord], capital: float) -> Dict[str, Any]:
        if not trades:
            return {}
            
        gross_pnl = sum(t.pnl_gross for t in trades)
        stt_tax = sum(t.stt_tax for t in trades)
        net_pnl = gross_pnl - stt_tax
        
        wins = [t for t in trades if t.pnl_net > 0]
        losses = [t for t in trades if t.pnl_net <= 0]
        
        win_rate = (len(wins) / len(trades)) * 100 if trades else 0.0
        
        avg_win = sum(t.pnl_net for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.pnl_net for t in losses) / len(losses) if losses else 0.0
        
        profit_factor = abs(sum(t.pnl_net for t in wins) / sum(t.pnl_net for t in losses)) if sum(t.pnl_net for t in losses) != 0 else float('inf')
        
        expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)
        
        return {
            "Total Trades": len(trades),
            "Win Rate": f"{win_rate:.2f}%",
            "Gross Return": f"{(gross_pnl / capital * 100):.2f}%",
            "STT Impact": f"{(-stt_tax / capital * 100):.2f}%",
            "Net Return": f"{(net_pnl / capital * 100):.2f}%",
            "Profit Factor": f"{profit_factor:.2f}",
            "Average Win": f"₹{avg_win:.2f}",
            "Average Loss": f"₹{avg_loss:.2f}",
            "Expectancy": f"₹{expectancy:.2f} per trade"
        }

    @staticmethod
    def print_tearsheet(long_trades: List[TradeRecord], short_trades: List[TradeRecord], capital: float, stock_dfs: Dict[str, pd.DataFrame] = None, out_path: str = None):
        logger.info("Generating Final Enterprise Tearsheet & Advanced Analytics...")
        
        if out_path is None:
            out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "advanced_analytics_report.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("# 🏛️ ALCOSOFT FINANCIAL SERVICES - ADVANCED ANALYTICS REPORT 🏛️\n\n")
            
            def _write_advanced_table(metrics_list):
                if not metrics_list:
                    f.write("*No data available.*\n\n")
                    return
                f.write("| Exit Reason | Count | Win Rate | Net PnL | Missed Profit (Fav) | Saved Loss (Adv) |\n")
                f.write("|---|---|---|---|---|---|\n")
                for m in metrics_list:
                    f.write(f"| {m['Reason']} | {m['Count']} | {m['Win Rate']} | {m['Net PnL']} | {m['Avg Missed (Fav)']} | {m['Avg Saved (Adv)']} |\n")
                f.write("\n")

            def _write_monthly_table(trades_list):
                if not trades_list:
                    return
                monthly_groups = {}
                for t in trades_list:
                    m = t.entry_time.strftime("%B %Y")
                    m_key = t.entry_time.strftime("%Y-%m")
                    if m_key not in monthly_groups:
                        monthly_groups[m_key] = {"label": m, "trades": []}
                    monthly_groups[m_key]["trades"].append(t)
                    
                sorted_keys = sorted(monthly_groups.keys())
                f.write("### Monthly Breakdown\n")
                f.write("| Month | Trades | Win Rate | Gross Return | Net Return | Profit Factor |\n")
                f.write("|---|---|---|---|---|---|\n")
                
                for k in sorted_keys:
                    g = monthly_groups[k]
                    m = ReportingEngine._calculate_metrics(g["trades"], capital)
                    if m:
                        f.write(f"| {g['label']} | {m['Total Trades']} | {m['Win Rate']} | {m['Gross Return']} | {m['Net Return']} | {m['Profit Factor']} |\n")
                f.write("\n")

            long_metrics = ReportingEngine._calculate_metrics(long_trades, capital)
            f.write("## 🟢 THE BULL MARKET ASSASSIN (LONG ENGINE)\n\n### Overall Metrics\n")
            if long_metrics:
                for k, v in long_metrics.items():
                    f.write(f"- **{k}**: {v}\n")
                f.write("\n")
                _write_monthly_table(long_trades)
                f.write("### Post-Exit Excursion Analysis (By Reason)\n")
                _write_advanced_table(ReportingEngine._calculate_advanced_metrics(long_trades, stock_dfs))
            else:
                f.write("No Long trades recorded.\n\n")
                
            short_metrics = ReportingEngine._calculate_metrics(short_trades, capital)
            f.write("## 🔴 THE BEAR MARKET ASSASSIN (SHORT ENGINE)\n\n### Overall Metrics\n")
            if short_metrics:
                for k, v in short_metrics.items():
                    f.write(f"- **{k}**: {v}\n")
                f.write("\n")
                _write_monthly_table(short_trades)
                f.write("### Post-Exit Excursion Analysis (By Reason)\n")
                _write_advanced_table(ReportingEngine._calculate_advanced_metrics(short_trades, stock_dfs))
            else:
                f.write("No Short trades recorded.\n\n")
                
            combined_trades = long_trades + short_trades
            combined_trades.sort(key=lambda t: t.entry_time)
            combined_metrics = ReportingEngine._calculate_metrics(combined_trades, capital)
            
            f.write("## 🔥 DUAL-ENGINE PORTFOLIO (COMBINED NET PERFORMANCE)\n\n")
            if combined_metrics:
                for k, v in combined_metrics.items():
                    f.write(f"- **{k}**: {v}\n")
            else:
                f.write("No trades recorded in portfolio.\n\n")
                
        logger.info(f"Advanced Analytics Report written to {out_path}")

# ==============================================================================
# MAIN EXECUTION ROUTINE
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Run Dual Engine Backtest")
    parser.add_argument("--start-date", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--symbols", type=str, help="Comma-separated list of symbols to include")
    args = parser.parse_args()

    logger.info("Initializing AlcoSoft Dual-Engine Verification Framework...")
    
    sys_config = SystemConfig()
    long_config = LongEngineConfig()
    short_config = ShortEngineConfig()
    
    # 1. Load Data
    logger.info("Loading market data cache...")
    stock_dfs = load_cache()
    from screener.morning_screener import NIFTY_50
    stock_dfs = {sym: df for sym, df in stock_dfs.items() if sym in NIFTY_50}
    
    if not stock_dfs:
        logger.error("Failed to load market data cache. Exiting.")
        return
        
    if args.symbols:
        allowed = set(s.strip().upper() for s in args.symbols.split(","))
        stock_dfs = {k: v for k, v in stock_dfs.items() if k in allowed}
        logger.info(f"Filtered to {len(stock_dfs)} symbols based on input.")
        
    # 2. Enrich Data
    IndicatorPreprocessor.enrich_data(stock_dfs)
    
    # 3. Analyze Market Regime
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
    
    # 4. Generate Signals
    signal_gen = SignalGenerator(stock_dfs)
    signal_gen.precompute_signals(start_date=args.start_date)
    
    # 5. Execute Long Engine
    long_executor = LongEngineExecutor(
        sys_config=sys_config,
        long_config=long_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        signals=signal_gen.long_signals
    )
    long_trades = long_executor.execute(start_date=args.start_date, end_date=args.end_date)
    
    # 6. Execute Short Engine
    short_executor = ShortEngineExecutor(
        sys_config=sys_config,
        short_config=short_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        signals=signal_gen.short_signals
    )
    short_trades = short_executor.execute(start_date=args.start_date, end_date=args.end_date)
    
    # 7. Reporting
    ReportingEngine.print_tearsheet(long_trades, short_trades, sys_config.capital, stock_dfs)

if __name__ == "__main__":
    main()
