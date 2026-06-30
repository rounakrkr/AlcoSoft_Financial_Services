import sys
import os
import math
import logging
import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any

import pandas as pd
import numpy as np
import ta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.build_cache import load_cache
from core.strategy_sets import load_strategy_sets, StrategySetDefinition
from core.strategy import StrategySetEvaluator, StrategyEvaluationContext, CONDITION_REGISTRY
from core.order_executor import round_to_tick

logging.basicConfig(level=logging.WARNING)

@dataclass
class TradeRecord:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: int
    direction: str  # 'LONG' or 'SHORT'
    exit_reason: str
    
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

    def get_bull_days(self, threshold: float, breadth: float) -> Set[datetime.date]:
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

    def get_bear_days(self, threshold: float, breadth: float) -> Set[datetime.date]:
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

def run_backtest(
    stock_dfs, regime, timeline, stock_ts_map, long_signals, short_signals,
    bull_days, bear_days, evaluator, buy_set_long, buy_set_short, cover_set_long,
    long_rsi_mode="13_LAG1_85",
    long_kinetic=True,
    short_sl=0.012,
    short_tsl=0.008,
    short_rsi_exit=15.0,
    short_rsi_mode="16_LAG1"
):
    capital = 100000.0
    buying_power = capital * 5.0
    cap_per_trade = buying_power / 3
    
    l_rsi_period = 14 if "14" in long_rsi_mode else 13
    l_rsi_col = f"rsi_{l_rsi_period}"
    l_rsi_thr = 72.0 if "72" in long_rsi_mode else 85.0
    l_rsi_lag = 0 if "LAG0" in long_rsi_mode else 1

    # Run Long Engine
    long_trades = []
    open_positions = {}
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
                long_trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=ep, exit_price=cp, quantity=pos.quantity,
                    direction="LONG", exit_reason="EOD_TIME"
                ))
                syms_to_close.append(sym)
                continue
                
            # Stop Loss
            if lp <= pos.stop_loss_price:
                exit_price = min(pos.stop_loss_price, op)
                long_trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=ep, exit_price=exit_price, quantity=pos.quantity,
                    direction="LONG", exit_reason="STOP_LOSS"
                ))
                syms_to_close.append(sym)
                continue
                
            # RSI Exit
            if ts > pos.entry_time and idx >= 1:
                chk_idx = idx - 1
                curr_rsi = df[l_rsi_col].iloc[chk_idx]
                if curr_rsi >= l_rsi_thr:
                    exit_price = op
                    long_trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=ep, exit_price=exit_price, quantity=pos.quantity,
                        direction="LONG", exit_reason="RSI_OVERBOUGHT"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
            # Kinetic Profit Booking (+0.5% profit)
            if not pos.partial_booked:
                target_price = ep * 1.005
                if hp >= target_price:
                    # Check Kinetic
                    rsi_prev1 = df[l_rsi_col].iloc[idx-1] if idx >= 1 else 50.0
                    rsi_prev2 = df[l_rsi_col].iloc[idx-2] if idx >= 2 else 50.0
                    is_strong = rsi_prev1 > rsi_prev2
                    pos.partial_booked = True
                    if is_strong:
                        pass
                    else:
                        exit_price = max(target_price, op)
                        cover_qty = max(1, int(pos.quantity * 0.75))
                        if cover_qty >= pos.quantity:
                            cover_qty = max(0, pos.quantity - 1)
                        if cover_qty > 0:
                            long_trades.append(TradeRecord(
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
                    long_trades.append(TradeRecord(
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
        if is_bull_day and ts.hour < 15 and len(open_positions) < 3:
            for sym in stock_dfs:
                if len(open_positions) >= 3:
                    break
                if sym in open_positions:
                    continue
                gap_pct = regime.get_gap(date_only, sym)
                if gap_pct <= -0.008:
                    continue
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
                        sl_price = entry_price * 0.990
                        open_positions[sym] = OpenPosition(
                            symbol=sym, entry_time=entry_time, entry_price=entry_price,
                            quantity=qty, direction="LONG", stop_loss_price=sl_price
                        )
                        
    # Run Short Engine
    short_trades = []
    open_positions.clear()
    
    def prev_high_break(ctx):
        df = ctx.indicator_df
        if len(df) < 3: return {"fired": False}
        c1 = df["close"].iloc[-2]
        h2 = df["high"].iloc[-3]
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
                short_trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=ep, exit_price=cp, quantity=pos.quantity,
                    direction="SHORT", exit_reason="EOD_TIME"
                ))
                syms_to_close.append(sym)
                continue
                
            # Dynamic SL Lag-1
            if ts > pos.entry_time and idx >= 2:
                c1 = float(df["close"].iloc[idx-1])
                h2 = float(df["high"].iloc[idx-2])
                if c1 > h2:
                    short_trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=ep, exit_price=op, quantity=pos.quantity,
                        direction="SHORT", exit_reason="DYN_SL_LAG1"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
            # Stop Loss (hard or trailing)
            if short_sl is not None:
                if not pos.tsl_active and short_tsl is not None:
                    if lp <= ep * 0.995:
                        pos.tsl_active = True
                        pos.stop_loss_price = min(pos.stop_loss_price, round_to_tick(lp * (1 + short_tsl)))
                elif pos.tsl_active and short_tsl is not None:
                    pos.stop_loss_price = min(pos.stop_loss_price, round_to_tick(lp * (1 + short_tsl)))
                
                if hp >= pos.stop_loss_price:
                    exit_price = max(pos.stop_loss_price, op)
                    short_trades.append(TradeRecord(
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
                    short_trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=ep, exit_price=exit_price, quantity=pos.quantity,
                        direction="SHORT", exit_reason="RSI_OVERSOLD"
                    ))
                    syms_to_close.append(sym)
                    continue
                    
            # Partial Profit booking (+0.5% profit)
            if not pos.partial_booked:
                target_price = ep * 0.995
                if lp <= target_price:
                    exit_price = min(target_price, op)
                    cover_qty = max(1, int(pos.quantity * 0.75))
                    if cover_qty >= pos.quantity:
                        cover_qty = max(0, pos.quantity - 1)
                    if cover_qty > 0:
                        short_trades.append(TradeRecord(
                            symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                            entry_price=ep, exit_price=exit_price, quantity=cover_qty,
                            direction="SHORT", exit_reason="PARTIAL_PROFIT"
                        ))
                        pos.quantity -= cover_qty
                        pos.partial_booked = True
                        if short_tsl is not None:
                            pos.tsl_active = True
                            pos.stop_loss_price = min(pos.stop_loss_price, round_to_tick(lp * (1 + short_tsl)))
                    if pos.quantity <= 0:
                        syms_to_close.append(sym)
                        continue
                        
        for sym in syms_to_close:
            if sym in open_positions:
                del open_positions[sym]
                
        # Scan for entries
        if is_bear_day and ts.hour < 15 and len(open_positions) < 3:
            for sym in stock_dfs:
                if len(open_positions) >= 3:
                    break
                if sym in open_positions:
                    continue
                gap_pct = regime.get_gap(date_only, sym)
                if gap_pct > -0.008:
                    continue
                idx = stock_ts_map[sym].get(ts, -1)
                if idx == -1 or not short_signals[sym][idx]:
                    continue
                
                df = stock_dfs[sym]
                sliced = df.iloc[:idx+1]
                cover_ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
                if prev_high_break(cover_ctx)["fired"]:
                    continue
                    
                if idx + 1 < len(df):
                    entry_price = float(df.iloc[idx+1]["open"])
                    entry_time = df.index[idx+1]
                    qty = int(cap_per_trade // entry_price)
                    if qty > 0:
                        sl_price = entry_price * (1 + (short_sl if short_sl is not None else 0.010))
                        open_positions[sym] = OpenPosition(
                            symbol=sym, entry_time=entry_time, entry_price=entry_price,
                            quantity=qty, direction="SHORT", stop_loss_price=sl_price
                        )
                        
    combined = long_trades + short_trades
    c_net = sum(t.pnl_net for t in combined)
    c_net_pct = c_net / capital * 100
    
    l_net = sum(t.pnl_net for t in long_trades)
    l_net_pct = l_net / capital * 100
    
    s_net = sum(t.pnl_net for t in short_trades)
    s_net_pct = s_net / capital * 100
    
    return c_net_pct, l_net_pct, s_net_pct, len(long_trades), len(short_trades)

def main():
    stock_dfs = load_cache()
    for sym, df in stock_dfs.items():
        df["rsi_13"] = ta.momentum.rsi(df["close"], window=13).fillna(50.0)
        df["rsi_14"] = ta.momentum.rsi(df["close"], window=14).fillna(50.0)
        df["rsi_16"] = ta.momentum.rsi(df["close"], window=16).fillna(50.0)
        
    regime = MarketRegimeAnalyzer(stock_dfs)
    
    config_obj = load_strategy_sets()
    buy_set_long = next((s for s in config_obj.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
    buy_set_short = next((s for s in config_obj.buy_sets if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN"), None)
    cover_set_long = next((s for s in config_obj.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
    
    long_signals = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
    short_signals = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
    for sym, df in stock_dfs.items():
        for i in range(10, len(df)):
            sliced = df.iloc[:i+1]
            c_le = evaluator._evaluate_conditions(buy_set_long, StrategyEvaluationContext("buy", sliced, sliced, i+1))
            if c_le and all(r.get("fired") for r in c_le):
                long_signals[sym][i] = True
            c_se = evaluator._evaluate_conditions(buy_set_short, StrategyEvaluationContext("buy", sliced, sliced, i+1))
            if c_se and all(r.get("fired") for r in c_se):
                short_signals[sym][i] = True
                
    timeline_set = set()
    for sym, df in stock_dfs.items():
        timeline_set.update(df.index)
    timeline = sorted(list(timeline_set))
    stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
    
    regimes = [
        # (long_gap, long_breadth, short_gap, short_breadth)
        (0.010, 0.40, -0.006, 0.40, "Baseline Gaps"),
        (0.005, 0.40, -0.005, 0.40, "Wider Gaps (0.5%/-0.5%)"),
        (0.003, 0.30, -0.003, 0.30, "Very Broad Gaps (0.3%/-0.3%)"),
        (0.008, 0.40, -0.008, 0.40, "Medium Gaps (0.8%/-0.8%)"),
    ]
    
    for l_gap, l_br, s_gap, s_br, label in regimes:
        bull_days = regime.get_bull_days(l_gap, l_br)
        bear_days = regime.get_bear_days(s_gap, s_br)
        
        # Test baseline vs optimized on this regime
        # Baseline
        bc_net, bl_net, bs_net, blt, bst = run_backtest(
            stock_dfs, regime, timeline, stock_ts_map, long_signals, short_signals,
            bull_days, bear_days, evaluator, buy_set_long, buy_set_short, cover_set_long,
            long_rsi_mode="14_LAG1_72", long_kinetic=False,
            short_sl=None, short_tsl=None, short_rsi_exit=15.0, short_rsi_mode="16_LAG1"
        )
        
        # Optimized
        oc_net, ol_net, os_net, olt, ost = run_backtest(
            stock_dfs, regime, timeline, stock_ts_map, long_signals, short_signals,
            bull_days, bear_days, evaluator, buy_set_long, buy_set_short, cover_set_long,
            long_rsi_mode="13_LAG1_85", long_kinetic=True,
            short_sl=0.012, short_tsl=0.008, short_rsi_exit=15.0, short_rsi_mode="16_LAG1"
        )
        
        print(f"Regime: {label} (LongGap={l_gap*100:.1f}%, ShortGap={s_gap*100:.1f}%)")
        print(f"  Baseline  - Combined: {bc_net:6.2f}% | Long: {bl_net:6.2f}% ({blt:3d}) | Short: {bs_net:6.2f}% ({bst:3d})")
        print(f"  Optimized - Combined: {oc_net:6.2f}% | Long: {ol_net:6.2f}% ({olt:3d}) | Short: {os_net:6.2f}% ({ost:3d})")
        print()

if __name__ == "__main__":
    main()
