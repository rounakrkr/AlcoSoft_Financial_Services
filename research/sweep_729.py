import sys
import os
import math
import logging
import json
import datetime
import pickle
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any

import pandas as pd
import numpy as np
import ta

# Ensure we can load local modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
logger = logging.getLogger("SweepMidcap50Engine")

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
    dyn_exit_type: str = "EMA21"              # "EMA21", "EMA50", "EMA9", "DISABLE", "TIME", "PROFIT_ONLY"
    dyn_exit_hold_time: int = 0               # hold time in minutes (15, 30, 45)
    reentry_cap: int = 9999                   # Max N re-entries per stock per day (0, 1, 2, 3)
    min_hold_time: int = 0                    # Min hold time before RSI_OVERBOUGHT exit allowed
    min_price: float = 0.0                    # Min price filter
    min_expected_pnl: float = 0.0             # Min expected PnL filter

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
    disable_shorts: bool = False              # Disable shorts entirely (Strategy A)
    savior_exit: bool = False                 # Savior dynamic exit (Strategy D)

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
        self.gaps_by_date = {}
        for (d, s), g in self.all_daily_gaps.items():
            self.gaps_by_date.setdefault(d, []).append(g)
        # Pre-convert to numpy arrays for fast regime calculation
        for d in self.gaps_by_date:
            self.gaps_by_date[d] = np.array(self.gaps_by_date[d], dtype=float)
        logger.info(f"Gap computation complete. Discovered {len(self.trading_dates)} trading days.")

    def get_bull_days(self, config: LongEngineConfig) -> Set[datetime.date]:
        bull_days = set()
        threshold = config.market_gap_threshold
        req = config.market_breadth_requirement
        for curr_d, gaps in self.gaps_by_date.items():
            if len(gaps) == 0:
                continue
            ratio = np.sum(gaps >= threshold) / len(gaps)
            if ratio >= req:
                bull_days.add(curr_d)
        return bull_days

    def get_bear_days(self, config: ShortEngineConfig) -> Set[datetime.date]:
        bear_days = set()
        threshold = config.market_gap_threshold
        req = config.market_breadth_requirement
        for curr_d, gaps in self.gaps_by_date.items():
            if len(gaps) == 0:
                continue
            ratio = np.sum(gaps <= threshold) / len(gaps)
            if ratio >= req:
                bear_days.add(curr_d)
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
            df["ema21"] = ta.trend.ema_indicator(df["close"], window=21).fillna(method="bfill")
            # EMA9 and EMA50 precomputation
            df["ema9"] = ta.trend.ema_indicator(df["close"], window=9).fillna(method="bfill")
            df["ema50"] = ta.trend.ema_indicator(df["close"], window=50).fillna(method="bfill")
        logger.info("Indicator enrichment complete.")

