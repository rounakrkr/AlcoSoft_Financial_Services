import sys
import os
import time
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, Any
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.build_cache import load_cache
from research.verify_dual_engine_enterprise_opt import (
    SystemConfig, OpenPosition, TradeRecord, IndicatorPreprocessor, MarketRegimeAnalyzer
)
from screener.morning_screener import NIFTY_50

@dataclass
class LogicConfig:
    market_gap_threshold: float = 0.006
    market_breadth_requirement: float = 0.30
    profit_target_pct: float = 0.01
    stop_loss_pct: float = 0.01
    rsi_exit_threshold: float = 70.0
    partial_booking_fraction: float = 0.5
    strategy_name: str = "BREAKOUT"
    dynamic_exit_ema: Optional[int] = 21
    trailing_sl: bool = False

class FastLogicExecutor:
    def __init__(
        self,
        config: LogicConfig,
        timeline_meta: List[Tuple],
        stock_ts_map: Dict[str, Dict[pd.Timestamp, int]],
        arrays: Dict[str, Dict[str, np.ndarray]],
        signals: Dict[str, List[bool]],
        active_timestamps: Set[pd.Timestamp],
        is_long: bool
    ):
        self.config = config
        self.timeline_meta = timeline_meta
        self.stock_ts_map = stock_ts_map
        self.arrays = arrays
        self.signals = signals
        self.active_timestamps = active_timestamps
        self.is_long = is_long
        
        self.trades: List[TradeRecord] = []
        self.open_positions: Dict[str, OpenPosition] = {}
        
    def execute(self) -> List[TradeRecord]:
        self.trades = []
        self.open_positions = {}
        
        for ts, date_only, is_valid_day, before_15 in self.timeline_meta:
            if not self.open_positions and (ts not in self.active_timestamps or not is_valid_day or not before_15):
                continue
            
            syms_to_close = []
            for sym, pos in list(self.open_positions.items()):
                if ts not in self.stock_ts_map.get(sym, {}):
                    continue
                
                idx = self.stock_ts_map[sym][ts]
                arr = self.arrays[sym]
                
                cp = float(arr["close"][idx])
                lp = float(arr["low"][idx])
                op = float(arr["open"][idx])
                hp = float(arr["high"][idx])
                
                # EOD Exit
                if ts.hour == 15 and ts.minute >= 15:
                    exit_price = op
                    self.trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=pos.entry_price, exit_price=exit_price, quantity=pos.quantity,
                        direction="LONG" if self.is_long else "SHORT", exit_reason="EOD_TIME"
                    ))
                    syms_to_close.append(sym)
                    continue
                
                # Dynamic Exit (EMA)
                if self.config.dynamic_exit_ema is not None and idx >= 1:
                    ema_key = f"ema{self.config.dynamic_exit_ema}"
                    ema_val = float(arr[ema_key][idx-1])
                    close_1 = float(arr["close"][idx-1])
                    
                    if self.is_long and close_1 < ema_val:
                        self.trades.append(TradeRecord(
                            symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                            entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                            direction="LONG", exit_reason="DYN_EXIT"
                        ))
                        syms_to_close.append(sym)
                        continue
                    elif not self.is_long and close_1 > ema_val:
                        self.trades.append(TradeRecord(
                            symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                            entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                            direction="SHORT", exit_reason="DYN_EXIT"
                        ))
                        syms_to_close.append(sym)
                        continue
                
                # Stop Loss
                if self.is_long:
                    if pos.stop_loss_price is not None and lp <= pos.stop_loss_price:
                        exit_price = min(pos.stop_loss_price, op) if op < pos.stop_loss_price else pos.stop_loss_price
                        self.trades.append(TradeRecord(
                            symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                            entry_price=pos.entry_price, exit_price=exit_price, quantity=pos.quantity,
                            direction="LONG", exit_reason="STOP_LOSS"
                        ))
                        syms_to_close.append(sym)
                        continue
                else:
                    if pos.stop_loss_price is not None and hp >= pos.stop_loss_price:
                        exit_price = max(pos.stop_loss_price, op) if op > pos.stop_loss_price else pos.stop_loss_price
                        self.trades.append(TradeRecord(
                            symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                            entry_price=pos.entry_price, exit_price=exit_price, quantity=pos.quantity,
                            direction="SHORT", exit_reason="STOP_LOSS"
                        ))
                        syms_to_close.append(sym)
                        continue
                
                # Overbought / Oversold
                if ts > pos.entry_time:
                    if self.is_long:
                        if arr["rsi_14"][idx] >= self.config.rsi_exit_threshold:
                            self.trades.append(TradeRecord(
                                symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                                entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                                direction="LONG", exit_reason="RSI_EXIT"
                            ))
                            syms_to_close.append(sym)
                            continue
                    else:
                        if arr["rsi_16"][idx] <= self.config.rsi_exit_threshold:
                            self.trades.append(TradeRecord(
                                symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                                entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                                direction="SHORT", exit_reason="RSI_EXIT"
                            ))
                            syms_to_close.append(sym)
                            continue
                
                # Partial Profit & Trailing SL
                if self.is_long:
                    target_price = pos.entry_price * (1 + self.config.profit_target_pct)
                    if hp >= target_price:
                        if not pos.partial_booked:
                            exit_price = max(target_price, op) if op > target_price else target_price
                            cover_qty = max(1, int(pos.quantity * self.config.partial_booking_fraction))
                            if cover_qty >= pos.quantity: cover_qty = max(0, pos.quantity - 1)
                            if cover_qty > 0:
                                self.trades.append(TradeRecord(
                                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                                    entry_price=pos.entry_price, exit_price=exit_price, quantity=cover_qty,
                                    direction="LONG", exit_reason="PARTIAL_PROFIT"
                                ))
                                pos.quantity -= cover_qty
                                pos.partial_booked = True
                                
                                # Trailing SL to breakeven
                                if self.config.trailing_sl:
                                    pos.stop_loss_price = pos.entry_price
                            
                            if pos.quantity <= 0:
                                syms_to_close.append(sym)
                                continue
                else:
                    target_price = pos.entry_price * (1 - self.config.profit_target_pct)
                    if lp <= target_price:
                        if not pos.partial_booked:
                            exit_price = min(target_price, op) if op < target_price else target_price
                            cover_qty = max(1, int(pos.quantity * self.config.partial_booking_fraction))
                            if cover_qty >= pos.quantity: cover_qty = max(0, pos.quantity - 1)
                            if cover_qty > 0:
                                self.trades.append(TradeRecord(
                                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                                    entry_price=pos.entry_price, exit_price=exit_price, quantity=cover_qty,
                                    direction="SHORT", exit_reason="PARTIAL_PROFIT"
                                ))
                                pos.quantity -= cover_qty
                                pos.partial_booked = True
                                
                                # Trailing SL to breakeven
                                if self.config.trailing_sl:
                                    pos.stop_loss_price = pos.entry_price
                                    
                            if pos.quantity <= 0:
                                syms_to_close.append(sym)
                                continue
                                
            for sym in syms_to_close:
                if sym in self.open_positions:
                    del self.open_positions[sym]
                    
            # New Entries
            if not is_valid_day or ts.hour >= 15:
                continue
                
            if len(self.open_positions) >= 2:  # max_open_positions = 2
                continue
                
            for sym in self.arrays.keys():
                if len(self.open_positions) >= 2:
                    break
                if sym in self.open_positions:
                    continue
                
                idx = self.stock_ts_map.get(sym, {}).get(ts)
                if idx is None or not self.signals[sym][idx]:
                    continue
                
                arr = self.arrays[sym]
                entry_price = float(arr["close"][idx])
                qty = int(500000 // entry_price) # 5L per trade (10L capital, max 2 pos)
                
                if qty > 0:
                    if self.is_long:
                        sl_price = entry_price * (1 - self.config.stop_loss_pct)
                        self.open_positions[sym] = OpenPosition(
                            symbol=sym, entry_time=ts, entry_price=entry_price,
                            quantity=qty, direction="LONG", stop_loss_price=sl_price, partial_booked=False
                        )
                    else:
                        sl_price = entry_price * (1 + self.config.stop_loss_pct)
                        self.open_positions[sym] = OpenPosition(
                            symbol=sym, entry_time=ts, entry_price=entry_price,
                            quantity=qty, direction="SHORT", stop_loss_price=sl_price, partial_booked=False
                        )
                        
        return self.trades

def enrich_and_precompute(stock_dfs):
    print("Enriching indicators...")
    for sym, df in stock_dfs.items():
        import ta
        df["rsi_14"] = ta.momentum.rsi(df["close"], window=14).fillna(50.0)
        df["rsi_16"] = ta.momentum.rsi(df["close"], window=16).fillna(50.0)
        df["ema50"] = ta.trend.ema_indicator(df["close"], window=50).ffill().fillna(0)
        df["ema21"] = ta.trend.ema_indicator(df["close"], window=21).ffill().fillna(0)
        df["ema20"] = ta.trend.ema_indicator(df["close"], window=20).ffill().fillna(0)
        df["ema9"] = ta.trend.ema_indicator(df["close"], window=9).ffill().fillna(0)
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        df["vwap"] = (tp * df["volume"]).groupby(df.index.date).cumsum() / df["volume"].groupby(df.index.date).cumsum()
        df["vwap"] = df["vwap"].ffill().fillna(0)
        
        # Median Price SMA 14
        df["median_price"] = (df["high"] + df["low"]) / 2.0
        df["median_sma14"] = df["median_price"].rolling(14).mean().fillna(0)

    print("Precomputing multi-strategy signals...")
    long_signals = {"BREAKOUT": {}, "TREND": {}}
    short_signals = {"BREAKDOWN": {}}
    
    for sym, df in stock_dfs.items():
        n = len(df)
        c0 = df["close"]
        c1 = c0.shift(1)
        v0 = df["vwap"]
        e20_1 = df["ema20"].shift(1)
        r1 = df["rsi_14"].shift(1)
        h10 = df["high"].shift(1).rolling(10).max()
        l10 = df["low"].shift(1).rolling(10).min()
        h1 = df["high"].shift(1)
        med14 = df["median_sma14"]
        e20_0 = df["ema20"]
        
        # Long Breakout
        lb = (c1 > v0) & (e20_1 > v0) & (r1 > 61) & (c0 > h10)
        lb.iloc[:11] = False
        long_signals["BREAKOUT"][sym] = lb.fillna(False).values
        
        # Long Trend
        lt = (med14 > v0) & (med14 > e20_0) & (c1 > e20_0) & (c0 > v0)
        lt.iloc[:14] = False
        long_signals["TREND"][sym] = lt.fillna(False).values
        
        # Short Breakdown
        sb = (c1 < v0) & (e20_1 < v0) & (r1 < 39) & (c0 < l10) & (c0 <= h1) & (c0 >= v0 * 0.988)
        sb.iloc[:11] = False
        short_signals["BREAKDOWN"][sym] = sb.fillna(False).values
        
    return long_signals, short_signals

def run_mega_sweep():
    print("Loading data...")
    stock_dfs = load_cache()
    stock_dfs = {s: d for s, d in stock_dfs.items() if s in NIFTY_50}
    
    long_signals_dict, short_signals_dict = enrich_and_precompute(stock_dfs)
    regime = MarketRegimeAnalyzer(stock_dfs)
    
    # Prebuild structures
    timeline_set = set()
    for df in stock_dfs.values(): timeline_set.update(df.index)
    timeline = sorted(list(timeline_set))
    timeline_filtered = [ts for ts in timeline if pd.Timestamp("2024-01-01") <= ts.tz_localize(None) <= pd.Timestamp("2026-06-21")]
    stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
    
    arrays = {}
    for sym, df in stock_dfs.items():
        arrays[sym] = {
            "close": df["close"].values, "high": df["high"].values, "low": df["low"].values, "open": df["open"].values,
            "rsi_14": df["rsi_14"].values, "rsi_16": df["rsi_16"].values, "ema9": df["ema9"].values, 
            "ema20": df["ema20"].values, "ema21": df["ema21"].values, "ema50": df["ema50"].values, "vwap": df["vwap"].values
        }

    long_params = [
        {"pt": 0.02, "sl": 0.007, "g": 0.006, "b": 0.3},
        {"pt": 0.03, "sl": 0.009, "g": 0.008, "b": 0.4},
        {"pt": 0.04, "sl": 0.011, "g": 0.010, "b": 0.5},
        {"pt": 0.05, "sl": 0.015, "g": 0.012, "b": 0.6}
    ]
    
    short_params = [
        {"pt": 0.02, "sl": 0.007, "g": -0.006, "b": 0.3},
        {"pt": 0.03, "sl": 0.009, "g": -0.008, "b": 0.4},
        {"pt": 0.04, "sl": 0.011, "g": -0.010, "b": 0.5}
    ]

    strategies = ["BREAKOUT", "TREND"]
    emas = [None, 9, 21, 50]
    tsls = [False, True]
    
    long_runs = []
    print("Sweeping Long Logic...")
    for strat in strategies:
        active_ts = set()
        for sym, sigs in long_signals_dict[strat].items():
            for idx, has_sig in enumerate(sigs):
                if has_sig: active_ts.add(stock_dfs[sym].index[idx])
                
        for p in long_params:
            class DummyCfg:
                market_gap_threshold = p["g"]
                market_breadth_requirement = p["b"]
            bull_days = regime.get_bull_days(DummyCfg())
            for ema in emas:
                for tsl in tsls:
                    cfg = LogicConfig(p["g"], p["b"], p["pt"], p["sl"], 75.0, 0.5, strat, ema, tsl)
                    t_meta = [(ts, ts.date(), ts.date() in bull_days, ts.hour < 15) for ts in timeline_filtered]
                    exe = FastLogicExecutor(cfg, t_meta, stock_ts_map, arrays, long_signals_dict[strat], active_ts, True)
                    trades = exe.execute()
                    if not trades: continue
                    gp = sum(t.pnl_net for t in trades if t.pnl_net > 0)
                    gl = sum(t.pnl_net for t in trades if t.pnl_net <= 0)
                    net = sum(t.pnl_gross - t.stt_tax for t in trades)
                    long_runs.append({"cfg": cfg, "trades": len(trades), "gp": gp, "gl": gl, "net": net})

    short_runs = []
    print("Sweeping Short Logic...")
    active_ts = set()
    for sym, sigs in short_signals_dict["BREAKDOWN"].items():
        for idx, has_sig in enumerate(sigs):
            if has_sig: active_ts.add(stock_dfs[sym].index[idx])
    
    for p in short_params:
        class DummyCfg:
            market_gap_threshold = p["g"]
            market_breadth_requirement = p["b"]
        bear_days = regime.get_bear_days(DummyCfg())
        for ema in emas:
            for tsl in tsls:
                cfg = LogicConfig(p["g"], p["b"], p["pt"], p["sl"], 20.0, 0.5, "BREAKDOWN", ema, tsl)
                t_meta = [(ts, ts.date(), ts.date() in bear_days, ts.hour < 15) for ts in timeline_filtered]
                exe = FastLogicExecutor(cfg, t_meta, stock_ts_map, arrays, short_signals_dict["BREAKDOWN"], active_ts, False)
                trades = exe.execute()
                if not trades: continue
                gp = sum(t.pnl_net for t in trades if t.pnl_net > 0)
                gl = sum(t.pnl_net for t in trades if t.pnl_net <= 0)
                net = sum(t.pnl_gross - t.stt_tax for t in trades)
                short_runs.append({"cfg": cfg, "trades": len(trades), "gp": gp, "gl": gl, "net": net})

    print("Combining...")
    results = []
    for lr in long_runs:
        for sr in short_runs:
            total_net = lr["net"] + sr["net"]
            net_pct = total_net / 1000000.0 * 100.0
            
            # Fast filter: save all results
            results.append({
                "net_pct": net_pct,
                "lr": lr,
                "sr": sr
            })
                
    results.sort(key=lambda x: x["net_pct"], reverse=True)
    
    with open("research/best_opt_params.txt", "w") as f:
        for i, r in enumerate(results[:10]):
            f.write(f"=== RANK {i+1} ===\n")
            f.write(f"Net Return: {r['net_pct']:.2f}%\n")
            f.write(f"Total Trades: {r['lr']['trades'] + r['sr']['trades']}\n")
            f.write(f"LONG LOGIC:\n")
            f.write(f"  Strategy: {r['lr']['cfg'].strategy_name}\n")
            f.write(f"  Dynamic Exit EMA: {r['lr']['cfg'].dynamic_exit_ema}\n")
            f.write(f"  Trailing SL: {r['lr']['cfg'].trailing_sl}\n")
            f.write(f"  Profit Target: {r['lr']['cfg'].profit_target_pct}\n")
            f.write(f"  Stop Loss: {r['lr']['cfg'].stop_loss_pct}\n")
            f.write(f"  Gap: {r['lr']['cfg'].market_gap_threshold}\n")
            f.write(f"  Breadth: {r['lr']['cfg'].market_breadth_requirement}\n")
            f.write(f"SHORT LOGIC:\n")
            f.write(f"  Strategy: {r['sr']['cfg'].strategy_name}\n")
            f.write(f"  Dynamic Exit EMA: {r['sr']['cfg'].dynamic_exit_ema}\n")
            f.write(f"  Trailing SL: {r['sr']['cfg'].trailing_sl}\n")
            f.write(f"  Profit Target: {r['sr']['cfg'].profit_target_pct}\n")
            f.write(f"  Stop Loss: {r['sr']['cfg'].stop_loss_pct}\n")
            f.write(f"  Gap: {r['sr']['cfg'].market_gap_threshold}\n")
            f.write(f"  Breadth: {r['sr']['cfg'].market_breadth_requirement}\n\n")

if __name__ == "__main__":
    run_mega_sweep()
