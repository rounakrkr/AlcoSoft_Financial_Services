import sys
import os
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener.morning_screener import NIFTY_50
from core.strategy import (
    CONDITION_REGISTRY,
    StrategySetEvaluator,
    StrategyEvaluationContext,
    _build_indicators
)
from core.strategy_sets import load_strategy_sets
from core.order_executor import calculate_stop_loss, calculate_target, round_to_tick
from core.trading_settings import get as cfg

import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

class BacktestRunner:
    def __init__(self, initial_capital: float, buy_strategy_name: str, sell_strategy_name: str, use_margin: bool = False):
        self.initial_capital = initial_capital
        self.buy_strategy_name = buy_strategy_name
        self.sell_strategy_name = sell_strategy_name
        self.use_margin = use_margin
        
        self.evaluator = StrategySetEvaluator(CONDITION_REGISTRY)
        self.trades = []

        self.stop_loss_percent = max(0.0001, min(0.20, float(cfg("risk", "stop_loss_percent", 0.01))))
        self.target_rr_ratio = max(0.1, min(10.0, float(cfg("risk", "target_rr_ratio", 2.0))))
        self.tsl_activation_ratio = max(1.0, min(2.0, float(cfg("risk", "tsl_activation_ratio", 1.4))))
        self.tsl_mode_after_activation = bool(cfg("risk", "tsl_mode_after_activation", True))
        self.trailing_sl_percent = max(0.0001, min(0.20, float(cfg("risk", "trailing_sl_percent", 0.008))))
        
        self.margin_leverage = max(1.0, min(5.0, float(cfg("risk", "margin_leverage", 2.0)))) if self.use_margin else 1.0
        self.position_size_margin = max(0.10, min(1.0, float(cfg("risk", "position_size_margin", 1.0))))

        config = load_strategy_sets()
        self.buy_set_def = next((s for s in config.buy_sets if s.name == self.buy_strategy_name), None)
        self.sell_set_def = next((s for s in config.sell_sets if s.name == self.sell_strategy_name), None)

        if not self.buy_set_def or not self.sell_set_def:
            raise ValueError("Invalid strategy names")

    def run(self):
        print(f"Starting Independent Backtest on {len(NIFTY_50)} symbols. Fetching 60 days of 5m data...")
        
        for idx, symbol in enumerate(NIFTY_50):
            print(f"Processing {idx+1}/{len(NIFTY_50)}: {symbol}")
            self._process_symbol(symbol)
            
        print("Backtest processing complete.")
        return self.trades

    def _fetch_history(self, symbol: str) -> pd.DataFrame:
        from screener.morning_screener import _fetch_yahoo_history
        try:
            # 60d trading days = approx 84 normal days
            df = _fetch_yahoo_history(symbol, period="60d", interval="5m")
            if df.empty:
                return pd.DataFrame()
            df.columns = [col.lower() for col in df.columns]
            df.dropna(subset=["close"], inplace=True)
            return df
        except Exception as e:
            return pd.DataFrame()

    def _process_symbol(self, symbol: str):
        df = self._fetch_history(symbol)
        if df.empty or len(df) < 50:
            return

        df["bucket"] = df.index
        df = _build_indicators(df)
        df.dropna(subset=["ema21", "macd", "vwap"], inplace=True)
        
        if len(df) < 20:
            return

        in_position = False
        entry_time = None
        entry_price = 0.0
        quantity = 0
        initial_sl = 0.0
        target = 0.0
        trailing_sl = 0.0
        tsl_activated = False

        trades_today = 0
        current_date = None

        for i in range(10, len(df)):
            sliced_df = df.iloc[:i+1]
            current_candle = sliced_df.iloc[-1]
            current_time = sliced_df.index[-1]
            close_price = current_candle["close"]
            high_price = current_candle["high"]
            low_price = current_candle["low"]

            candle_date = current_time.date()
            if candle_date != current_date:
                current_date = candle_date
                trades_today = 0

            if not in_position:
                if current_time.hour == 15 and current_time.minute >= 0:
                    continue
                # MAX TRADE CYCLES PER DAY = 1 (to match Streak)
                if trades_today >= 1:
                    continue

                ctx = StrategyEvaluationContext(side="buy", indicator_df=sliced_df, pattern_df=sliced_df, ws_count=0)
                condition_results = self.evaluator._evaluate_conditions(self.buy_set_def, ctx)
                
                if condition_results and all(r.get("fired") for r in condition_results):
                    # Enter on next candle open
                    if i + 1 < len(df):
                        next_candle_time = df.index[i+1]
                        next_candle_open = df.iloc[i+1]["open"]
                        in_position = True
                        entry_time = next_candle_time
                        entry_price = next_candle_open
                        trades_today += 1
                    
                    if self.use_margin:
                        capital_available = self.initial_capital * self.margin_leverage
                        allocated_capital = capital_available * self.position_size_margin
                    else:
                        allocated_capital = self.initial_capital * self.position_size_margin

                    quantity = int(allocated_capital // entry_price)
                    if quantity == 0:
                        in_position = False
                        trades_today -= 1
                        continue
                    
                    initial_sl = calculate_stop_loss(entry_price, "BUY")
                    target = calculate_target(entry_price, initial_sl)
                    trailing_sl = initial_sl
                    tsl_activated = False

            else:
                exit_reason = None
                exit_price = 0.0

                if low_price <= trailing_sl:
                    exit_reason = "STOPLOSS/TRAILING_SL"
                    exit_price = min(trailing_sl, current_candle["open"])
                elif high_price >= target:
                    exit_reason = "TARGET"
                    exit_price = max(target, current_candle["open"])
                elif current_time.hour == 15 and current_time.minute >= 15:
                    exit_reason = "SQUAREOFF"
                    exit_price = close_price
                else:
                    ctx = StrategyEvaluationContext(side="sell", indicator_df=sliced_df, pattern_df=sliced_df, ws_count=0)
                    condition_results = self.evaluator._evaluate_conditions(self.sell_set_def, ctx)
                    if condition_results and all(r.get("fired") for r in condition_results):
                        if i + 1 < len(df):
                            exit_reason = "SELL_STRATEGY"
                            exit_price = df.iloc[i+1]["open"]
                            current_time = df.index[i+1] # Update exit time to next candle
                if not exit_reason:
                    if not tsl_activated:
                        sl_percent = abs(entry_price - initial_sl) / entry_price
                        activation_threshold = entry_price + (entry_price * sl_percent * self.tsl_activation_ratio)
                        if high_price >= activation_threshold:
                            tsl_activated = True
                    
                    if tsl_activated:
                        raw_tsl = high_price * (1 - self.trailing_sl_percent)
                        new_tsl = round_to_tick(raw_tsl)
                        if new_tsl > trailing_sl:
                            trailing_sl = new_tsl

                if exit_reason:
                    pnl = (exit_price - entry_price) * quantity
                    self.trades.append({
                        "stock": symbol,
                        "buy_strategy": self.buy_strategy_name,
                        "sell_strategy": self.sell_strategy_name,
                        "entry_time": entry_time,
                        "exit_time": current_time,
                        "quantity": quantity,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "pnl": pnl,
                        "exit_reason": exit_reason
                    })
                    
                    in_position = False
                    entry_time = None
                    entry_price = 0.0
                    quantity = 0
                    initial_sl = 0.0
                    target = 0.0
                    trailing_sl = 0.0
                    tsl_activated = False