# ==============================================================================
# SIGNAL GENERATOR
# ==============================================================================
def compute_variant_f_signals(df: pd.DataFrame) -> pd.Series:
    # Technical variables
    open_0 = df["open"]
    high_0 = df["high"]
    low_0 = df["low"]
    close_0 = df["close"]
    volume_0 = df["volume"]
    avg_vol_0 = df["avg_vol"]
    rsi_0 = df["rsi"]
    vwap_0 = df["vwap"]
    ema9_0 = df["ema9"]
    ema21_0 = df["ema21"]
    ema50_0 = df["ema50"]
    macd_0 = df["macd"]
    macd_sig_0 = df["macd_sig"]
    sma20_0 = df["sma20"]
    obv_0 = df["obv"]
    bb_lower_0 = df["bb_lower"]
    
    # Pre-calculated shifts
    close_1 = close_0.shift(1)
    open_1 = open_0.shift(1)
    high_1 = high_0.shift(1)
    low_1 = low_0.shift(1)
    rsi_1 = rsi_0.shift(1)
    
    close_2 = close_0.shift(2)
    open_2 = open_0.shift(2)
    high_2 = high_0.shift(2)
    low_2 = low_0.shift(2)
    
    # 1. price_above_vwap
    price_above_vwap = close_0 > vwap_0
    
    # 2. ema_trending_up
    ema_trending_up = ema9_0 > ema21_0
    
    # 3. rsi_recovering
    rsi_lt_45 = rsi_0 < 45
    recent_oversold = rsi_lt_45 | rsi_lt_45.shift(1) | rsi_lt_45.shift(2)
    rsi_recovering = recent_oversold & (rsi_0 > rsi_1) & (rsi_0 < 65)
    
    # 4. bullish_reversal_candle patterns
    body_0 = (close_0 - open_0).abs()
    lower_wick_0 = df[["open", "close"]].min(axis=1) - low_0
    upper_wick_0 = high_0 - df[["open", "close"]].max(axis=1)
    
    body_1 = (close_1 - open_1).abs()
    body_2 = (close_2 - open_2).abs()
    
    # hammer
    hammer = (body_0 > 0) & (lower_wick_0 >= 2 * body_0) & (upper_wick_0 <= 0.5 * body_0)
    hammer_found = hammer | hammer.shift(1) | hammer.shift(2)
    rsi_20_40 = (rsi_0 > 20) & (rsi_0 < 40)
    rsi_zone = rsi_20_40 | rsi_20_40.shift(1) | rsi_20_40.shift(2)
    vol_ok_1 = volume_0 > avg_vol_0
    vol_ok = vol_ok_1 | vol_ok_1.shift(1) | vol_ok_1.shift(2)
    hammer_reversal = hammer_found & rsi_zone & vol_ok
    
    # engulfing
    engulf = (close_1 < open_1) & (close_0 > open_0) & (open_0 <= close_1) & (close_0 >= open_1)
    engulf_found = engulf | engulf.shift(1) | engulf.shift(2)
    rsi_ok_engulf = rsi_0 < 45
    bullish_engulfing = engulf_found & rsi_ok_engulf
    
    # inverted hammer
    shape_ok_inv = (body_0 > 0) & (upper_wick_0 >= 2 * body_0) & (lower_wick_0 <= 0.5 * body_0)
    prior_bearish_inv = close_1 < open_1
    inverted_hammer = shape_ok_inv & prior_bearish_inv
    inverted_hammer_found = inverted_hammer | inverted_hammer.shift(1) | inverted_hammer.shift(2)
    
    # dragonfly doji
    rng_0 = high_0 - low_0
    dragonfly = (rng_0 > 0) & (body_0 <= 0.1 * rng_0) & (lower_wick_0 >= 0.6 * rng_0) & (upper_wick_0 <= 0.1 * rng_0)
    dragonfly_found = dragonfly | dragonfly.shift(1) | dragonfly.shift(2)
    
    # marubozu
    marubozu = (close_0 > open_0) & (rng_0 > 0) & (body_0 >= 0.85 * rng_0) & (upper_wick_0 <= 0.1 * rng_0) & (lower_wick_0 <= 0.1 * rng_0)
    marubozu_found = marubozu | marubozu.shift(1) | marubozu.shift(2)
    
    # piercing line
    prev_bear = close_1 < open_1
    curr_bull = close_0 > open_0
    midpoint = (open_1 + close_1) / 2
    piercing = prev_bear & curr_bull & (open_0 < low_1) & (close_0 > midpoint)
    piercing_found = piercing | piercing.shift(1) | piercing.shift(2)
    
    # harami
    harami = prev_bear & curr_bull & (open_0 > close_1) & (close_0 < open_1) & (body_0 < body_1 * 0.6)
    harami_found = harami | harami.shift(1) | harami.shift(2)
    
    # morning star
    c1_bear = close_2 < open_2
    c3_bull = close_0 > open_0
    star_body = body_1
    c1_body = body_2
    gap_down_star = high_1 < close_2
    closes_into_star = close_0 > (open_2 + close_2) / 2
    morning_star = c1_bear & c3_bull & (c1_body > 0) & gap_down_star & (star_body < c1_body * 0.4) & closes_into_star
    morning_star_found = morning_star | morning_star.shift(1) | morning_star.shift(2)
    
    # three white soldiers
    c1_bull = close_2 > open_2
    c2_bull = close_1 > open_1
    c3_bull = close_0 > open_0
    rising_soldiers = (close_1 > close_2) & (close_0 > close_1)
    three_soldiers = c1_bull & c2_bull & c3_bull & rising_soldiers
    three_soldiers_found = three_soldiers | three_soldiers.shift(1) | three_soldiers.shift(2)
    
    bullish_reversal_candle = hammer_reversal | bullish_engulfing | inverted_hammer_found | dragonfly_found | marubozu_found | piercing_found | harami_found | morning_star_found | three_soldiers_found
    
    # 5. ema_9_21_crossover
    cross_up = ((ema9_0.shift(1) < ema21_0.shift(1)) & (ema9_0 > ema21_0)) | \
               ((ema9_0.shift(2) < ema21_0.shift(2)) & (ema9_0.shift(1) > ema21_0.shift(1))) | \
               ((ema9_0.shift(3) < ema21_0.shift(3)) & (ema9_0.shift(2) > ema21_0.shift(2)))
    ema_9_21_crossover = cross_up & (close_0 > ema50_0)
    
    # 6. volume_spike
    cond_vol = volume_0 > avg_vol_0 * 1.5
    volume_spike = cond_vol | cond_vol.shift(1) | cond_vol.shift(2)
    
    # 7. volume_breakout
    vol_spike_bo = (volume_0 > avg_vol_0 * 2.0) | (volume_0.shift(1) > avg_vol_0.shift(1) * 2.0) | (volume_0.shift(2) > avg_vol_0.shift(2) * 2.0)
    price_brk_bo = (close_0 > high_1) | (close_1 > high_2) | (close_2 > high_0.shift(3))
    above_sma_bo = close_0 > sma20_0
    volume_breakout = vol_spike_bo & price_brk_bo & above_sma_bo
    
    # 8. macd_positive
    macd_positive = macd_0 > 0
    
    # 9. rsi_not_overbought
    rsi_not_overbought = rsi_0 < 70
    
    # 10. obv_trending_up
    obv_trending_up = obv_0 > obv_0.shift(2)
    
    # 11. rsi_macd_momentum
    rsi_ok_rm = (rsi_0 < 35) | (rsi_0.shift(1) < 35) | (rsi_0.shift(2) < 35)
    macd_cross_rm = ((macd_0.shift(1) < macd_sig_0.shift(1)) & (macd_0 > macd_sig_0)) | \
                    ((macd_0.shift(2) < macd_sig_0.shift(2)) & (macd_0.shift(1) > macd_sig_0.shift(1))) | \
                    ((macd_0.shift(3) < macd_sig_0.shift(3)) & (macd_0.shift(2) > macd_sig_0.shift(2)))
    rsi_macd_momentum = rsi_ok_rm & macd_cross_rm
    
    # 12. bollinger_band_bounce
    touched_bb = (close_0 <= bb_lower_0) | (close_1 <= bb_lower_0.shift(1)) | (close_2 <= bb_lower_0.shift(2))
    bounced_bb = close_0 > bb_lower_0
    rsi_ok_bb = rsi_0 < 35
    bollinger_band_bounce = touched_bb & bounced_bb & rsi_ok_bb
    
    # 15. rsi_crosses_35_up
    rsi_crosses_35_up = (rsi_0.shift(1) <= 35) & (rsi_0 > 35)
    
    # 16. two_green_candles
    two_green_candles = (close_0 > open_0) & (close_1 > open_1)
    
    s1 = price_above_vwap & ema_trending_up & rsi_recovering & bullish_reversal_candle
    s2 = price_above_vwap & ema_9_21_crossover & volume_spike
    s3 = volume_breakout & price_above_vwap & macd_positive & rsi_not_overbought & obv_trending_up
    s4 = rsi_macd_momentum & bullish_reversal_candle & volume_spike
    s5 = price_above_vwap & bullish_reversal_candle & volume_spike
    s6 = bollinger_band_bounce & bullish_reversal_candle & macd_positive
    s7 = morning_star_found & rsi_recovering
    s8 = three_soldiers_found & obv_trending_up
    s9 = rsi_crosses_35_up & two_green_candles
    
    strategy_map = {
        "BUY_PERFECT_TREND_PULLBACK": s1,
        "BUY_GOLDEN_CROSSOVER_SURGE": s2,
        "BUY_POWER_BREAKOUT": s3,
        "BUY_OVERSOLD_REVERSAL_WITH_VOLUME": s4,
        "BUY_VWAP_RECLAIM_POWER": s5,
        "BUY_BOLLINGER_SQUEEZE_REVERSAL": s6,
        "BUY_MORNING_STAR_DIP": s7,
        "BUY_THREE_SOLDIERS_MOMENTUM": s8,
        "BUY_RSI_35_SUPPORT_BOUNCE": s9
    }
    
    active_strategies = []
    try:
        with open("config/strategy.txt", "r") as f:
            content = f.read().strip()
        data = json.loads("[" + content + "]")
        active_strategies = [x["name"] for x in data if x["name"] in strategy_map]
    except Exception as e:
        active_strategies = list(strategy_map.keys())
        
    combined = pd.Series(False, index=df.index)
    for name in active_strategies:
        combined = combined | strategy_map[name]
        
    return combined

