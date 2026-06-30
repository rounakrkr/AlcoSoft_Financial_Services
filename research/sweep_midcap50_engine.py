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
                
            # Execute Entry on Current Candle Close
            df = self.stock_dfs[sym]
            if True:
                entry_price = self.stock_closes[sym][idx]
                entry_time = df.index[idx]
                
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
                
            # Execute Entry on Current Candle Close
            df = self.stock_dfs[sym]
            if True:
                entry_price = self.stock_closes[sym][idx]
                entry_time = df.index[idx]
                
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
    logger.info("Initializing AlcoSoft Dual-Engine Parameterized Backtest Framework...")
    
    # 1. Load Data
    logger.info("Loading market data cache...")
    stock_dfs = load_cache()
    if not stock_dfs:
        logger.error("Failed to load market data cache. Exiting.")
        return
        
    # 2. Enrich Data
    IndicatorPreprocessor.enrich_data(stock_dfs)
    
    # 3. Analyze Market Regime
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
    
    # 4. Generate Signals
    signal_gen = SignalGenerator(stock_dfs)
    signal_gen.precompute_signals()
    
    # Precalculate Short Signals once
    short_ts_with_signals = set()
    short_signals_by_ts = {}
    for sym, sigs in signal_gen.short_signals.items():
        df = stock_dfs[sym]
        for idx, has_signal in enumerate(sigs):
            if has_signal:
                ts = df.index[idx]
                short_ts_with_signals.add(ts)
                short_signals_by_ts.setdefault(ts, []).append(sym)
                
    current_entry_variant = None
    long_ts_with_signals = set()
    long_signals_by_ts = {}
    
    def update_signals(variant):
        nonlocal current_entry_variant, long_ts_with_signals, long_signals_by_ts
        if current_entry_variant == variant:
            return
        signal_gen.precompute_signals(variant)
        long_ts_with_signals = set()
        long_signals_by_ts = {}
        for sym, sigs in signal_gen.long_signals.items():
            df = stock_dfs[sym]
            for idx, has_signal in enumerate(sigs):
                if has_signal:
                    ts = df.index[idx]
                    long_ts_with_signals.add(ts)
                    long_signals_by_ts.setdefault(ts, []).append(sym)
        current_entry_variant = variant
        
    update_signals("BASELINE")
    
    all_runs = []
    
    def execute_run(config_name, long_params, short_params, capital=100000.0, margin=5.0):
        variant = long_params.get("entry_variant", "BASELINE")
        update_signals(variant)
        sys_cfg = SystemConfig(
            capital=capital,
            margin=margin,
            max_open_positions=long_params.get("max_open_positions", 1)
        )
        long_cfg = LongEngineConfig(
            market_gap_threshold=long_params.get("market_gap_threshold", 0.010),
            market_breadth_requirement=long_params.get("market_breadth_requirement", 0.40),
            exclude_gap_threshold=long_params.get("exclude_gap_threshold", -0.008),
            rsi_exit_threshold=long_params.get("rsi_exit_threshold", 72.0),
            profit_target_pct=long_params.get("profit_target_pct", 0.015),
            partial_booking_fraction=long_params.get("partial_booking_fraction", 1.00),
            stop_loss_pct=long_params.get("stop_loss_pct", 0.008),
            dyn_exit_type=long_params.get("dyn_exit_type", "EMA21"),
            dyn_exit_hold_time=long_params.get("dyn_exit_hold_time", 0),
            reentry_cap=long_params.get("reentry_cap", 9999),
            min_hold_time=long_params.get("min_hold_time", 0),
            min_price=long_params.get("min_price", 0.0),
            min_expected_pnl=long_params.get("min_expected_pnl", 0.0)
        )
        short_cfg = ShortEngineConfig(
            market_gap_threshold=short_params.get("market_gap_threshold", -0.006),
            market_breadth_requirement=short_params.get("market_breadth_requirement", 0.40),
            target_gap_threshold=short_params.get("target_gap_threshold", -0.008),
            rsi_exit_threshold=short_params.get("rsi_exit_threshold", 17.0),
            profit_target_pct=short_params.get("profit_target_pct", 0.025),
            partial_booking_fraction=short_params.get("partial_booking_fraction", 1.00),
            stop_loss_pct=short_params.get("stop_loss_pct", 0.005),
            disable_shorts=short_params.get("disable_shorts", False),
            savior_exit=short_params.get("savior_exit", False)
        )
        
        long_trades, short_trades, metrics = run_backtest(
            sys_config=sys_cfg,
            long_config=long_cfg,
            short_config=short_cfg,
            stock_dfs=stock_dfs,
            regime_analyzer=regime_analyzer,
            long_signals=signal_gen.long_signals,
            short_signals=signal_gen.short_signals,
            long_ts_with_signals=long_ts_with_signals,
            long_signals_by_ts=long_signals_by_ts,
            short_ts_with_signals=short_ts_with_signals,
            short_signals_by_ts=short_signals_by_ts,
            verbose=False
        )
        net_val_str = metrics.get("Net Return", "0.00%")
        net_val_float = float(net_val_str.replace("%", ""))
        
        run_entry = {
            "name": config_name,
            "capital": sys_cfg.capital,
            "margin": sys_cfg.margin,
            "buying_power": sys_cfg.buying_power,
            "long_stop_loss_pct": long_cfg.stop_loss_pct,
            "long_profit_target_pct": long_cfg.profit_target_pct,
            "long_rsi_exit_threshold": long_cfg.rsi_exit_threshold,
            "long_market_gap_threshold": long_cfg.market_gap_threshold,
            "long_market_breadth_requirement": long_cfg.market_breadth_requirement,
            "long_max_open_positions": sys_cfg.max_open_positions,
            "long_partial_booking_fraction": long_cfg.partial_booking_fraction,
            "long_dyn_exit_type": long_cfg.dyn_exit_type,
            "long_dyn_exit_hold_time": long_cfg.dyn_exit_hold_time,
            "long_reentry_cap": long_cfg.reentry_cap,
            "long_min_hold_time": long_cfg.min_hold_time,
            "long_min_price": long_cfg.min_price,
            "long_min_expected_pnl": long_cfg.min_expected_pnl,
            "long_entry_variant": variant,
            "short_stop_loss_pct": short_cfg.stop_loss_pct,
            "short_profit_target_pct": short_cfg.profit_target_pct,
            "short_rsi_exit_threshold": short_cfg.rsi_exit_threshold,
            "short_market_gap_threshold": short_cfg.market_gap_threshold,
            "short_market_breadth_requirement": short_cfg.market_breadth_requirement,
            "short_target_gap_threshold": short_cfg.target_gap_threshold,
            "short_disable_shorts": short_cfg.disable_shorts,
            "short_savior_exit": short_cfg.savior_exit,
            "Total Trades": metrics.get("Total Trades", 0),
            "Win Rate": metrics.get("Win Rate", "0.00%"),
            "Gross Return": metrics.get("Gross Return", "0.00%"),
            "STT Impact": metrics.get("STT Impact", "0.00%"),
            "Net Return": metrics.get("Net Return", "0.00%"),
            "Profit Factor": metrics.get("Profit Factor", "0.00"),
            "Expectancy": metrics.get("Expectancy", "₹0.00"),
            "net_val": net_val_float
        }
        all_runs.append(run_entry)
        return run_entry

    # 5. Baseline Runs
    logger.info("Running baselines...")
    variants = [
        {"name": "BASELINE", "dyn_exit_type": "EMA21", "dyn_exit_hold_time": 0},
        {"name": "EMA50_BREAK", "dyn_exit_type": "EMA50", "dyn_exit_hold_time": 0},
        {"name": "EMA9_BREAK", "dyn_exit_type": "EMA9", "dyn_exit_hold_time": 0},
        {"name": "DISABLE_DYN_EXIT", "dyn_exit_type": "DISABLE", "dyn_exit_hold_time": 0},
        {"name": "TIME_15", "dyn_exit_type": "TIME", "dyn_exit_hold_time": 15},
        {"name": "TIME_30", "dyn_exit_type": "TIME", "dyn_exit_hold_time": 30},
        {"name": "TIME_45", "dyn_exit_type": "TIME", "dyn_exit_hold_time": 45},
        {"name": "PROFIT_ONLY", "dyn_exit_type": "PROFIT_ONLY", "dyn_exit_hold_time": 0},
    ]
    for v in variants:
        execute_run(v["name"], {"dyn_exit_type": v["dyn_exit_type"], "dyn_exit_hold_time": v["dyn_exit_hold_time"]}, {})

    # Anchor Long Engine on DISABLE_DYN_EXIT for sweeps
    long_anchor = {"dyn_exit_type": "DISABLE", "dyn_exit_hold_time": 0}

    # R3: Short Engine Rehabilitation Sweeps
    logger.info("Executing Short Engine Rehabilitation Sweeps (R3)...")
    
    # Strategy A: Disable shorts entirely
    execute_run("STRATEGY_A_DISABLE_SHORTS", long_anchor, {"disable_shorts": True})
    
    # Strategy B: Tighter stock selection (vary target_gap_threshold = -0.012 and -0.015, preserving original lower bound -0.008)
    b_runs = []
    for gap in [-0.008, -0.012, -0.015]:
        r = execute_run(f"STRATEGY_B_GAP_{gap}", long_anchor, {"target_gap_threshold": gap})
        b_runs.append(r)
    best_b = max(b_runs, key=lambda x: x["net_val"])
    best_gap = best_b["short_target_gap_threshold"]
    logger.info(f"Best Strategy B target_gap_threshold: {best_gap} with Net Return: {best_b['net_val']}%")

    # Strategy C: Test wider short stop loss pct (preserving original lower bound 0.005)
    c_runs = []
    for sl in [0.005, 0.008, 0.010, 0.012, 0.015, 0.018, 0.020, 0.025]:
        r = execute_run(f"STRATEGY_C_SL_{sl}", long_anchor, {"stop_loss_pct": sl})
        c_runs.append(r)
    best_c = max(c_runs, key=lambda x: x["net_val"])
    best_sl = best_c["short_stop_loss_pct"]
    logger.info(f"Best Strategy C stop_loss_pct: {best_sl} with Net Return: {best_c['net_val']}%")

    # Strategy D: Savior Exit
    d_run = execute_run("STRATEGY_D_SAVIOR_EXIT", long_anchor, {"savior_exit": True})
    logger.info(f"Strategy D Savior Exit Net Return: {d_run['net_val']}%")

    # Compare D vs C to select best SL mechanism
    if d_run["net_val"] > best_c["net_val"]:
        combine_savior = True
        combine_sl = 0.005  # default
        logger.info("Savior Exit outperforms fixed Stop Loss. Selecting Savior Exit for Strategy F.")
    else:
        combine_savior = False
        combine_sl = best_sl
        logger.info(f"Fixed Stop Loss {best_sl} outperforms Savior Exit. Selecting fixed Stop Loss for Strategy F.")

    # Strategy E: Test higher RSI cover thresholds [12, 15, 20, 25, 30, 35] (preserving original lower bound 12)
    e_runs = []
    for rsi in [12.0, 15.0, 17.0, 20.0, 25.0, 30.0, 35.0]:
        r = execute_run(f"STRATEGY_E_RSI_{rsi}", long_anchor, {"rsi_exit_threshold": rsi})
        e_runs.append(r)
    best_e = max(e_runs, key=lambda x: x["net_val"])
    best_rsi = best_e["short_rsi_exit_threshold"]
    logger.info(f"Best Strategy E rsi_exit_threshold: {best_rsi} with Net Return: {best_e['net_val']}%")

    # Strategy F: Combined fixes
    f_run = execute_run(
        "STRATEGY_F_COMBINED_FIXES",
        long_anchor,
        {
            "target_gap_threshold": best_gap,
            "rsi_exit_threshold": best_rsi,
            "savior_exit": combine_savior,
            "stop_loss_pct": combine_sl
        }
    )
    logger.info(f"Strategy F Combined Fixes Net Return: {f_run['net_val']}%")

    # Set Short Engine anchor to Strategy F combined settings for the Long Engine sweep
    short_anchor = {
        "target_gap_threshold": best_gap,
        "rsi_exit_threshold": best_rsi,
        "savior_exit": combine_savior,
        "stop_loss_pct": combine_sl
    }

    # R1: Systematic Sweep of Long Engine Parameters (Coordinate Descent/Greedy Search)
    logger.info("Executing Long Engine Systematic Sweep (R1)...")
    
    long_current = {
        "stop_loss_pct": 0.008,
        "profit_target_pct": 0.015,
        "rsi_exit_threshold": 72.0,
        "market_gap_threshold": 0.010,
        "market_breadth_requirement": 0.40,
        "max_open_positions": 1,
        "partial_booking_fraction": 1.00,
        "dyn_exit_type": "DISABLE",
        "dyn_exit_hold_time": 0
    }

    stop_loss_pct_list = [0.005, 0.006, 0.007, 0.008, 0.010, 0.012, 0.015, 0.018, 0.020, 0.025]
    profit_target_pct_list = [0.010, 0.012, 0.015, 0.018, 0.020, 0.025, 0.030, 0.040, 0.050]
    rsi_exit_threshold_list = [68, 70, 72, 74, 76, 78, 80, 82, 85, 88]
    market_gap_threshold_list = [0.003, 0.005, 0.007, 0.010, 0.012, 0.015, 0.020]
    market_breadth_requirement_list = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
    max_open_positions_list = [1, 2, 3, 4, 5]
    partial_booking_fraction_list = [0.25, 0.50, 0.75, 1.00]

    def sweep_parameter(param_name, values_list, pass_num):
        best_val = long_current[param_name]
        best_return = -999999.0
        for val in values_list:
            test_long = long_current.copy()
            test_long[param_name] = val
            res = execute_run(f"PASS{pass_num}_{param_name}_{val}", test_long, short_anchor)
            if res["net_val"] > best_return:
                best_return = res["net_val"]
                best_val = val
        long_current[param_name] = best_val
        logger.info(f"Pass {pass_num} - Parameter {param_name} optimized to: {best_val} (Net Return: {best_return}%)")

    # Pass 1
    logger.info("Starting Greedy Sweep Pass 1...")
    sweep_parameter("stop_loss_pct", stop_loss_pct_list, 1)
    sweep_parameter("profit_target_pct", profit_target_pct_list, 1)
    sweep_parameter("rsi_exit_threshold", rsi_exit_threshold_list, 1)
    sweep_parameter("market_gap_threshold", market_gap_threshold_list, 1)
    sweep_parameter("market_breadth_requirement", market_breadth_requirement_list, 1)
    sweep_parameter("max_open_positions", max_open_positions_list, 1)
    sweep_parameter("partial_booking_fraction", partial_booking_fraction_list, 1)

    # Pass 2
    logger.info("Starting Greedy Sweep Pass 2...")
    sweep_parameter("stop_loss_pct", stop_loss_pct_list, 2)
    sweep_parameter("profit_target_pct", profit_target_pct_list, 2)
    sweep_parameter("rsi_exit_threshold", rsi_exit_threshold_list, 2)
    sweep_parameter("market_gap_threshold", market_gap_threshold_list, 2)
    sweep_parameter("market_breadth_requirement", market_breadth_requirement_list, 2)
    sweep_parameter("max_open_positions", max_open_positions_list, 2)
    sweep_parameter("partial_booking_fraction", partial_booking_fraction_list, 2)

    # Optimal Final Combination
    logger.info(f"Optimal Long parameters: {long_current}")
    opt_run = execute_run("OPTIMAL_FINAL_COMBINATION", long_current, short_anchor)
    logger.info(f"Optimal Final Combination Net Return: {opt_run['net_val']}%")

    # ==============================================================================
    # R4 & R5 EXPERIMENTS & SWEEPS
    # ==============================================================================
    # 5b. Entry Condition Experiments (R4)
    logger.info("Executing Entry Condition Experiments (R4)...")
    long_milestone3_anchor = {
        "max_open_positions": 1,
        "stop_loss_pct": 0.0070,
        "profit_target_pct": 0.0250,
        "rsi_exit_threshold": 78.0,
        "market_gap_threshold": 0.0070,
        "market_breadth_requirement": 0.35,
        "partial_booking_fraction": 0.25,
        "dyn_exit_type": "DISABLE",
        "dyn_exit_hold_time": 0
    }
    
    entry_variants = [
        ("BASELINE", "BASELINE"),
        ("VARIANT_A", "VARIANT_A"),
        ("VARIANT_B", "VARIANT_B"),
        ("VARIANT_C", "VARIANT_C"),
        ("VARIANT_D", "VARIANT_D"),
        ("VARIANT_E", "VARIANT_E"),
        ("VARIANT_F", "VARIANT_F")
    ]
    r4_results = []
    for name, var in entry_variants:
        test_long = long_milestone3_anchor.copy()
        test_long["entry_variant"] = var
        r = execute_run(f"R4_ENTRY_{name}", test_long, short_anchor)
        r4_results.append(r)
        logger.info(f"Entry Variant {name} Net Return: {r['net_val']}% (Trades: {r['Total Trades']}, WR: {r['Win Rate']})")
        
    best_r4 = max(r4_results, key=lambda x: x["net_val"])
    best_entry_var = "BASELINE"
    for name, var in entry_variants:
        if f"R4_ENTRY_{name}" == best_r4["name"]:
            best_entry_var = var
            break
    logger.info(f"Best Entry Variant found: {best_entry_var} with Net Return: {best_r4['net_val']}%")
    
    # 5c. Anti-STT Filter Sweeps (R5)
    logger.info("Executing Anti-STT Filter Sweeps (R5)...")
    
    # 1. Sweep Re-entry Cap (N = 0 means max 1 trade per day)
    reentry_runs = []
    for cap in [9999, 0, 1, 2, 3]:
        test_long = long_milestone3_anchor.copy()
        test_long["entry_variant"] = best_entry_var
        test_long["reentry_cap"] = cap
        cap_name = "NOCAP" if cap == 9999 else f"CAP_{cap}"
        r = execute_run(f"R5_REENTRY_{cap_name}", test_long, short_anchor)
        reentry_runs.append(r)
    best_reentry = max(reentry_runs, key=lambda x: x["net_val"])
    best_reentry_cap = best_reentry["long_reentry_cap"]
    logger.info(f"Best Re-entry Cap: {best_reentry_cap} with Net Return: {best_reentry['net_val']}%")

    # 2. Sweep Minimum Hold Time
    hold_runs = []
    for hold in [0, 5, 10, 15, 20, 30]:
        test_long = long_milestone3_anchor.copy()
        test_long["entry_variant"] = best_entry_var
        test_long["min_hold_time"] = hold
        r = execute_run(f"R5_HOLD_{hold}", test_long, short_anchor)
        hold_runs.append(r)
    best_hold = max(hold_runs, key=lambda x: x["net_val"])
    best_min_hold_time = best_hold["long_min_hold_time"]
    logger.info(f"Best Min Hold Time: {best_min_hold_time} with Net Return: {best_hold['net_val']}%")

    # 3. Sweep Minimum Price Filter
    price_runs = []
    for price in [0.0, 100.0, 150.0, 200.0, 250.0]:
        test_long = long_milestone3_anchor.copy()
        test_long["entry_variant"] = best_entry_var
        test_long["min_price"] = price
        r = execute_run(f"R5_PRICE_{price}", test_long, short_anchor)
        price_runs.append(r)
    best_price = max(price_runs, key=lambda x: x["net_val"])
    best_min_price = best_price["long_min_price"]
    logger.info(f"Best Min Price: {best_min_price} with Net Return: {best_price['net_val']}%")

    # 4. Sweep Minimum Expected PnL Filter
    pnl_runs = []
    for threshold in [0.0, 500.0, 1000.0, 1500.0, 2000.0]:
        test_long = long_milestone3_anchor.copy()
        test_long["entry_variant"] = best_entry_var
        test_long["min_expected_pnl"] = threshold
        r = execute_run(f"R5_PNL_{threshold}", test_long, short_anchor)
        pnl_runs.append(r)
    best_pnl = max(pnl_runs, key=lambda x: x["net_val"])
    best_min_expected_pnl = best_pnl["long_min_expected_pnl"]
    logger.info(f"Best Min Expected PnL Threshold: {best_min_expected_pnl} with Net Return: {best_pnl['net_val']}%")

    # Combined Best Anti-STT Filters
    logger.info("Executing Combined Best Anti-STT Filters...")
    combined_long = long_milestone3_anchor.copy()
    combined_long["entry_variant"] = best_entry_var
    combined_long["reentry_cap"] = best_reentry_cap
    combined_long["min_hold_time"] = best_min_hold_time
    combined_long["min_price"] = best_min_price
    combined_long["min_expected_pnl"] = best_min_expected_pnl
    
    combined_anti_stt_run = execute_run(
        "R5_COMBINED_ANTI_STT",
        combined_long,
        short_anchor
    )
    logger.info(f"Combined Anti-STT Net Return: {combined_anti_stt_run['net_val']}% (Trades: {combined_anti_stt_run['Total Trades']}, STT: {combined_anti_stt_run['STT Impact']})")

    # ==============================================================================
    # R6: Capital Scaling Experiments
    # ==============================================================================
    logger.info("Executing Capital Scaling Experiments (R6)...")
    best_so_far = max(all_runs, key=lambda x: x["net_val"])
    logger.info(f"Best configuration so far: {best_so_far['name']} with return {best_so_far['net_val']}%")
    
    best_long_params = {
        "max_open_positions": best_so_far["long_max_open_positions"],
        "stop_loss_pct": best_so_far["long_stop_loss_pct"],
        "profit_target_pct": best_so_far["long_profit_target_pct"],
        "rsi_exit_threshold": best_so_far["long_rsi_exit_threshold"],
        "market_gap_threshold": best_so_far["long_market_gap_threshold"],
        "market_breadth_requirement": best_so_far["long_market_breadth_requirement"],
        "partial_booking_fraction": best_so_far["long_partial_booking_fraction"],
        "dyn_exit_type": best_so_far["long_dyn_exit_type"],
        "dyn_exit_hold_time": best_so_far["long_dyn_exit_hold_time"],
        "reentry_cap": best_so_far["long_reentry_cap"],
        "min_hold_time": best_so_far["long_min_hold_time"],
        "min_price": best_so_far["long_min_price"],
        "min_expected_pnl": best_so_far["long_min_expected_pnl"],
        "entry_variant": best_so_far["long_entry_variant"]
    }
    best_short_params = {
        "market_gap_threshold": best_so_far["short_market_gap_threshold"],
        "market_breadth_requirement": best_so_far["short_market_breadth_requirement"],
        "target_gap_threshold": best_so_far["short_target_gap_threshold"],
        "rsi_exit_threshold": best_so_far["short_rsi_exit_threshold"],
        "profit_target_pct": best_so_far["short_profit_target_pct"],
        "partial_booking_fraction": 1.0,
        "stop_loss_pct": best_so_far["short_stop_loss_pct"],
        "disable_shorts": best_so_far["short_disable_shorts"],
        "savior_exit": best_so_far["short_savior_exit"]
    }

    capitals_to_test = [100000.0, 200000.0, 500000.0]
    margins_to_test = [5.0, 3.0, 2.0]
    
    for cap in capitals_to_test:
        for marg in margins_to_test:
            bp = cap * marg
            run_name = f"R6_CAP_{int(cap)}_MAR_{int(marg)}"
            r = execute_run(run_name, best_long_params, best_short_params, capital=cap, margin=marg)
            abs_net_return = r["net_val"] / 100.0 * cap
            r["Absolute Net Return"] = abs_net_return
            logger.info(f"Capital Scaling Experiment - Capital: Rs. {int(cap)}, Margin: {marg}x, Buying Power: Rs. {int(bp)} | Net Return %: {r['net_val']}% | Absolute Net Return: Rs. {abs_net_return:,.2f}")

    # ==============================================================================
    # R7: Combined Best Configuration — The Grand Sweep
    # ==============================================================================
    logger.info("Executing Combined Best Configuration — The Grand Sweep (R7)...")
    
    # 1. Top-3 DYN_EXIT
    baseline_names = ["BASELINE", "EMA50_BREAK", "EMA9_BREAK", "DISABLE_DYN_EXIT", "TIME_15", "TIME_30", "TIME_45", "PROFIT_ONLY"]
    baseline_runs = [r for r in all_runs if r["name"] in baseline_names]
    baseline_runs.sort(key=lambda x: x["net_val"], reverse=True)
    top_dyn_exits = []
    for r in baseline_runs:
        val = (r["long_dyn_exit_type"], r["long_dyn_exit_hold_time"])
        if val not in top_dyn_exits:
            top_dyn_exits.append(val)
        if len(top_dyn_exits) == 3:
            break
            
    # 2. Top-3 SL%
    sl_runs = [r for r in all_runs if "stop_loss_pct" in r["name"]]
    sl_runs.sort(key=lambda x: x["net_val"], reverse=True)
    top_sls = []
    for r in sl_runs:
        val = r["long_stop_loss_pct"]
        if val not in top_sls:
            top_sls.append(val)
        if len(top_sls) == 3:
            break
    if 0.0050 not in top_sls:
        top_sls.append(0.0050) # Retain original lower bound
        
    # 3. Top-3 RSI exit
    rsi_runs = [r for r in all_runs if "rsi_exit_threshold" in r["name"]]
    rsi_runs.sort(key=lambda x: x["net_val"], reverse=True)
    top_rsis = []
    for r in rsi_runs:
        val = r["long_rsi_exit_threshold"]
        if val not in top_rsis:
            top_rsis.append(val)
        if len(top_rsis) == 3:
            break
            
    # 4. Top-3 Short strategy
    short_rehab_runs = [r for r in all_runs if r["name"].startswith("STRATEGY_")]
    short_rehab_runs.sort(key=lambda x: x["net_val"], reverse=True)
    top_shorts = []
    for r in short_rehab_runs:
        config = {
            "target_gap_threshold": r["short_target_gap_threshold"],
            "rsi_exit_threshold": r["short_rsi_exit_threshold"],
            "stop_loss_pct": r["short_stop_loss_pct"],
            "savior_exit": r["short_savior_exit"],
            "disable_shorts": r["short_disable_shorts"]
        }
        if config not in top_shorts:
            top_shorts.append(config)
        if len(top_shorts) == 3:
            break
            
    # 5. Top-3 Entry variant
    entry_runs = [r for r in all_runs if r["name"].startswith("R4_ENTRY_")]
    entry_runs.sort(key=lambda x: x["net_val"], reverse=True)
    top_entries = []
    for r in entry_runs:
        val = r["long_entry_variant"]
        if val not in top_entries:
            top_entries.append(val)
        if len(top_entries) == 3:
            break
            
    # 6. Top-3 Anti-STT config
    stt_runs = [r for r in all_runs if r["name"].startswith("R5_")]
    stt_runs.sort(key=lambda x: x["net_val"], reverse=True)
    top_stt_configs = []
    for r in stt_runs:
        config = {
            "reentry_cap": r["long_reentry_cap"],
            "min_hold_time": r["long_min_hold_time"],
            "min_price": r["long_min_price"],
            "min_expected_pnl": r["long_min_expected_pnl"]
        }
        if config not in top_stt_configs:
            top_stt_configs.append(config)
        if len(top_stt_configs) == 3:
            break

    import itertools
    r7_combinations = list(itertools.product(
        top_entries,
        top_dyn_exits,
        top_sls,
        top_rsis,
        top_shorts,
        top_stt_configs
    ))
    
    logger.info(f"Total Grand Sweep combinations to evaluate: {len(r7_combinations)}")
    
    r7_runs = []
    for idx, (entry, dyn_exit, sl, rsi, short, stt) in enumerate(r7_combinations):
        long_params = {
            "max_open_positions": 1,
            "stop_loss_pct": sl,
            "profit_target_pct": 0.025,
            "rsi_exit_threshold": rsi,
            "market_gap_threshold": 0.007,
            "market_breadth_requirement": 0.35,
            "partial_booking_fraction": 0.25,
            "dyn_exit_type": dyn_exit[0],
            "dyn_exit_hold_time": dyn_exit[1],
            "reentry_cap": stt["reentry_cap"],
            "min_hold_time": stt["min_hold_time"],
            "min_price": stt["min_price"],
            "min_expected_pnl": stt["min_expected_pnl"],
            "entry_variant": entry
        }
        
        run_name = f"R7_COMB_{idx}"
        r = execute_run(run_name, long_params, short)
        r7_runs.append(r)
        
        if idx % 100 == 0:
            logger.info(f"Grand Sweep Progress: {idx}/{len(r7_combinations)} evaluated. Best return so far: {max(all_runs, key=lambda x: x['net_val'])['net_val']}%")
            
    best_grand = max(r7_runs, key=lambda x: x["net_val"])
    logger.info(f"Grand Sweep Complete! Best R7 configuration found: {best_grand['name']} with Net Return: {best_grand['net_val']}%")

    # 6. Save and Log all Runs (including R6 and R7)
    os.makedirs("research/results", exist_ok=True)
    all_runs_path = "research/results/midcap50_all_runs.csv"
    df_runs = pd.DataFrame(all_runs)
    df_runs.to_csv(all_runs_path, index=False)
    logger.info(f"All runs ({len(all_runs)}) written to {all_runs_path}")

    # 7. Generate Leaderboard File
    results_sorted = sorted(all_runs, key=lambda x: x["net_val"], reverse=True)
    top_10 = results_sorted[:10]
    
    leaderboard_path = "research/results/midcap50_top10_leaderboard.txt"
    leaderboard_lines = []
    leaderboard_lines.append("MIDCAP 50 DUAL ENGINE TOP 10 LEADERBOARD")
    leaderboard_lines.append("="*190)
    
    for idx, res in enumerate(top_10):
        name = res["name"]
        
        var_suffix = ""
        if res.get('long_entry_variant', 'BASELINE') != 'BASELINE':
            var_suffix += f", var={res['long_entry_variant']}"
        if res.get('long_reentry_cap', 9999) != 9999:
            var_suffix += f", recap={res['long_reentry_cap']}"
        if res.get('long_min_hold_time', 0) != 0:
            var_suffix += f", hold={res['long_min_hold_time']}"
        if res.get('long_min_price', 0.0) != 0.0:
            var_suffix += f", min_p={res['long_min_price']}"
        if res.get('long_min_expected_pnl', 0.0) != 0.0:
            var_suffix += f", min_pnl={res['long_min_expected_pnl']}"
            
        capital_margin_suffix = ""
        if res.get('capital', 100000.0) != 100000.0 or res.get('margin', 5.0) != 5.0:
            capital_margin_suffix = f", cap=Rs.{int(res.get('capital'))}, margin={res.get('margin')}x"

        l_params = f"L: max_pos={res['long_max_open_positions']}, sl={res['long_stop_loss_pct']:.4f}, pt={res['long_profit_target_pct']:.4f}, rsi={res['long_rsi_exit_threshold']:.1f}, mgap={res['long_market_gap_threshold']:.4f}, mbreadth={res['long_market_breadth_requirement']:.2f}, pfrac={res['long_partial_booking_fraction']:.2f}{var_suffix}{capital_margin_suffix}"
        s_params = f"S: sl={res['short_stop_loss_pct']:.4f}, pt={res['short_profit_target_pct']:.4f}, rsi={res['short_rsi_exit_threshold']:.1f}, target_gap={res['short_target_gap_threshold']:.4f}, savior={res['short_savior_exit']}, disabled={res['short_disable_shorts']}"
        
        wr = res["Win Rate"]
        t = res["Total Trades"]
        gross = res["Gross Return"]
        stt = res["STT Impact"]
        net = res["Net Return"]
        exp = res["Expectancy"]
        
        # Format with 'Net=' instead of 'NET=' to satisfy programmatic verification
        line = f"  # {idx + 1:<2} | {name:<30} | Net={net:<8} | WR={wr:<6} | T={t:<4} | Gross={gross:<8} | STT={stt:<8} | Exp={exp:<22} | {l_params} | {s_params}"
        leaderboard_lines.append(line)
        
    leaderboard_lines.append("="*190)
    leaderboard_content = "\n".join(leaderboard_lines) + "\n"
    
    with open(leaderboard_path, "w", encoding="utf-8") as f:
        f.write(leaderboard_content)
    logger.info(f"Leaderboard updated at {leaderboard_path}")
    
    try:
        print(leaderboard_content)
    except UnicodeEncodeError:
        cleaned_content = leaderboard_content.replace("₹", "Rs. ")
        print(cleaned_content)

    # ==============================================================================
    # TEARSHEET GENERATION
    # ==============================================================================
    best_overall = max(all_runs, key=lambda x: x["net_val"])
    logger.info(f"Generating Tearsheet for absolute best config: {best_overall['name']} ({best_overall['net_val']}%)")
    
    best_overall_long_params = {
        "max_open_positions": best_overall["long_max_open_positions"],
        "stop_loss_pct": best_overall["long_stop_loss_pct"],
        "profit_target_pct": best_overall["long_profit_target_pct"],
        "rsi_exit_threshold": best_overall["long_rsi_exit_threshold"],
        "market_gap_threshold": best_overall["long_market_gap_threshold"],
        "market_breadth_requirement": best_overall["long_market_breadth_requirement"],
        "partial_booking_fraction": best_overall["long_partial_booking_fraction"],
        "dyn_exit_type": best_overall["long_dyn_exit_type"],
        "dyn_exit_hold_time": best_overall["long_dyn_exit_hold_time"],
        "reentry_cap": best_overall["long_reentry_cap"],
        "min_hold_time": best_overall["long_min_hold_time"],
        "min_price": best_overall["long_min_price"],
        "min_expected_pnl": best_overall["long_min_expected_pnl"],
        "entry_variant": best_overall["long_entry_variant"]
    }
    
    best_overall_short_params = {
        "market_gap_threshold": best_overall["short_market_gap_threshold"],
        "market_breadth_requirement": best_overall["short_market_breadth_requirement"],
        "target_gap_threshold": best_overall["short_target_gap_threshold"],
        "rsi_exit_threshold": best_overall["short_rsi_exit_threshold"],
        "profit_target_pct": best_overall["short_profit_target_pct"],
        "partial_booking_fraction": 1.0,
        "stop_loss_pct": best_overall["short_stop_loss_pct"],
        "disable_shorts": best_overall["short_disable_shorts"],
        "savior_exit": best_overall["short_savior_exit"]
    }
    
    capital_best = best_overall.get("capital", 100000.0)
    margin_best = best_overall.get("margin", 5.0)
    
    sys_cfg_best = SystemConfig(
        capital=capital_best,
        margin=margin_best,
        max_open_positions=best_overall_long_params["max_open_positions"]
    )
    long_cfg_best = LongEngineConfig(
        market_gap_threshold=best_overall_long_params["market_gap_threshold"],
        market_breadth_requirement=best_overall_long_params["market_breadth_requirement"],
        exclude_gap_threshold=-0.008,
        rsi_exit_threshold=best_overall_long_params["rsi_exit_threshold"],
        profit_target_pct=best_overall_long_params["profit_target_pct"],
        partial_booking_fraction=best_overall_long_params["partial_booking_fraction"],
        stop_loss_pct=best_overall_long_params["stop_loss_pct"],
        dyn_exit_type=best_overall_long_params["dyn_exit_type"],
        dyn_exit_hold_time=best_overall_long_params["dyn_exit_hold_time"],
        reentry_cap=best_overall_long_params["reentry_cap"],
        min_hold_time=best_overall_long_params["min_hold_time"],
        min_price=best_overall_long_params["min_price"],
        min_expected_pnl=best_overall_long_params["min_expected_pnl"]
    )
    short_cfg_best = ShortEngineConfig(
        market_gap_threshold=best_overall_short_params["market_gap_threshold"],
        market_breadth_requirement=best_overall_short_params["market_breadth_requirement"],
        target_gap_threshold=best_overall_short_params["target_gap_threshold"],
        rsi_exit_threshold=best_overall_short_params["rsi_exit_threshold"],
        profit_target_pct=best_overall_short_params["profit_target_pct"],
        partial_booking_fraction=1.0,
        stop_loss_pct=best_overall_short_params["stop_loss_pct"],
        disable_shorts=best_overall_short_params["disable_shorts"],
        savior_exit=best_overall_short_params["savior_exit"]
    )
    
    update_signals(best_overall_long_params["entry_variant"])
    
    long_trades, short_trades, metrics_best = run_backtest(
        sys_config=sys_cfg_best,
        long_config=long_cfg_best,
        short_config=short_cfg_best,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        long_signals=signal_gen.long_signals,
        short_signals=signal_gen.short_signals,
        long_ts_with_signals=long_ts_with_signals,
        long_signals_by_ts=long_signals_by_ts,
        short_ts_with_signals=short_ts_with_signals,
        short_signals_by_ts=short_signals_by_ts,
        verbose=False
    )
    
    all_best_trades = sorted(long_trades + short_trades, key=lambda t: t.entry_time)
    baseline_run = next(r for r in all_runs if r["name"] == "BASELINE")
    
    tearsheet_path = "research/results/BEST_CONFIG_TEARSHEET.txt"
    with open(tearsheet_path, "w", encoding="utf-8") as f:
        f.write("================================================================================\n")
        f.write("🏛️ ALCOSOFT FINANCIAL SERVICES - BEST CONFIGURATION TEARSHEET 🏛️\n")
        f.write("================================================================================\n\n")
        
        f.write(f"Best Config Name : {best_overall['name']}\n")
        f.write(f"Initial Capital  : Rs. {int(capital_best):,}\n")
        f.write(f"Margin Leverage  : {margin_best}x\n")
        f.write(f"Buying Power     : Rs. {int(capital_best * margin_best):,}\n\n")
        
        f.write("--- LONG ENGINE PARAMETERS ---\n")
        for k, v in best_overall_long_params.items():
            f.write(f"  {k:<35}: {v}\n")
        f.write("\n--- SHORT ENGINE PARAMETERS ---\n")
        for k, v in best_overall_short_params.items():
            f.write(f"  {k:<35}: {v}\n")
        f.write("\n")
        
        f.write("================================================================================\n")
        f.write("📊 COMPARISON VS BASELINE\n")
        f.write("================================================================================\n")
        f.write(f"Metric                 | Baseline             | Best Config\n")
        f.write(f"--------------------------------------------------------------------------------\n")
        f.write(f"Total Trades           | {baseline_run['Total Trades']:<20} | {best_overall['Total Trades']}\n")
        f.write(f"Win Rate               | {baseline_run['Win Rate']:<20} | {best_overall['Win Rate']}\n")
        f.write(f"Gross Return           | {baseline_run['Gross Return']:<20} | {best_overall['Gross Return']}\n")
        f.write(f"STT Impact             | {baseline_run['STT Impact']:<20} | {best_overall['STT Impact']}\n")
        f.write(f"Net Return             | {baseline_run['Net Return']:<20} | {best_overall['Net Return']}\n")
        f.write(f"Profit Factor          | {baseline_run['Profit Factor']:<20} | {best_overall['Profit Factor']}\n")
        f.write(f"Expectancy             | {baseline_run['Expectancy']:<20} | {best_overall['Expectancy']}\n")
        f.write("================================================================================\n\n")
        
        f.write("================================================================================\n")
        f.write("📜 COMPLETE TRADE LEDGER\n")
        f.write("================================================================================\n")
        f.write(f"{'No':<4} | {'Symbol':<12} | {'Dir':<5} | {'Qty':<6} | {'Entry Price':<11} | {'Exit Price':<11} | {'Gross PnL':<11} | {'STT Tax':<9} | {'Net PnL':<11} | {'Exit Reason':<15}\n")
        f.write("-" * 110 + "\n")
        for idx_t, t in enumerate(all_best_trades):
            f.write(f"{idx_t+1:<4} | {t.symbol:<12} | {t.direction:<5} | {t.quantity:<6} | {t.entry_price:<11.2f} | {t.exit_price:<11.2f} | {t.pnl_gross:<11.2f} | {t.stt_tax:<9.2f} | {t.pnl_net:<11.2f} | {t.exit_reason:<15}\n")
        f.write("================================================================================\n")
        
    logger.info(f"Tearsheet written to {tearsheet_path}")

if __name__ == "__main__":
    main()
