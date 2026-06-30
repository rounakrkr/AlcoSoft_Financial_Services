import sys
import os
import time
import logging
import warnings
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Set
from datetime import datetime, date

import pandas as pd
import numpy as np

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.build_cache import load_cache
from core.strategy import (CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext)
from core.strategy_sets import load_strategy_sets
from core.order_executor import round_to_tick

# =========================================================================================
# CONFIGURATION & CONSTANTS
# =========================================================================================
CAPITAL: float = 100000.0
MARGIN: float = 5.0
BUYING_POWER: float = CAPITAL * MARGIN
MP: int = 3
SL_PCT: float = 0.010

# Profit Booking Rules
PROFIT_TARGET_PCT: float = 0.005 # 0.5%
PARTIAL_FRAC: float = 0.75

# Bull Engine (Long) Sweet Spot
BULL_MKT_GAP: float = 0.010      # >= 1.0% Gap Up
BULL_MKT_BREADTH: float = 0.40   # 40% stocks
LONG_RSI_EXIT_PERIOD: int = 13
LONG_RSI_EXIT_THR: float = 85.0  # Lag1 >= 85

# Bear Engine (Short) Sweet Spot
BEAR_IND_GAP: float = -0.008     # <= -0.8% Gap Down
BEAR_MKT_BREADTH: float = 0.40   # 40% stocks
SHORT_RSI_EXIT_PERIOD: int = 16
# SHORT_RSI_EXIT_THR will be swept dynamically

USER_DEFINED_STT_PCT: float = 0.00035

# =========================================================================================
# DATACLASSES
# =========================================================================================
@dataclass
class Trade:
    symbol: str
    side: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    qty: int
    reason: str
    
    @property
    def gross_pnl(self) -> float:
        if self.side == "LONG": return (self.exit_price - self.entry_price) * self.qty
        else: return (self.entry_price - self.exit_price) * self.qty
        
    @property
    def stt_tax(self) -> float:
        # STT on sell side for both
        sell_price = self.exit_price if self.side == "LONG" else self.entry_price
        return sell_price * self.qty * USER_DEFINED_STT_PCT
        
    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.stt_tax

@dataclass
class Position:
    symbol: str
    side: str
    entry_ts: pd.Timestamp
    entry_price: float
    qty: int
    sl_price: float
    partial_done: bool = False