class SignalGenerator:
    """Evaluates strategy signals using fast vectorized Pandas operations"""
    
    def __init__(self, stock_dfs: Dict[str, pd.DataFrame]):
        self.stock_dfs = stock_dfs
        self.long_signals: Dict[str, List[bool]] = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
        self.short_signals: Dict[str, List[bool]] = {sym: [False]*len(df) for sym, df in stock_dfs.items()}

    def precompute_signals(self, entry_variant="BASELINE"):
        logger.info(f"Precomputing Entry Signals via Vectorized Pandas operations for variant {entry_variant}...")
        for sym, df in self.stock_dfs.items():
            close_0 = df["close"]
            close_1 = df["close"].shift(1)
            ema20_1 = df["ema20"].shift(1)
            vwap_0 = df["vwap"]
            rsi_1 = df["rsi"].shift(1)
            highest_10_prev = df["high"].shift(2).rolling(10).max()

            if entry_variant == "BASELINE":
                vec_long = (
                    (close_1 > vwap_0) &
                    (ema20_1 > vwap_0) &
                    (rsi_1 > 61) &
                    (close_0 > highest_10_prev)
                )
            elif entry_variant == "VARIANT_A":
                vec_long = (
                    (close_1 > vwap_0) &
                    (ema20_1 > vwap_0) &
                    (rsi_1 > 55) &
                    (close_0 > highest_10_prev)
                )
            elif entry_variant == "VARIANT_B":
                vec_long = (
                    (close_1 > vwap_0) &
                    (ema20_1 > vwap_0) &
                    (rsi_1 > 50) &
                    (close_0 > highest_10_prev)
                )
            elif entry_variant == "VARIANT_C":
                cond_vol = df["volume"] > df["avg_vol"] * 1.5
                volume_spike = cond_vol | cond_vol.shift(1) | cond_vol.shift(2)
                vec_long = (
                    (close_1 > vwap_0) &
                    (ema20_1 > vwap_0) &
                    (rsi_1 > 61) &
                    (close_0 > highest_10_prev) &
                    volume_spike
                )
            elif entry_variant == "VARIANT_D":
                macd_0 = df["macd"]
                vec_long = (
                    (close_1 > vwap_0) &
                    (ema20_1 > vwap_0) &
                    (rsi_1 > 61) &
                    (macd_0 > 0)
                )
            elif entry_variant == "VARIANT_E":
                vec_baseline = (
                    (close_1 > vwap_0) &
                    (ema20_1 > vwap_0) &
                    (rsi_1 > 61) &
                    (close_0 > highest_10_prev)
                )
                median_sma14_0 = df["median_sma14"]
                ema20_0 = df["ema20"]
                vec_steady = (
                    (median_sma14_0 > vwap_0) &
                    (median_sma14_0 > ema20_0) &
                    (close_1 > ema20_0) &
                    (close_0 > vwap_0)
                )
                vec_long = vec_baseline | vec_steady
            elif entry_variant == "VARIANT_F":
                vec_long = compute_variant_f_signals(df)
            else:
                raise ValueError(f"Unknown entry variant: {entry_variant}")

            vec_long.iloc[:12] = False
            self.long_signals[sym] = vec_long.tolist()

            # Vectorized Short (SHORT_STREAK_MOMENTUM_BREAKDOWN)
            close_1_s = df["close"].shift(1)
            ema20_1_s = df["ema20"].shift(1)
            vwap_0_s = df["vwap"]
            rsi_1_s = df["rsi"].shift(1)
            close_0_s = df["close"]
            lowest_10_prev = df["low"].shift(1).rolling(10).min()
            high_1_s = df["high"].shift(1)

            vec_short = (
                (close_1_s < vwap_0_s) &
                (ema20_1_s < vwap_0_s) &
                (rsi_1_s < 39) &
                (close_0_s < lowest_10_prev) &
                (close_0_s <= high_1_s) &
                (close_0_s >= vwap_0_s * 0.988)
            )
            vec_short.iloc[:12] = False
            self.short_signals[sym] = vec_short.tolist()
            
        logger.info("Signal generation complete.")

