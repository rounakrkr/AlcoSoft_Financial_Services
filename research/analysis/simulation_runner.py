import sys
import os
import math
import logging
import datetime
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, Any

import pandas as pd
import numpy as np
import ta
import matplotlib.pyplot as plt
import seaborn as sns

# Ensure we can load local modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from research.build_cache import load_cache
from core.strategy_sets import load_strategy_sets
from core.strategy import StrategySetEvaluator, StrategyEvaluationContext, CONDITION_REGISTRY
from core.order_executor import round_to_tick

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] - %(message)s")
logger = logging.getLogger("SimulationRunner")

@dataclass
class TradeRecord:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: int
    direction: str  # 'LONG' or 'SHORT'
    exit_reason: str # 'PARTIAL_PROFIT', 'RSI_OVERBOUGHT', 'RSI_OVERSOLD', 'STOP_LOSS', 'EOD_TIME', 'DYN_EXIT', 'DYN_SL_LAG1'
    
    @property
    def pnl_gross(self) -> float:
        if self.direction == "LONG":
            return (self.exit_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - self.exit_price) * self.quantity

    @property
    def stt_tax(self) -> float:
        # 0.035% of sell turnover (or entry for short, exit for long)
        sell_price = self.entry_price if self.direction == "SHORT" else self.exit_price
        turnover = sell_price * self.quantity
        return turnover * 0.00035

    @property
    def pnl_net(self) -> float:
        return self.pnl_gross - self.stt_tax

@dataclass
class OpenPosition:
    symbol: str
    entry_time: pd.Timestamp
    entry_price: float
    quantity: int
    direction: str
    stop_loss_price: float
    partial_booked: bool = False
    tsl_active: bool = False

class MarketRegimeAnalyzer:
    def __init__(self, stock_dfs: Dict[str, pd.DataFrame]):
        self.stock_dfs = stock_dfs
        self.all_daily_gaps: Dict[Tuple[datetime.date, str], float] = {}
        self.trading_dates: List[datetime.date] = []
        self._compute_gaps()

    def _compute_gaps(self):
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

    def get_bull_days(self, threshold: float = 0.010, breadth: float = 0.40) -> Set[datetime.date]:
        bull_days = set()
        for curr_d in self.trading_dates:
            gaps = [g for (d, s), g in self.all_daily_gaps.items() if d == curr_d]
            if not gaps:
                continue
            qualified = sum(1 for g in gaps if g >= threshold)
            ratio = qualified / len(gaps)
            if ratio >= breadth:
                bull_days.add(curr_d)
        return bull_days

    def get_bear_days(self, threshold: float = -0.006, breadth: float = 0.40) -> Set[datetime.date]:
        bear_days = set()
        for curr_d in self.trading_dates:
            gaps = [g for (d, s), g in self.all_daily_gaps.items() if d == curr_d]
            if not gaps:
                continue
            qualified = sum(1 for g in gaps if g <= threshold)
            ratio = qualified / len(gaps)
            if ratio >= breadth:
                bear_days.add(curr_d)
        return bear_days

    def get_gap(self, date: datetime.date, symbol: str) -> float:
        return self.all_daily_gaps.get((date, symbol), 0.0)

