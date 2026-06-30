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
    direction: str
    exit_reason: str
    
    @property
    def pnl_gross(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def stt_tax(self) -> float:
        return self.exit_price * self.quantity * 0.00035

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

    def get_bull_days(self, threshold: float = 0.005, breadth: float = 0.40) -> Set[datetime.date]:
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

    def get_gap(self, date: datetime.date, symbol: str) -> float:
        return self.all_daily_gaps.get((date, symbol), 0.0)

def main():
    stock_dfs = load_cache()
    for sym, df in stock_dfs.items():
        df["rsi"] = ta.momentum.rsi(df["close"], window=14).fillna(50.0)
        
    regime = MarketRegimeAnalyzer(stock_dfs)
    bull_days = regime.get_bull_days(0.005, 0.40)
    
    config_obj = load_strategy_sets()
    buy_set_long = next((s for s in config_obj.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
    cover_set_long = next((s for s in config_obj.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
    
    long_signals = {sym: [False]*len(df) for sym, df in stock_dfs.items()}
    for sym, df in stock_dfs.items():
        for i in range(10, len(df)):
            sliced = df.iloc[:i+1]
            c_le = evaluator._evaluate_conditions(buy_set_long, StrategyEvaluationContext("buy", sliced, sliced, i+1))
            if c_le and all(r.get("fired") for r in c_le):
                long_signals[sym][i] = True
                
    timeline_set = set()
    for sym, df in stock_dfs.items():
        timeline_set.update(df.index)
    timeline = sorted(list(timeline_set))
    stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
    
    capital = 100000.0
    buying_power = capital * 5.0
    cap_per_trade = buying_power / 3
    
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
                
            # RSI Exit (RSI(14) Lag-0 >= 72)
            rsi0 = float(cc["rsi"])
            if rsi0 >= 72.0:
                long_trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=ep, exit_price=cp, quantity=pos.quantity,
                    direction="LONG", exit_reason="RSI_OVERBOUGHT"
                ))
                syms_to_close.append(sym)
                continue
                
            # Kinetic Profit Booking (+0.5% profit)
            if not pos.partial_booked:
                target_price = ep * 1.005
                if cp >= target_price:
                    # Check Kinetic
                    rsi_prev1 = df["rsi"].iloc[idx-1] if idx >= 1 else 50.0
                    rsi_prev2 = df["rsi"].iloc[idx-2] if idx >= 2 else 50.0
                    is_strong = rsi_prev1 > rsi_prev2
                    pos.partial_booked = True
                    if is_strong:
                        pass
                    else:
                        exit_price = cp
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
                        
    gross = sum(t.pnl_gross for t in long_trades)
    stt = sum(t.stt_tax for t in long_trades)
    net = gross - stt
    print(f"LONG ONLY MULTILAYER:")
    print(f"  Trades: {len(long_trades)}")
    print(f"  Gross Return: {gross/capital*100:.2f}%")
    print(f"  STT Tax: {stt/capital*100:.2f}%")
    print(f"  Net Return: {net/capital*100:.2f}%")

if __name__ == "__main__":
    main()