# ==============================================================================
# LONG ENGINE EXECUTOR
# ==============================================================================
class LongEngineExecutor:
    """Executes trades based on Long Strategy rules"""
    _static_cache = {}
    
    def __init__(
        self, 
        sys_config: SystemConfig, 
        long_config: LongEngineConfig,
        stock_dfs: Dict[str, pd.DataFrame],
        regime_analyzer: MarketRegimeAnalyzer,
        signals: Dict[str, List[bool]],
        ts_with_signals: Set[pd.Timestamp],
        signals_by_ts: Dict[pd.Timestamp, List[str]]
    ):
        self.sys_config = sys_config
        self.config = long_config
        self.stock_dfs = stock_dfs
        self.regime = regime_analyzer
        self.signals = signals
        self.ts_with_signals = ts_with_signals
        self.signals_by_ts = signals_by_ts
        
        config_obj = load_strategy_sets()
        self.cover_set_def = next((s for s in config_obj.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)
        self.evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
        
        self.bull_days = self.regime.get_bull_days(self.config)
        self.trades: List[TradeRecord] = []
        self.open_positions: Dict[str, OpenPosition] = {}
        self.daily_entries: Dict[Tuple[datetime.date, str], int] = {}
        
        if not LongEngineExecutor._static_cache:
            # Build master timeline
            timeline_set = set()
            for sym, df in stock_dfs.items():
                timeline_set.update(df.index)
            timeline = sorted(list(timeline_set))
            stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
            
            # Pre-extract numpy arrays for fast lookups
            stock_opens = {sym: df["open"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_highs = {sym: df["high"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_lows = {sym: df["low"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_closes = {sym: df["close"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_rsi14 = {sym: df["rsi_14"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_ema21 = {sym: df["ema21"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_ema50 = {sym: df["ema50"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_ema9 = {sym: df["ema9"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            
            LongEngineExecutor._static_cache = {
                "timeline": timeline,
                "stock_ts_map": stock_ts_map,
                "stock_opens": stock_opens,
                "stock_highs": stock_highs,
                "stock_lows": stock_lows,
                "stock_closes": stock_closes,
                "stock_rsi14": stock_rsi14,
                "stock_ema21": stock_ema21,
                "stock_ema50": stock_ema50,
                "stock_ema9": stock_ema9,
            }
            
        cache = LongEngineExecutor._static_cache
        self.timeline = cache["timeline"]
        self.stock_ts_map = cache["stock_ts_map"]
        self.signal_indices = [i for i, ts in enumerate(self.timeline) if ts in ts_with_signals]
        self.stock_opens = cache["stock_opens"]
        self.stock_highs = cache["stock_highs"]
        self.stock_lows = cache["stock_lows"]
        self.stock_closes = cache["stock_closes"]
        self.stock_rsi14 = cache["stock_rsi14"]
        self.stock_ema21 = cache["stock_ema21"]
        self.stock_ema50 = cache["stock_ema50"]
        self.stock_ema9 = cache["stock_ema9"]

    def execute(self) -> List[TradeRecord]:
        import bisect
        timeline_len = len(self.timeline)
        timeline_idx = 0
        
        while timeline_idx < timeline_len:
            ts = self.timeline[timeline_idx]
            
            # Fast-forward if no open positions
            if not self.open_positions:
                next_sig_idx_pos = bisect.bisect_left(self.signal_indices, timeline_idx)
                if next_sig_idx_pos >= len(self.signal_indices):
                    break
                timeline_idx = self.signal_indices[next_sig_idx_pos]
                ts = self.timeline[timeline_idx]
                
            date_only = ts.date()
            is_bull_day = date_only in self.bull_days
            
            # 1. Manage Open Positions
            self._manage_positions(ts)
            
            # 2. Look for New Entries
            if is_bull_day and ts.hour < 15 and len(self.open_positions) < self.sys_config.max_open_positions:
                self._scan_for_entries(ts, date_only)
                
            timeline_idx += 1
            
        return self.trades

    def _manage_positions(self, ts: pd.Timestamp):
        syms_to_close = []
        
        for sym, pos in self.open_positions.items():
            idx = self.stock_ts_map[sym].get(ts)
            if idx is None:
                continue
            
            cp = self.stock_closes[sym][idx]
            lp = self.stock_lows[sym][idx]
            op = self.stock_opens[sym][idx]
            hp = self.stock_highs[sym][idx]
            
            # A. EOD Exit
            if ts.hour == 15 and ts.minute >= 15:
                self.trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                    direction="LONG", exit_reason="EOD_TIME"
                ))
                syms_to_close.append(sym)
                continue
                
            # B. Strict Stop Loss (Flat stop_loss_pct)
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
                curr_rsi = self.stock_rsi14[sym][idx]
                if curr_rsi >= self.config.rsi_exit_threshold:
                    hold_time_mins = (ts - pos.entry_time).total_seconds() / 60.0
                    if hold_time_mins >= self.config.min_hold_time:
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
                        
            # E. Dynamic Exit (SELL_EMA_MOMENTUM_LOSS or variants)
            if idx >= 1:
                close_1 = self.stock_closes[sym][idx-1]
                allow_exit = False
                
                if pos.entry_time is not None:
                    if self.config.dyn_exit_type == "EMA21":
                        ema21_1 = self.stock_ema21[sym][idx-1]
                        allow_exit = close_1 < ema21_1
                    elif self.config.dyn_exit_type == "EMA50":
                        ema50_1 = self.stock_ema50[sym][idx-1]
                        allow_exit = close_1 < ema50_1
                    elif self.config.dyn_exit_type == "EMA9":
                        ema9_1 = self.stock_ema9[sym][idx-1]
                        allow_exit = close_1 < ema9_1
                    elif self.config.dyn_exit_type == "DISABLE":
                        allow_exit = False
                    elif self.config.dyn_exit_type == "TIME":
                        ema21_1 = self.stock_ema21[sym][idx-1]
                        hold_time_mins = (ts - pos.entry_time).total_seconds() / 60.0
                        allow_exit = (close_1 < ema21_1) and (hold_time_mins >= self.config.dyn_exit_hold_time)
                    elif self.config.dyn_exit_type == "PROFIT_ONLY":
                        ema21_1 = self.stock_ema21[sym][idx-1]
                        allow_exit = (close_1 < ema21_1) and (close_1 > pos.entry_price)
                
                if allow_exit:
                    df = self.stock_dfs[sym]
                    if idx+1 < len(df): 
                        exit_price = self.stock_opens[sym][idx+1]
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
        syms_to_check = self.signals_by_ts.get(ts, [])
        for sym in syms_to_check:
            if len(self.open_positions) >= self.sys_config.max_open_positions:
                break
                
            if sym in self.open_positions:
                continue
                
            # Exclusion Filter: No-Clash Rule
            gap_pct = self.regime.get_gap(date_only, sym)
            if gap_pct <= self.config.exclude_gap_threshold:
                continue # Handed over to Short Engine
                
            idx = self.stock_ts_map[sym][ts]
            
            # Rule 2: Block entry if cover strategy is firing simultaneously
            if idx >= 1:
                close_1 = self.stock_closes[sym][idx-1]
                ema21_1 = self.stock_ema21[sym][idx-1]
                if close_1 < ema21_1:
                    continue # Blocked by Rule 2
                
            # Execute Entry on Next Candle Open
            df = self.stock_dfs[sym]
            if idx + 1 < len(df):
                entry_price = self.stock_opens[sym][idx+1]
                entry_time = df.index[idx+1]
                
                # Check reentry cap
                num_trades_today = self.daily_entries.get((entry_time.date(), sym), 0)
                if num_trades_today > self.config.reentry_cap:
                    continue
                
                # Check minimum price filter
                if entry_price < self.config.min_price:
                    continue
                
                # Determine Quantity
                qty = int(self.sys_config.capital_per_trade // entry_price)
                if qty > 0:
                    # Check minimum expected PnL filter
                    expected_pnl = (entry_price * self.config.profit_target_pct) * qty * 0.6
                    if expected_pnl < self.config.min_expected_pnl:
                        continue
                        
                    sl_price = entry_price * (1 - self.config.stop_loss_pct)
                    self.open_positions[sym] = OpenPosition(
                        symbol=sym, entry_time=entry_time, entry_price=entry_price,
                        quantity=qty, direction="LONG", stop_loss_price=sl_price,
                        partial_booked=False
                    )
                    self.daily_entries[(entry_time.date(), sym)] = num_trades_today + 1

# ==============================================================================
# SHORT ENGINE EXECUTOR
# ==============================================================================
class ShortEngineExecutor:
    """Executes trades based on Short Strategy rules"""
    _static_cache = {}
    
    def __init__(
        self, 
        sys_config: SystemConfig, 
        short_config: ShortEngineConfig,
        stock_dfs: Dict[str, pd.DataFrame],
        regime_analyzer: MarketRegimeAnalyzer,
        signals: Dict[str, List[bool]],
        ts_with_signals: Set[pd.Timestamp],
        signals_by_ts: Dict[pd.Timestamp, List[str]]
    ):
        self.sys_config = sys_config
        self.config = short_config
        self.stock_dfs = stock_dfs
        self.regime = regime_analyzer
        self.signals = signals
        self.ts_with_signals = ts_with_signals
        self.signals_by_ts = signals_by_ts
        
        self.bear_days = self.regime.get_bear_days(self.config)
        self.trades: List[TradeRecord] = []
        self.open_positions: Dict[str, OpenPosition] = {}
        
        if not ShortEngineExecutor._static_cache:
            # Build master timeline
            timeline_set = set()
            for sym, df in stock_dfs.items():
                timeline_set.update(df.index)
            timeline = sorted(list(timeline_set))
            stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
            
            # Pre-extract numpy arrays for fast lookups
            stock_opens = {sym: df["open"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_highs = {sym: df["high"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_lows = {sym: df["low"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_closes = {sym: df["close"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_rsi16 = {sym: df["rsi_16"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            stock_ema9 = {sym: df["ema9"].to_numpy(dtype=float) for sym, df in stock_dfs.items()}
            
            ShortEngineExecutor._static_cache = {
                "timeline": timeline,
                "stock_ts_map": stock_ts_map,
                "stock_opens": stock_opens,
                "stock_highs": stock_highs,
                "stock_lows": stock_lows,
                "stock_closes": stock_closes,
                "stock_rsi16": stock_rsi16,
                "stock_ema9": stock_ema9,
            }
            
        cache = ShortEngineExecutor._static_cache
        self.timeline = cache["timeline"]
        self.stock_ts_map = cache["stock_ts_map"]
        self.signal_indices = [i for i, ts in enumerate(self.timeline) if ts in ts_with_signals]
        self.stock_opens = cache["stock_opens"]
        self.stock_highs = cache["stock_highs"]
        self.stock_lows = cache["stock_lows"]
        self.stock_closes = cache["stock_closes"]
        self.stock_rsi16 = cache["stock_rsi16"]
        self.stock_ema9 = cache["stock_ema9"]

    def execute(self) -> List[TradeRecord]:
        if self.config.disable_shorts:
            return []
            
        import bisect
        timeline_len = len(self.timeline)
        timeline_idx = 0
        
        while timeline_idx < timeline_len:
            ts = self.timeline[timeline_idx]
            
            # Fast-forward if no open positions
            if not self.open_positions:
                next_sig_idx_pos = bisect.bisect_left(self.signal_indices, timeline_idx)
                if next_sig_idx_pos >= len(self.signal_indices):
                    break
                timeline_idx = self.signal_indices[next_sig_idx_pos]
                ts = self.timeline[timeline_idx]
                
            date_only = ts.date()
            is_bear_day = date_only in self.bear_days
            
            # 1. Manage Open Positions
            self._manage_positions(ts)
            
            # 2. Look for New Entries
            if is_bear_day and ts.hour < 15 and len(self.open_positions) < self.sys_config.max_open_positions:
                self._scan_for_entries(ts, date_only)
                
            timeline_idx += 1
            
        return self.trades

    def _manage_positions(self, ts: pd.Timestamp):
        syms_to_close = []
        
        for sym, pos in self.open_positions.items():
            idx = self.stock_ts_map[sym].get(ts)
            if idx is None:
                continue
            
            cp = self.stock_closes[sym][idx]
            lp = self.stock_lows[sym][idx]
            op = self.stock_opens[sym][idx]
            hp = self.stock_highs[sym][idx]
            
            # A. EOD Exit
            if ts.hour == 15 and ts.minute >= 15:
                self.trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                    direction="SHORT", exit_reason="EOD_TIME"
                ))
                syms_to_close.append(sym)
                continue
                
            # A2. Savior Exit (Strategy D) - checked at next open, exits at open price
            if self.config.savior_exit and ts > pos.entry_time and idx >= 1:
                prev_close = self.stock_closes[sym][idx-1]
                prev_ema9 = self.stock_ema9[sym][idx-1]
                if prev_close > prev_ema9:
                    self.trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=pos.entry_price, exit_price=op, quantity=pos.quantity,
                        direction="SHORT", exit_reason="SAVIOR_EXIT"
                    ))
                    syms_to_close.append(sym)
                    continue

            # B. Stop Loss
            if not self.config.savior_exit:
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
                prev_rsi = self.stock_rsi16[sym][idx-1]
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
        syms_to_check = self.signals_by_ts.get(ts, [])
        for sym in syms_to_check:
            if len(self.open_positions) >= self.sys_config.max_open_positions:
                break
                
            if sym in self.open_positions:
                continue
                
            # Inclusion Filter: Target Weak Stocks
            gap_pct = self.regime.get_gap(date_only, sym)
            if gap_pct > self.config.target_gap_threshold:
                continue # Not weak enough
                
            idx = self.stock_ts_map[sym][ts]
            
            # Block entry if the short cover strategy triggers (close > previous high)
            if idx >= 1 and self.stock_closes[sym][idx] > self.stock_highs[sym][idx-1]:
                continue
                
            # Execute Entry on Next Candle Open
            df = self.stock_dfs[sym]
            if idx + 1 < len(df):
                entry_price = self.stock_opens[sym][idx+1]
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
# ENTERPRISE REPORTING ENGINE
# ==============================================================================
class ReportingEngine:
    """Generates detailed performance metrics and tearsheets"""
    
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
            "Average Win": f"Rs. {avg_win:.2f}",
            "Average Loss": f"Rs. {avg_loss:.2f}",
            "Expectancy": f"Rs. {expectancy:.2f} per trade"
        }

    @staticmethod
    def print_tearsheet(long_trades: List[TradeRecord], short_trades: List[TradeRecord], capital: float) -> str:
        output_lines = []
        output_lines.append("\n" + "="*80)
        output_lines.append("🏛️ ALCOSOFT FINANCIAL SERVICES - MIDCAP 100 DUAL ENGINE REPORT 🏛️")
        output_lines.append("="*80)
        
        long_metrics = ReportingEngine._calculate_metrics(long_trades, capital)
        output_lines.append("\n🟢 THE BULL MARKET ASSASSIN (LONG ENGINE)")
        output_lines.append("-" * 50)
        if long_metrics:
            for k, v in long_metrics.items():
                output_lines.append(f"  {k:<15}: {v:>15}")
        else:
            output_lines.append("  No Long trades recorded.")
            
        short_metrics = ReportingEngine._calculate_metrics(short_trades, capital)
        output_lines.append("\n🔴 THE BEAR MARKET ASSASSIN (SHORT ENGINE)")
        output_lines.append("-" * 50)
        if short_metrics:
            for k, v in short_metrics.items():
                output_lines.append(f"  {k:<15}: {v:>15}")
        else:
            output_lines.append("  No Short trades recorded.")
            
        # Combined Portfolio
        combined_trades = long_trades + short_trades
        combined_trades.sort(key=lambda t: t.entry_time)
        combined_metrics = ReportingEngine._calculate_metrics(combined_trades, capital)
        
        output_lines.append("\n🔥 DUAL-ENGINE PORTFOLIO (COMBINED NET PERFORMANCE)")
        output_lines.append("=" * 80)
        if combined_metrics:
            for k, v in combined_metrics.items():
                output_lines.append(f"  {k:<15}: {v:>15}")
        else:
            output_lines.append("  No trades recorded in portfolio.")
            
        output_lines.append("="*80 + "\n")
        
        tearsheet_text = "\n".join(output_lines)
        try:
            print(tearsheet_text)
        except UnicodeEncodeError:
            cleaned_text = (
                tearsheet_text
                .replace("🏛️", "[AFS]")
                .replace("🟢", "[LONG]")
                .replace("🔴", "[SHORT]")
                .replace("🔥", "[PORTFOLIO]")
                .replace("₹", "Rs. ")
            )
            try:
                print(cleaned_text)
            except UnicodeEncodeError:
                print(cleaned_text.encode('ascii', errors='replace').decode('ascii'))
        
        # Save tearsheet locally in the workspace
        os.makedirs("research/results", exist_ok=True)
        local_out_path = "research/results/ALCOSOFT_MIDCAP_TEARSHEET.md"
        with open(local_out_path, "w", encoding="utf-8") as f:
            f.write(tearsheet_text)
        logger.info(f"Tearsheet written to {local_out_path}")
        
        # Fallback brain path for backwards compatibility
        brain_out_path = r"C:\Users\RounakKR\.gemini\antigravity\brain\43f12dc8-ce61-4ee1-a264-ac9e7d781d0a\ALCOSOFT_MIDCAP_TEARSHEET.md"
        try:
            os.makedirs(os.path.dirname(brain_out_path), exist_ok=True)
            with open(brain_out_path, "w", encoding="utf-8") as f:
                f.write(tearsheet_text)
            logger.info(f"Tearsheet also mirrored to brain path: {brain_out_path}")
        except Exception as e:
            logger.warning(f"Could not mirror tearsheet to brain path (expected on different systems): {e}")
            
        return tearsheet_text

# ==============================================================================
# MAIN BACKTEST EXECUTION FUNCTION
# ==============================================================================
def run_backtest(
    sys_config: SystemConfig,
    long_config: LongEngineConfig,
    short_config: ShortEngineConfig,
    stock_dfs: Dict[str, pd.DataFrame],
    regime_analyzer: MarketRegimeAnalyzer,
    long_signals: Dict[str, List[bool]],
    short_signals: Dict[str, List[bool]],
    long_ts_with_signals: Set[pd.Timestamp],
    long_signals_by_ts: Dict[pd.Timestamp, List[str]],
    short_ts_with_signals: Set[pd.Timestamp],
    short_signals_by_ts: Dict[pd.Timestamp, List[str]],
    verbose: bool = True
) -> Tuple[List[TradeRecord], List[TradeRecord], Dict[str, Any]]:
    """Runs a single parameterized backtest instance using preloaded cache and precomputed signals"""
    
    # 1. Execute Long Engine
    long_executor = LongEngineExecutor(
        sys_config=sys_config,
        long_config=long_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        signals=long_signals,
        ts_with_signals=long_ts_with_signals,
        signals_by_ts=long_signals_by_ts
    )
    long_trades = long_executor.execute()
    
    # 2. Execute Short Engine
    short_executor = ShortEngineExecutor(
        sys_config=sys_config,
        short_config=short_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        signals=short_signals,
        ts_with_signals=short_ts_with_signals,
        signals_by_ts=short_signals_by_ts
    )
    short_trades = short_executor.execute()
    
    # 3. Combined Metrics
    combined_trades = long_trades + short_trades
    combined_trades.sort(key=lambda t: t.entry_time)
    metrics = ReportingEngine._calculate_metrics(combined_trades, sys_config.capital)
    
    if verbose:
        ReportingEngine.print_tearsheet(long_trades, short_trades, sys_config.capital)
        
    return long_trades, short_trades, metrics

def load_cache():
    cache_path = os.path.join(os.path.dirname(__file__), "midcap50_historical_cache.pkl")
    with open(cache_path, "rb") as f:
        data = pickle.load(f)
    print(f"Loaded {len(data['stock_dfs'])} stocks from Midcap 50 historical cache.")
    return data["stock_dfs"]

# ==============================================================================
# MAIN EXECUTION ROUTINE
# ==============================================================================

def main():
    import itertools
    print("Loading 44-stock DataFrame cache for Midcap 50...")
    stock_dfs = load_cache()
    if not stock_dfs: return
    
    print("Enriching data...")
    IndicatorPreprocessor.enrich_data(stock_dfs)
    
    print("Analyzing market regime...")
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)

    d1_params = [
        {"sl": 0.007, "pt": 0.025, "rsi": 78.0},
        {"sl": 0.008, "pt": 0.025, "rsi": 78.0},
        {"sl": 0.010, "pt": 0.025, "rsi": 78.0}
    ]
    d2_params = ['DISABLE', 'EMA50', 'PROFIT_ONLY']
    d3_params = [
        {"s_gap": -0.015, "s_rsi": 17.0},
        {"s_gap": -0.015, "s_rsi": 25.0},
        {"s_gap": -0.012, "s_rsi": 17.0}
    ]
    d4_params = ['VARIANT_E', 'VARIANT_D', 'BASELINE']
    d5_params = [20, 15, 0]
    d6_params = [200000.0, 100000.0, 500000.0]

    all_combos = list(itertools.product(d1_params, d2_params, d3_params, d4_params, d5_params, d6_params))
    total = len(all_combos)
    
    results_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(results_dir, exist_ok=True)
    
    leaderboard_file = os.path.join(results_dir, "custom_729_leaderboard.txt")
    csv_file = os.path.join(results_dir, "custom_729_all_runs.csv")
    tearsheet_file = os.path.join(results_dir, "BEST_729_CONFIG_TEARSHEET.txt")
    
    with open(csv_file, 'w', encoding='utf-8') as f:
        f.write("name,net_val,Total Trades,Win Rate,Gross Return,STT Impact\n")

    top_results = []
    best_net = -999.0

    print(f"Executing {total} combinations...")
    count = 0
    
    cached_signals = {}
    for v in d4_params:
        print(f"Precomputing signals for {v}...")
        sig_gen = SignalGenerator(stock_dfs)
        sig_gen.precompute_signals(entry_variant=v)
        
        long_tws = set()
        long_sbts = {}
        for sym, sigs in sig_gen.long_signals.items():
            df = stock_dfs[sym]
            for idx, has_signal in enumerate(sigs):
                if has_signal:
                    ts = df.index[idx]
                    long_tws.add(ts)
                    long_sbts.setdefault(ts, []).append(sym)
                    
        short_tws = set()
        short_sbts = {}
        for sym, sigs in sig_gen.short_signals.items():
            df = stock_dfs[sym]
            for idx, has_signal in enumerate(sigs):
                if has_signal:
                    ts = df.index[idx]
                    short_tws.add(ts)
                    short_sbts.setdefault(ts, []).append(sym)
                    
        cached_signals[v] = (
            sig_gen.long_signals,
            long_tws,
            long_sbts,
            sig_gen.short_signals,
            short_tws,
            short_sbts
        )

    for c in all_combos:
        d1, dyn, d3, var, hold, cap = c
        count += 1
        name = f"C729_{count}"
        
        sys_config = SystemConfig(capital=cap, margin=5.0)
        long_config = LongEngineConfig(
            stop_loss_pct=d1["sl"],
            profit_target_pct=d1["pt"],
            rsi_exit_threshold=d1["rsi"],
            market_gap_threshold=0.007,
            market_breadth_requirement=0.35,
            partial_booking_fraction=0.25,
            dyn_exit_type=dyn,
            min_hold_time=hold,
            reentry_cap=9999
        )
        short_config = ShortEngineConfig(
            target_gap_threshold=d3["s_gap"],
            rsi_exit_threshold=d3["s_rsi"],
            stop_loss_pct=0.005,
            profit_target_pct=0.025,
            market_gap_threshold=-0.006,
            market_breadth_requirement=0.4,
            disable_shorts=False,
            savior_exit=False
        )

        l_s, l_tws, l_sbts, s_s, s_tws, s_sbts = cached_signals[var]
        
        long_trades, short_trades, combined_metrics = run_backtest(
            sys_config, long_config, short_config, stock_dfs, regime_analyzer,
            l_s, s_s, l_tws, l_sbts, s_tws, s_sbts, verbose=False
        )

        net_str = combined_metrics.get("Net Return", "0%")
        try:
            net_pct = float(net_str.replace('%', ''))
        except ValueError:
            net_pct = 0.0
        with open(csv_file, 'a', encoding='utf-8') as f:
            f.write(f"{name},{net_pct:.2f},{combined_metrics.get('Total Trades', 0)},{combined_metrics.get('Win Rate', '0%')},{combined_metrics.get('Gross Return', '0%')},{combined_metrics.get('STT Impact', '0%')}\n")

        top_results.append((name, net_pct, combined_metrics, long_trades, short_trades, sys_config))
        top_results.sort(key=lambda x: x[1], reverse=True)
        top_results = top_results[:10]

        if net_pct > best_net:
            best_net = net_pct
            tearsheet = ReportingEngine.print_tearsheet(long_trades, short_trades, sys_config.buying_power)
            with open(tearsheet_file, 'w', encoding='utf-8') as f:
                f.write(tearsheet)

        if count % 20 == 0 or count == total:
            with open(leaderboard_file, 'w', encoding='utf-8') as f:
                f.write("CUSTOM 729 CARTESIAN SWEEP LEADERBOARD\n")
                f.write("=================================================================\n")
                for i, tr in enumerate(top_results):
                    n_str, n_pct, r_dict, _, _, _ = tr
                    f.write(f" #{i+1} | {n_str} | Net={n_pct:.2f}% | WR={r_dict.get('Win Rate','')} | T={r_dict.get('Total Trades','')}\n")
            print(f"Processed {count}/{total}... Best Net: {top_results[0][1]:.2f}%")

if __name__ == '__main__':
    main()