def run_simulation(
    stock_dfs, regime, timeline, stock_ts_map, long_signals, short_signals,
    bull_days, bear_days, evaluator, buy_set_long, buy_set_short, cover_set_long,
    max_open_positions: int = 3,
    long_rsi_mode: str = "14_LAG1_72",
    long_kinetic: bool = False,
    long_tp: float = 0.005,
    long_frac: float = 0.75,
    long_sl: float = 0.010,
    short_sl: Optional[float] = None,
    short_tsl: Optional[float] = None,
    short_tp: float = 0.005,
    short_frac: float = 0.75,
    short_rsi_exit: float = 15.0,
    short_rsi_mode: str = "16_LAG1",
    short_dyn_sl: bool = True,
    short_block_entry: bool = True
) -> List[TradeRecord]:
    capital = 100000.0
    buying_power = capital * 5.0
    cap_per_trade = buying_power / max_open_positions
    
    # Parse Long RSI
    l_rsi_period = 14 if "14" in long_rsi_mode else 13
    l_rsi_col = f"rsi_{l_rsi_period}"
    l_rsi_thr = 72.0 if "72" in long_rsi_mode else 85.0
    l_rsi_lag = 0 if "LAG0" in long_rsi_mode else 1

    trades: List[TradeRecord] = []
    
    # Run Long Engine
    open_positions: Dict[str, OpenPosition] = {}
    for ts in timeline:
        date_only = ts.date()
        is_bull_day = date_only in bull_days
        
        # Manage positions
        syms_to_close = []
        for sym, pos in open_positions.items():
            if ts not in stock_ts_map.get(sym, {}):
                continue
            idx = stock_ts_map[sym][ts]
            df = stock_dfs[sym]
            cc = df.iloc[idx]
            cp, lp, op, hp = float(cc["close"]), float(cc["low"]), float(cc["open"]), float(cc["high"])
            ep = pos.entry_price
            
            # EOD
            if ts.hour == 15 and ts.minute >= 15:
                trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=ep, exit_price=cp, quantity=pos.quantity,
                    direction="LONG", exit_reason="EOD_TIME"
                ))
                syms_to_close.append(sym)
                continue
                
            # Stop Loss
            if lp <= pos.stop_loss_price:
                exit_price = min(pos.stop_loss_price, op)
                trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=ep, exit_price=exit_price, quantity=pos.quantity,
                    direction="LONG", exit_reason="STOP_LOSS"
                ))
                syms_to_close.append(sym)
                continue
                
            # RSI Exit
            if ts > pos.entry_time and idx >= 1:
                chk_idx = idx if l_rsi_lag == 0 else idx - 1
                curr_rsi = df[l_rsi_col].iloc[chk_idx]
                if curr_rsi >= l_rsi_thr:
                    exit_price = cp if l_rsi_lag == 0 else op
                    trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=ep, exit_price=exit_price, quantity=pos.quantity,
                        direction="LONG", exit_reason="RSI_OVERBOUGHT"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
            # Profit Target & Kinetic Profit Booking
            if not pos.partial_booked:
                target_price = ep * (1 + long_tp)
                if hp >= target_price:
                    pos.partial_booked = True
                    if long_kinetic:
                        # Check Kinetic condition: RSI[-2] > RSI[-3]
                        rsi_prev1 = df[l_rsi_col].iloc[idx-1] if idx >= 1 else 50.0
                        rsi_prev2 = df[l_rsi_col].iloc[idx-2] if idx >= 2 else 50.0
                        is_strong = rsi_prev1 > rsi_prev2
                        if is_strong:
                            # Hold full position
                            pass
                        else:
                            # Book fraction (e.g. 75%)
                            exit_price = max(target_price, op)
                            cover_qty = max(1, int(pos.quantity * long_frac))
                            if cover_qty >= pos.quantity:
                                cover_qty = max(0, pos.quantity - 1)
                            if cover_qty > 0:
                                trades.append(TradeRecord(
                                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                                    entry_price=ep, exit_price=exit_price, quantity=cover_qty,
                                    direction="LONG", exit_reason="PARTIAL_PROFIT"
                                ))
                                pos.quantity -= cover_qty
                            if pos.quantity <= 0:
                                syms_to_close.append(sym)
                                continue
                    else:
                        # Blind Profit Booking
                        exit_price = max(target_price, op)
                        cover_qty = max(1, int(pos.quantity * long_frac))
                        if cover_qty >= pos.quantity:
                            cover_qty = max(0, pos.quantity - 1)
                        if cover_qty > 0:
                            trades.append(TradeRecord(
                                symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                                entry_price=ep, exit_price=exit_price, quantity=cover_qty,
                                direction="LONG", exit_reason="PARTIAL_PROFIT"
                            ))
                            pos.quantity -= cover_qty
                        if pos.quantity <= 0:
                            syms_to_close.append(sym)
                            continue
                            
            # Dynamic Exit
            sliced = df.iloc[:idx+1]
            ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
            cond = evaluator._evaluate_conditions(cover_set_long, ctx)
            if cond and all(r.get("fired") for r in cond):
                if idx + 1 < len(df):
                    exit_price = float(df.iloc[idx+1]["open"])
                    exit_ts = df.index[idx+1]
                    trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=exit_ts,
                        entry_price=ep, exit_price=exit_price, quantity=pos.quantity,
                        direction="LONG", exit_reason="DYN_EXIT"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
        for sym in syms_to_close:
            if sym in open_positions:
                del open_positions[sym]
                
        # Scan for entries
        if is_bull_day and ts.hour < 15 and len(open_positions) < max_open_positions:
            for sym in stock_dfs:
                if len(open_positions) >= max_open_positions:
                    break
                if sym in open_positions:
                    continue
                gap_pct = regime.get_gap(date_only, sym)
                if gap_pct <= -0.008:
                    continue  # Segregation exclusion
                idx = stock_ts_map[sym].get(ts, -1)
                if idx == -1 or not long_signals[sym][idx]:
                    continue
                
                df = stock_dfs[sym]
                sliced = df.iloc[:idx+1]
                cover_ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                cover_cond = evaluator._evaluate_conditions(cover_set_long, cover_ctx)
                if cover_cond and all(r.get("fired") for r in cover_cond):
                    continue
                    
                if idx + 1 < len(df):
                    entry_price = float(df.iloc[idx+1]["open"])
                    entry_time = df.index[idx+1]
                    qty = int(cap_per_trade // entry_price)
                    if qty > 0:
                        sl_price = entry_price * (1 - long_sl)
                        open_positions[sym] = OpenPosition(
                            symbol=sym, entry_time=entry_time, entry_price=entry_price,
                            quantity=qty, direction="LONG", stop_loss_price=sl_price
                        )
                        
    # Run Short Engine
    open_positions.clear()
    
    def prev_high_break(ctx):
        df = ctx.indicator_df
        if len(df) < 2: return {"fired": False}
        c1 = df["close"].iloc[-1]
        h2 = df["high"].iloc[-2]
        return {"fired": bool(c1 > h2)}
        
    for ts in timeline:
        date_only = ts.date()
        is_bear_day = date_only in bear_days
        
        # Manage positions
        syms_to_close = []
        for sym, pos in open_positions.items():
            if ts not in stock_ts_map.get(sym, {}):
                continue
            idx = stock_ts_map[sym][ts]
            df = stock_dfs[sym]
            cc = df.iloc[idx]
            cp, lp, op, hp = float(cc["close"]), float(cc["low"]), float(cc["open"]), float(cc["high"])
            ep = pos.entry_price
            
            # EOD
            if ts.hour == 15 and ts.minute >= 15:
                trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=ep, exit_price=cp, quantity=pos.quantity,
                    direction="SHORT", exit_reason="EOD_TIME"
                ))
                syms_to_close.append(sym)
                continue
                
            # Dynamic SL Lag-1
            if short_dyn_sl and ts > pos.entry_time and idx >= 2:
                c1 = float(df["close"].iloc[idx-1])
                h2 = float(df["high"].iloc[idx-2])
                if c1 > h2:
                    trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=ep, exit_price=op, quantity=pos.quantity,
                        direction="SHORT", exit_reason="DYN_SL_LAG1"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
            # Stop Loss (hard or trailing)
            if short_sl is not None:
                if short_tsl is not None and ts > pos.entry_time and idx >= 1:
                    prev_lp = float(df["low"].iloc[idx-1])
                    if not pos.tsl_active:
                        if prev_lp <= ep * 0.995:
                            pos.tsl_active = True
                            pos.stop_loss_price = min(pos.stop_loss_price, round_to_tick(prev_lp * (1 + short_tsl)))
                    else:
                        pos.stop_loss_price = min(pos.stop_loss_price, round_to_tick(prev_lp * (1 + short_tsl)))
                
                if hp >= pos.stop_loss_price:
                    exit_price = max(pos.stop_loss_price, op)
                    trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=ep, exit_price=exit_price, quantity=pos.quantity,
                        direction="SHORT", exit_reason="STOP_LOSS"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
            # RSI Exit
            if ts > pos.entry_time and idx >= 1:
                chk_idx = idx if short_rsi_mode == "16_LAG0" else idx - 1
                curr_rsi = df["rsi_16"].iloc[chk_idx]
                if curr_rsi <= short_rsi_exit:
                    exit_price = cp if short_rsi_mode == "16_LAG0" else op
                    trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=ep, exit_price=exit_price, quantity=pos.quantity,
                        direction="SHORT", exit_reason="RSI_OVERSOLD"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
            # Profit Target Booking
            if not pos.partial_booked:
                target_price = ep * (1 - short_tp)
                if lp <= target_price:
                    exit_price = min(target_price, op)
                    cover_qty = max(1, int(pos.quantity * short_frac))
                    if cover_qty >= pos.quantity:
                        cover_qty = max(0, pos.quantity - 1)
                    if cover_qty > 0:
                        trades.append(TradeRecord(
                            symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                            entry_price=ep, exit_price=exit_price, quantity=cover_qty,
                            direction="SHORT", exit_reason="PARTIAL_PROFIT"
                        ))
                        pos.quantity -= cover_qty
                        pos.partial_booked = True
                        if short_tsl is not None:
                            pos.tsl_active = True
                    if pos.quantity <= 0:
                        syms_to_close.append(sym)
                        continue
                        
        for sym in syms_to_close:
            if sym in open_positions:
                del open_positions[sym]
                
        # Scan for entries
        if is_bear_day and ts.hour < 15 and len(open_positions) < max_open_positions:
            for sym in stock_dfs:
                if len(open_positions) >= max_open_positions:
                    break
                if sym in open_positions:
                    continue
                gap_pct = regime.get_gap(date_only, sym)
                if gap_pct > -0.008:
                    continue  # Only gap down <= -0.8%
                idx = stock_ts_map[sym].get(ts, -1)
                if idx == -1 or not short_signals[sym][idx]:
                    continue
                
                df = stock_dfs[sym]
                if short_block_entry:
                    sliced = df.iloc[:idx+1]
                    cover_ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                    if prev_high_break(cover_ctx)["fired"]:
                        continue
                    
                if idx + 1 < len(df):
                    entry_price = float(df.iloc[idx+1]["open"])
                    entry_time = df.index[idx+1]
                    qty = int(cap_per_trade // entry_price)
                    if qty > 0:
                        initial_sl = short_sl if short_sl is not None else 0.010
                        sl_price = entry_price * (1 + initial_sl)
                        open_positions[sym] = OpenPosition(
                            symbol=sym, entry_time=entry_time, entry_price=entry_price,
                            quantity=qty, direction="SHORT", stop_loss_price=sl_price
                        )
                        
    return trades

def calculate_metrics(trades: List[TradeRecord], capital: float) -> Dict[str, Any]:
    if not trades:
        return {
            "Total Trades": 0, "Win Rate": "0.00%", "Gross Return": "0.00%",
            "STT Impact": "0.00%", "Net Return": "0.00%", "Profit Factor": "0.00",
            "Avg Win": "₹0.00", "Avg Loss": "₹0.00", "Expectancy": "₹0.00"
        }
    gross_pnl = sum(t.pnl_gross for t in trades)
    stt_tax = sum(t.stt_tax for t in trades)
    net_pnl = gross_pnl - stt_tax
    
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    
    win_rate = (len(wins) / len(trades)) * 100
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
        "Net Return Float": net_pnl / capital * 100,
        "Profit Factor": f"{profit_factor:.2f}",
        "Avg Win": f"Rs.{avg_win:.2f}",
        "Avg Loss": f"Rs.{avg_loss:.2f}",
        "Expectancy": f"Rs.{expectancy:.2f} per trade"
    }