# =========================================================================================
# DUAL SIMULATION ENGINE
# =========================================================================================
class MasterPortfolioEngine:
    def __init__(self, timeline, stock_ts_map, stock_dfs, 
                 precomputed_long_entries, precomputed_long_dyn_exits,
                 precomputed_short_entries, precomputed_short_dyn_exits,
                 all_daily_gaps, bull_days, bear_days):
        
        self.timeline = timeline
        self.stock_ts_map = stock_ts_map
        self.stock_dfs = stock_dfs
        self.long_entries = precomputed_long_entries
        self.long_dyn_exits = precomputed_long_dyn_exits
        self.short_entries = precomputed_short_entries
        self.short_dyn_exits = precomputed_short_dyn_exits
        
        self.all_daily_gaps = all_daily_gaps
        self.bull_days = bull_days
        self.bear_days = bear_days
        
        self.positions: Dict[str, Position] = {}
        self.completed_trades: List[Trade] = []
        # Base per_slot capital assumes MP=3. Even if hybrid_mp is higher, 
        # we still divide capital by 3 so each trade gets 1/3rd of buying power, 
        # which means going over 3 positions will use up all buying power unless we use margin/reinvest.
        # Actually, if we allow up to 7 positions, per_slot must be calculated per day or dynamically.
        # But BUYING_POWER is CAPITAL * 5. So we can hold up to 15 trades (each 1/3rd of CAPITAL).
        self.per_slot_capital = BUYING_POWER / 3 
        
    def run(self, hybrid_mp: int, short_rsi_thr: float, long_rsi_mode: str):
        self.positions.clear()
        self.completed_trades.clear()
        self.current_short_rsi_thr = short_rsi_thr
        self.long_rsi_mode = long_rsi_mode
        
        for ts in self.timeline:
            current_date = ts.date()
            self._manage_open_positions(ts, current_date)
            
            if ts.hour >= 15: continue
            
            is_bull_day = current_date in self.bull_days
            is_bear_day = current_date in self.bear_days
            if not is_bull_day and not is_bear_day: continue
            
            current_mp = hybrid_mp if (is_bull_day and is_bear_day) else 3
            if len(self.positions) >= current_mp: continue
            
            self._scan_for_new_entries(ts, current_date, is_bull_day, is_bear_day, current_mp, short_rsi_thr)
            
        self._force_eod_square_off_all()
        return self.completed_trades
        
    def _manage_open_positions(self, ts, current_date):
        symbols_to_close = []
        
        for sym, pos in self.positions.items():
            if ts not in self.stock_ts_map.get(sym, {}): continue
            idx = self.stock_ts_map[sym][ts]
            df = self.stock_dfs[sym]
            
            cc = df.iloc[idx]
            close_price, high_price, low_price, open_price = float(cc["close"]), float(cc["high"]), float(cc["low"]), float(cc["open"])
            
            # Partial Profit
            if not pos.partial_done:
                profit_ratio = (close_price - pos.entry_price) / pos.entry_price if pos.side == "LONG" else (pos.entry_price - close_price) / pos.entry_price
                if profit_ratio >= PROFIT_TARGET_PCT:
                    cover_qty = max(1, int(pos.qty * PARTIAL_FRAC))
                    if cover_qty >= pos.qty: cover_qty = max(0, pos.qty - 1)
                    if cover_qty > 0:
                        self.completed_trades.append(Trade(sym, pos.side, pos.entry_ts.date(), current_date, pos.entry_price, close_price, cover_qty, "PARTIAL_PROFIT"))
                        pos.qty -= cover_qty
                        pos.partial_done = True
                        if pos.qty <= 0: symbols_to_close.append(sym); continue
            
            # Stop Loss
            if pos.side == "LONG" and low_price <= pos.sl_price:
                self.completed_trades.append(Trade(sym, pos.side, pos.entry_ts.date(), current_date, pos.entry_price, min(pos.sl_price, open_price), pos.qty, "SL_EXIT"))
                symbols_to_close.append(sym); continue
            elif pos.side == "SHORT" and high_price >= pos.sl_price:
                self.completed_trades.append(Trade(sym, pos.side, pos.entry_ts.date(), current_date, pos.entry_price, max(pos.sl_price, open_price), pos.qty, "SL_EXIT"))
                symbols_to_close.append(sym); continue
                
            # Time Exit
            if ts.hour == 15 and ts.minute >= 15:
                self.completed_trades.append(Trade(sym, pos.side, pos.entry_ts.date(), current_date, pos.entry_price, close_price, pos.qty, "TIME_EXIT"))
                symbols_to_close.append(sym); continue
                
            # RSI Exit
            # For LONG
            if self.long_rsi_mode == "13_LAG1_85":
                long_rsi_lag1 = df["rsi_13"].iloc[idx-1] if idx > 0 else 50.0
                if pos.side == "LONG" and long_rsi_lag1 >= 85.0:
                    self.completed_trades.append(Trade(sym, pos.side, pos.entry_ts.date(), current_date, pos.entry_price, close_price, pos.qty, "RSI_EXIT"))
                    symbols_to_close.append(sym); continue
            else: # "14_LAG0_72"
                long_rsi_lag0 = float(cc["rsi"]) if "rsi" in cc.index and pd.notna(cc["rsi"]) else 50.0
                if pos.side == "LONG" and long_rsi_lag0 >= 72.0:
                    self.completed_trades.append(Trade(sym, pos.side, pos.entry_ts.date(), current_date, pos.entry_price, close_price, pos.qty, "RSI_EXIT"))
                    symbols_to_close.append(sym); continue
                
            # For SHORT: RSI(16) Lag0 <= passed thr
            short_rsi_lag0 = df["rsi_16"].iloc[idx]
            if pos.side == "SHORT" and short_rsi_lag0 <= self.current_short_rsi_thr:
                self.completed_trades.append(Trade(sym, pos.side, pos.entry_ts.date(), current_date, pos.entry_price, close_price, pos.qty, "RSI_EXIT"))
                symbols_to_close.append(sym); continue
                
            # Dynamic Exit
            if pos.side == "LONG" and self.long_dyn_exits[sym][idx]:
                if idx + 1 < len(df):
                    self.completed_trades.append(Trade(sym, pos.side, pos.entry_ts.date(), current_date, pos.entry_price, float(df.iloc[idx+1]["open"]), pos.qty, "DYN_EXIT"))
                    symbols_to_close.append(sym); continue
            elif pos.side == "SHORT" and self.short_dyn_exits[sym][idx]:
                if idx + 1 < len(df):
                    self.completed_trades.append(Trade(sym, pos.side, pos.entry_ts.date(), current_date, pos.entry_price, float(df.iloc[idx+1]["open"]), pos.qty, "DYN_EXIT"))
                    symbols_to_close.append(sym); continue
                    
        for sym in symbols_to_close:
            if sym in self.positions: del self.positions[sym]
                
    def _scan_for_new_entries(self, ts, current_date, is_bull_day, is_bear_day, current_mp, short_rsi_thr):
        for sym, df in self.stock_dfs.items():
            if len(self.positions) >= current_mp: break
            if sym in self.positions: continue
            
            gap_pct = self.all_daily_gaps.get((current_date, sym), 0.0)
            if ts not in self.stock_ts_map[sym]: continue
            idx = self.stock_ts_map[sym][ts]
            
            # Segregation Logic
            if gap_pct <= BEAR_IND_GAP:
                # Exclusive to Bear Engine
                if is_bear_day and self.short_entries[sym][idx] and not self.short_dyn_exits[sym][idx]:
                    if idx + 1 < len(df):
                        ep = float(df.iloc[idx+1]["open"])
                        qty = int(self.per_slot_capital // ep)
                        if qty > 0:
                            self.positions[sym] = Position(sym, "SHORT", ts, ep, qty, round_to_tick(ep * (1.0 + SL_PCT)))
            else:
                # Exclusive to Bull Engine
                if is_bull_day and self.long_entries[sym][idx] and not self.long_dyn_exits[sym][idx]:
                    if idx + 1 < len(df):
                        ep = float(df.iloc[idx+1]["open"])
                        qty = int(self.per_slot_capital // ep)
                        if qty > 0:
                            self.positions[sym] = Position(sym, "LONG", ts, ep, qty, round_to_tick(ep * (1.0 - SL_PCT)))
                            
    def _force_eod_square_off_all(self):
        for sym, pos in self.positions.items():
            df = self.stock_dfs[sym]
            self.completed_trades.append(Trade(sym, pos.side, pos.entry_ts.date(), df.index[-1].date(), pos.entry_price, float(df["close"].iloc[-1]), pos.qty, "SIMULATION_END"))
        self.positions.clear()

def prev_high_break(ctx):
    df = ctx.indicator_df
    if len(df)<3: return {"fired": False}
    c1=df["close"].iloc[-2]; h2=df["high"].iloc[-3]
    return {"fired": bool(c1 > h2)}

def main():
    print("LAUNCHING DUAL-ENGINE MASTER PORTFOLIO SIMULATOR")
    
    stock_dfs = load_cache()
    timeline = sorted(list(set(ts for df in stock_dfs.values() for ts in df.index)))
    stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
    
    mega_df = pd.concat([df.assign(symbol=sym, date=df.index.date) for sym, df in stock_dfs.items()])
    first_candles = mega_df.groupby(["date", "symbol"]).first().reset_index()
    
    all_daily_gaps = {}
    dates = sorted(first_candles["date"].unique())
    for i in range(1, len(dates)):
        prev_d, curr_d = dates[i-1], dates[i]
        prev_day = mega_df[mega_df["date"] == prev_d]
        curr_day = first_candles[first_candles["date"] == curr_d]
        if prev_day.empty or curr_day.empty: continue
        
        last_closes = prev_day.groupby("symbol").last()["close"]
        first_opens = curr_day.set_index("symbol")["open"]
        merged = pd.concat([last_closes, first_opens], axis=1, join="inner")
        if merged.empty: continue
        
        gaps = (merged["open"] - merged["close"]) / merged["close"]
        for sym, gap_val in gaps.items(): all_daily_gaps[(curr_d, sym)] = float(gap_val)

    print("Calculating Custom RSIs (Long: RSI13, Short: RSI16)...")
    import ta
    for sym, df in stock_dfs.items():
        df["rsi_13"] = ta.momentum.rsi(df["close"], window=LONG_RSI_EXIT_PERIOD).fillna(50.0)
        df["rsi_16"] = ta.momentum.rsi(df["close"], window=SHORT_RSI_EXIT_PERIOD).fillna(50.0)

    # Calculate Bull/Bear Days
    bull_days, bear_days = set(), set()
    for i in range(1, len(dates)):
        curr_d = dates[i]
        daily_gaps_vals = [g for (d, s), g in all_daily_gaps.items() if d == curr_d]
        if not daily_gaps_vals: continue
        
        if sum(1 for g in daily_gaps_vals if g >= BULL_MKT_GAP) / len(daily_gaps_vals) >= BULL_MKT_BREADTH:
            bull_days.add(curr_d)
            
        if sum(1 for g in daily_gaps_vals if g <= BEAR_IND_GAP) / len(daily_gaps_vals) >= BEAR_MKT_BREADTH:
            bear_days.add(curr_d)
            
    print(f"Market Profiling: {len(bull_days)} Bull Days, {len(bear_days)} Bear Days")

    config = load_strategy_sets()
    long_set = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
    long_exit_set = next((s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)
    short_set = next((s for s in config.buy_sets if s.name == "SHORT_STREAK_MOMENTUM_BREAKDOWN"), None)
    
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

    pre_le = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}
    pre_lx = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}
    pre_se = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}
    pre_sx = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}

    print("Precomputing signals (this may take a minute)...")
    import time
    st = time.time()
    for sym, df in stock_dfs.items():
        for idx in range(1, len(df)):
            sliced = df.iloc[:idx+1]
            
            # Long Entry
            c_le = evaluator._evaluate_conditions(long_set, StrategyEvaluationContext("buy", sliced, sliced, len(sliced)))
            if c_le and all(r.get("fired") for r in c_le): pre_le[sym][idx] = True
            
            # Long Dyn Exit (EMA)
            c_lx = evaluator._evaluate_conditions(long_exit_set, StrategyEvaluationContext("sell", sliced, sliced, 0))
            if c_lx and all(r.get("fired") for r in c_lx): pre_lx[sym][idx] = True
            
            # Short Entry (starts after 10th candle of entire timeline normally, but we use sliced logic)
            if idx >= 10:
                c_se = evaluator._evaluate_conditions(short_set, StrategyEvaluationContext("buy", sliced, sliced, len(sliced)))
                if c_se and all(r.get("fired") for r in c_se): pre_se[sym][idx] = True
                
            # Short Dyn Exit (PREV_CANDLE_HIGH_LAG1)
            c_sx = prev_high_break(StrategyEvaluationContext("sell", sliced, sliced, 0))
            if c_sx["fired"]: pre_sx[sym][idx] = True
    print(f"Precomputation finished in {time.time()-st:.1f}s")

    print("Running Long RSI Exit Sweeps (13 Lag1 >= 85 vs 14 Lag0 >= 72)...")
    engine = MasterPortfolioEngine(timeline, stock_ts_map, stock_dfs, pre_le, pre_lx, pre_se, pre_sx, all_daily_gaps, bull_days, bear_days)
    
    sweep_results = []
    
    for rsi_mode in ["14_LAG0_72", "13_LAG1_85"]:
        print(f"Running Long RSI Mode = {rsi_mode}...")
        trades = engine.run(3, 20.0, rsi_mode) # MP=3, Short_RSI=20.0
        
        df_t = pd.DataFrame([vars(t) for t in trades])
        if df_t.empty: continue
            
        df_t["gross_pnl"] = [t.gross_pnl for t in trades]
        df_t["stt_tax"] = [t.stt_tax for t in trades]
        df_t["net_pnl"] = df_t["gross_pnl"] - df_t["stt_tax"]
        
        overall_net = df_t["net_pnl"].sum()
        overall_win_rate = len(df_t[df_t["gross_pnl"] > 0]) / len(df_t) * 100
        
        longs = df_t[df_t["side"] == "LONG"]
        shorts = df_t[df_t["side"] == "SHORT"]
        
        sweep_results.append({
            "Long_RSI_Mode": rsi_mode,
            "Total_Trades": len(df_t),
            "Win_Rate": overall_win_rate,
            "Gross_Return": df_t["gross_pnl"].sum() / CAPITAL * 100,
            "Net_Return": overall_net / CAPITAL * 100,
            "Long_Net": longs["net_pnl"].sum() / CAPITAL * 100 if len(longs) > 0 else 0.0,
            "Short_Net": shorts["net_pnl"].sum() / CAPITAL * 100 if len(shorts) > 0 else 0.0
        })
        
    print("\n" + "="*80)
    print("LONG RSI EXIT SWEEP RESULTS")
    print("="*80)
    
    res_df = pd.DataFrame(sweep_results)
    print(res_df.to_string(index=False))
    
    # We just want to see the console output, no need to write detailed report for this sweep
    print("\nSweep Complete!")

if __name__ == "__main__":
    main()