def print_table(title: str, long_trades: List[TradeRecord], short_trades: List[TradeRecord], capital: float):
    print("="*80)
    print(title.upper())
    print("="*80)
    
    long_metrics = calculate_metrics(long_trades, capital)
    short_metrics = calculate_metrics(short_trades, capital)
    combined_metrics = calculate_metrics(long_trades + short_trades, capital)
    
    print(f"{'Metric':<25} | {'Long Engine':<15} | {'Short Engine':<15} | {'Combined':<15}")
    print("-" * 78)
    for k in ["Total Trades", "Win Rate", "Gross Return", "STT Impact", "Net Return", "Profit Factor", "Avg Win", "Avg Loss", "Expectancy"]:
        print(f"{k:<25} | {str(long_metrics.get(k, 'N/A')):<15} | {str(short_metrics.get(k, 'N/A')):<15} | {str(combined_metrics.get(k, 'N/A')):<15}")
    print()

def main():
    logger.info("Loading cache data...")
    stock_dfs = load_cache()
    
    # Calculate Indicators
    for sym, df in stock_dfs.items():
        df["rsi_13"] = ta.momentum.rsi(df["close"], window=13).fillna(50.0)
        df["rsi_14"] = ta.momentum.rsi(df["close"], window=14).fillna(50.0)
        df["rsi_16"] = ta.momentum.rsi(df["close"], window=16).fillna(50.0)
        df["ema21"] = ta.trend.ema_indicator(df["close"], window=21).fillna(method="bfill")
        
    regime = MarketRegimeAnalyzer(stock_dfs)
    
    # Load config and precompute entry signals
    config_obj = load_strategy_sets()
    buy_set_long = next((s for s in config_obj.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
    buy_set_short = next((s for s in config_obj.buy_sets if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN"), None)
    cover_set_long = next((s for s in config_obj.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)
    
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
    
    long_signals = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
    short_signals = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
    
    logger.info("Precomputing entry signals (this may take a minute)...")
    for sym, df in stock_dfs.items():
        for i in range(10, len(df)):
            sliced = df.iloc[:i+1]
            # Long Entry
            c_le = evaluator._evaluate_conditions(buy_set_long, StrategyEvaluationContext("buy", sliced, sliced, i+1))
            if c_le and all(r.get("fired") for r in c_le):
                long_signals[sym][i] = True
            # Short Entry
            c_se = evaluator._evaluate_conditions(buy_set_short, StrategyEvaluationContext("buy", sliced, sliced, i+1))
            if c_se and all(r.get("fired") for r in c_se):
                short_signals[sym][i] = True
                
    timeline_set = set()
    for sym, df in stock_dfs.items():
        timeline_set.update(df.index)
    timeline = sorted(list(timeline_set))
    stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
    
    capital = 100000.0
    
    # ==========================================================================
    # 1. RUN BASELINE SYSTEM
    # ==========================================================================
    bull_days_base = regime.get_bull_days(0.010, 0.40)
    bear_days_base = regime.get_bear_days(-0.006, 0.40)
    
    logger.info("Running Baseline Dual-Engine simulation...")
    baseline_trades = run_simulation(
        stock_dfs, regime, timeline, stock_ts_map, long_signals, short_signals,
        bull_days_base, bear_days_base, evaluator, buy_set_long, buy_set_short, cover_set_long,
        max_open_positions=3, long_rsi_mode="14_LAG1_72", long_kinetic=False,
        long_tp=0.005, long_frac=0.75, long_sl=0.010,
        short_sl=None, short_tsl=None, short_tp=0.005, short_frac=0.75,
        short_rsi_exit=15.0, short_rsi_mode="16_LAG1", short_dyn_sl=True,
        short_block_entry=False
    )
    long_base = [t for t in baseline_trades if t.direction == "LONG"]
    short_base = [t for t in baseline_trades if t.direction == "SHORT"]
    print_table("Baseline dual-engine system (3 slots)", long_base, short_base, capital)
    
    # ==========================================================================
    # 2. RUN OPTIMIZED PATH A & B
    # ==========================================================================
    logger.info("Running Optimized Path A & B Dual-Engine simulation...")
    path_ab_trades = run_simulation(
        stock_dfs, regime, timeline, stock_ts_map, long_signals, short_signals,
        bull_days_base, bear_days_base, evaluator, buy_set_long, buy_set_short, cover_set_long,
        max_open_positions=3, long_rsi_mode="13_LAG1_85", long_kinetic=True,
        long_tp=0.005, long_frac=0.75, long_sl=0.010,
        short_sl=0.012, short_tsl=0.008, short_tp=0.005, short_frac=0.75,
        short_rsi_exit=15.0, short_rsi_mode="16_LAG1", short_dyn_sl=True,
        short_block_entry=True
    )
    long_ab = [t for t in path_ab_trades if t.direction == "LONG"]
    short_ab = [t for t in path_ab_trades if t.direction == "SHORT"]
    print_table("Optimized Path A & B system (3 slots)", long_ab, short_ab, capital)
    
    # ==========================================================================
    # 3. RUN OPTIMIZED PATH C (2 slots and 1 slot)
    # ==========================================================================
    logger.info("Running Optimized Path C (2 slots) simulation...")
    path_c_2slots_trades = run_simulation(
        stock_dfs, regime, timeline, stock_ts_map, long_signals, short_signals,
        bull_days_base, bear_days_base, evaluator, buy_set_long, buy_set_short, cover_set_long,
        max_open_positions=2, long_rsi_mode="14_LAG0_72", long_kinetic=False,
        long_tp=0.015, long_frac=1.00, long_sl=0.008,
        short_sl=0.005, short_tsl=None, short_tp=0.025, short_frac=1.00,
        short_rsi_exit=15.0, short_rsi_mode="16_LAG1", short_dyn_sl=False,
        short_block_entry=True
    )
    long_c2 = [t for t in path_c_2slots_trades if t.direction == "LONG"]
    short_c2 = [t for t in path_c_2slots_trades if t.direction == "SHORT"]
    print_table("Optimized Path C system (2 slots)", long_c2, short_c2, capital)
    
    logger.info("Running Optimized Path C (1 slot) simulation...")
    path_c_1slot_trades = run_simulation(
        stock_dfs, regime, timeline, stock_ts_map, long_signals, short_signals,
        bull_days_base, bear_days_base, evaluator, buy_set_long, buy_set_short, cover_set_long,
        max_open_positions=1, long_rsi_mode="14_LAG0_72", long_kinetic=False,
        long_tp=0.015, long_frac=1.00, long_sl=0.008,
        short_sl=0.005, short_tsl=None, short_tp=0.025, short_frac=1.00,
        short_rsi_exit=17.0, short_rsi_mode="16_LAG1", short_dyn_sl=False,
        short_block_entry=True
    )
    long_c1 = [t for t in path_c_1slot_trades if t.direction == "LONG"]
    short_c1 = [t for t in path_c_1slot_trades if t.direction == "SHORT"]
    print_table("Optimized Path C system (1 slot)", long_c1, short_c1, capital)
    
    # Export Path C 1-Slot Tearsheet for independent forensic audit
    tearsheet_path = r"c:\Extra Programs\Files\AlcoSoft_Financial_Services\research\analysis\path_c_tearsheet.txt"
    with open(tearsheet_path, "w", encoding="utf-8") as f:
        f.write("="*80 + "\n")
        f.write("ALCOSOFT PATH-C (1-SLOT) TEARSHEET\n")
        f.write("="*80 + "\n\n")
        
        # Write Long Trades
        f.write("EXHAUSTIVE TRADE LEDGER (LONG ENGINE)\n")
        f.write("="*80 + "\n")
        f.write("SYMBOL|DIRECTION|ENTRY_TIME|EXIT_TIME|QTY|ENTRY_PRICE|EXIT_PRICE|REASON|NET_PNL\n")
        for t in long_c1:
            f.write(f"{t.symbol}|{t.direction}|{t.entry_time}|{t.exit_time}|{t.quantity}|{t.entry_price:.2f}|{t.exit_price:.2f}|{t.exit_reason}|{t.pnl_net:.2f}\n")
        
        f.write("\n")
        f.write("EXHAUSTIVE TRADE LEDGER (SHORT ENGINE)\n")
        f.write("="*80 + "\n")
        f.write("SYMBOL|DIRECTION|ENTRY_TIME|EXIT_TIME|QTY|ENTRY_PRICE|EXIT_PRICE|REASON|NET_PNL\n")
        for t in short_c1:
            f.write(f"{t.symbol}|{t.direction}|{t.entry_time}|{t.exit_time}|{t.quantity}|{t.entry_price:.2f}|{t.exit_price:.2f}|{t.exit_reason}|{t.pnl_net:.2f}\n")
    logger.info(f"Path C tearsheet written to {tearsheet_path}")

    
    # Create output directory for analysis if not exists
    output_dir = r"C:\Extra Programs\Files\AlcoSoft_Financial_Services\research\analysis"
    os.makedirs(output_dir, exist_ok=True)
    
    # ==========================================================================
    # GRAPH 1: Cumulative PnL/returns comparison over time
    # ==========================================================================
    logger.info("Generating Graph 1: Cumulative PnL comparison...")
    plt.figure(figsize=(12, 6))
    
    # Sort all timelines by exit time
    for name, trades_list in [("Baseline (3 slots)", baseline_trades),
                             ("Path A/B (3 slots)", path_ab_trades),
                             ("Path C (2 slots)", path_c_2slots_trades),
                             ("Path C (1 slot)", path_c_1slot_trades)]:
        if not trades_list:
            continue
        sorted_trades = sorted(trades_list, key=lambda t: t.exit_time)
        dates_list = [t.exit_time.date() for t in sorted_trades]
        pnl_cum = np.cumsum([t.pnl_net for t in sorted_trades]) / capital * 100
        
        # Create daily series to smooth out plotting
        df_cum = pd.DataFrame({"date": dates_list, "pnl": pnl_cum})
        df_cum_daily = df_cum.groupby("date").last().reindex(pd.date_range(start=df_cum["date"].min(), end=df_cum["date"].max(), freq="D")).ffill().fillna(0.0)
        
        plt.plot(df_cum_daily.index, df_cum_daily["pnl"], label=f"{name} (Final: {df_cum_daily['pnl'].iloc[-1]:.2f}%)", linewidth=2)
        
    plt.title("Cumulative Net Portfolio Returns Comparison over Time (STT Tax Deducted)", fontsize=14, fontweight="bold")
    plt.xlabel("Timeline", fontsize=12)
    plt.ylabel("Cumulative Net Return (%)", fontsize=12)
    plt.grid(True, linestyle="--", alpha=0.6)
    plt.legend(fontsize=10)
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "cumulative_returns_comparison.png"), dpi=150)
    plt.close()
    
    # ==========================================================================
    # GRAPH 2: Segmented win/loss ratios by exit reason for Long and Short engines (Path C 2-slot as rep)
    # ==========================================================================
    logger.info("Generating Graph 2: Segmented win/loss ratios by exit reason...")
    plt.figure(figsize=(14, 6))
    
    # Use baseline trades as representative for exit reasons breakdown
    df_trades = pd.DataFrame([
        {"direction": t.direction, "exit_reason": t.exit_reason, "win": 1 if t.pnl_net > 0 else 0, "loss": 1 if t.pnl_net <= 0 else 0}
        for t in baseline_trades
    ])
    
    if not df_trades.empty:
        df_grouped = df_trades.groupby(["direction", "exit_reason"]).agg({"win": "sum", "loss": "sum"}).reset_index()
        df_grouped["Total"] = df_grouped["win"] + df_grouped["loss"]
        df_grouped["Win Rate (%)"] = df_grouped["win"] / df_grouped["Total"] * 100
        
        sns.barplot(data=df_grouped, x="exit_reason", y="Total", hue="direction", palette=["#2ca02c", "#d62728"])
        plt.title("Segmented Win/Loss Trades Count by Exit Reason (Baseline System)", fontsize=14, fontweight="bold")
        plt.xlabel("Exit Reason", fontsize=12)
        plt.ylabel("Number of Trades", fontsize=12)
        plt.grid(axis="y", linestyle="--", alpha=0.6)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, "exit_reason_distribution.png"), dpi=150)
        plt.close()
    
    # ==========================================================================
    # GRAPH 3: Performance by day types (Bull vs Bear days)
    # ==========================================================================
    logger.info("Generating Graph 3: Performance by day types...")
    # Classify trades in Path C (2 slots) by day type
    day_perf = []
    
    for name, trades_list in [("Baseline", baseline_trades), ("Path C (2 slots)", path_c_2slots_trades)]:
        for t in trades_list:
            dt = t.entry_time.date()
            is_bull = dt in bull_days_base
            is_bear = dt in bear_days_base
            day_type = "Bull Day" if is_bull else ("Bear Day" if is_bear else "Normal Day")
            day_perf.append({
                "System": name,
                "Day Type": day_type,
                "PnL Net": t.pnl_net,
                "Return (%)": (t.pnl_net / capital) * 100
            })
            
    df_day = pd.DataFrame(day_perf)
    if not df_day.empty:
        plt.figure(figsize=(10, 6))
        # Group by System and Day Type
        df_day_grouped = df_day.groupby(["System", "Day Type"])["Return (%)"].sum().reset_index()
        sns.barplot(data=df_day_grouped, x="Day Type", y="Return (%)", hue="System", palette="Set2")
        plt.title("Net Return Contribution by Day Types (Bull vs Bear Days)", fontsize=14, fontweight="bold")
        plt.xlabel("Day Type Regime", fontsize=12)
        plt.ylabel("Net Return Contribution (%)", fontsize=12)
        plt.grid(axis="y", linestyle="--", alpha=0.6)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, "day_type_performance.png"), dpi=150)
        plt.close()
        
    logger.info("All graphs generated successfully!")

if __name__ == "__main__":
    main()
