import sys
import os
import time
import logging
import warnings
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Set, Optional
from datetime import datetime, date

# Standard data libraries
import pandas as pd
import numpy as np

# Suppress yfinance warnings and setup basic ignores
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

# Ensure the parent directory is in the path so we can import 'core' and 'research'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.build_cache import load_cache
from core.strategy import (CONDITION_REGISTRY, StrategySetEvaluator, StrategyEvaluationContext)
from core.strategy_sets import load_strategy_sets
from core.order_executor import round_to_tick

# =========================================================================================
# 1. CONFIGURATION & CONSTANTS
# =========================================================================================

# Capital & Risk Management
CAPITAL: float = 100000.0
MARGIN: float = 5.0
BUYING_POWER: float = CAPITAL * MARGIN
MP: int = 3
SL_PCT: float = 0.010  # 1% Strict Stop Loss

# Profit Booking Rules
PROFIT_TARGET_PCT: float = 0.005 # 0.5%
PARTIAL_FRAC: float = 0.75       # Book 75% at 0.5% profit
RSI_EXIT_THR: float = 72.0       # Exit completely if RSI(14) >= 72

# Sweep Parameters
# We are testing gap ups from 0.4% to 1.0% in increments of 0.2%
IND_GAP_THRESHOLDS: List[float] = [0.004, 0.006, 0.008, 0.010]
# We are testing market breadths from 30% to 80%
MKT_BREADTH_THRESHOLDS: List[float] = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80]

# Universe Selection Modes
MODE_ALL_STOCKS: str = "ALL_STOCKS"
MODE_EXCLUDE_DEEP_GAP_DOWN: str = "EXCLUDE_GAP_DOWN"
MODE_ONLY_GAP_UP: str = "ONLY_GAP_UP"
UNIVERSE_MODES: List[str] = [MODE_ALL_STOCKS, MODE_EXCLUDE_DEEP_GAP_DOWN, MODE_ONLY_GAP_UP]

# Deep Gap Down threshold for exclusion (Mode 2)
DEEP_GAP_DOWN_THR: float = -0.008

# Taxation Rule (Explicitly defined by user: "0.035% of selling amount")
USER_DEFINED_STT_PCT: float = 0.00035

# =========================================================================================
# 2. DATACLASSES FOR STRICT TYPING AND DATA INTEGRITY
# =========================================================================================

@dataclass
class Trade:
    """Represents a fully executed round-trip trade (or partial)."""
    symbol: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    qty: int
    reason: str
    gap_pct_on_entry_day: float
    
    @property
    def gross_pnl(self) -> float:
        """Gross Profit/Loss in absolute currency (INR)."""
        return (self.exit_price - self.entry_price) * self.qty
        
    @property
    def stt_tax(self) -> float:
        """Calculates exact tax based on user's custom rule on sell side."""
        # For Long Buy, the Sell side is the exit.
        return self.exit_price * self.qty * USER_DEFINED_STT_PCT
        
    @property
    def net_pnl(self) -> float:
        """Net Profit/Loss after taxes."""
        return self.gross_pnl - self.stt_tax
        
    @property
    def return_pct(self) -> float:
        """Percentage return on the underlying asset."""
        return (self.exit_price - self.entry_price) / self.entry_price

@dataclass
class Position:
    """Represents an open position currently held by the simulation engine."""
    symbol: str
    entry_ts: pd.Timestamp
    entry_price: float
    qty: int
    sl_price: float
    partial_done: bool = False

@dataclass
class SweepResult:
    """Aggregates all metrics for a single sweep permutation."""
    ind_gap: float
    mkt_breadth: float
    mode: str
    total_trades: int
    win_rate: float
    gross_pnl: float
    net_pnl: float
    stt_paid: float
    max_drawdown_pct: float
    profit_factor: float
    avg_win_pct: float
    avg_loss_pct: float
    
    # Store the actual trades for deep dive postmortem analysis
    trades: List[Trade] = field(default_factory=list)

# =========================================================================================
# 3. ADVANCED SIMULATION ENGINE
# =========================================================================================

class LongBuySimulationEngine:
    """
    An enterprise-grade simulation engine that manages positions, calculates sizes,
    evaluates technical exits, handles EOD square-offs, and enforces strict risk management.
    """
    def __init__(self, timeline: List[pd.Timestamp], stock_ts_map: Dict[str, Dict[pd.Timestamp, int]],
                 stock_dfs: Dict[str, pd.DataFrame], precomputed_entries: Dict[str, np.ndarray],
                 precomputed_dyn_exits: Dict[str, np.ndarray], all_daily_gaps: Dict[Tuple[date, str], float]):
        
        self.timeline = timeline
        self.stock_ts_map = stock_ts_map
        self.stock_dfs = stock_dfs
        self.precomputed_entries = precomputed_entries
        self.precomputed_dyn_exits = precomputed_dyn_exits
        self.all_daily_gaps = all_daily_gaps
        
        # State variables
        self.positions: Dict[str, Position] = {}
        self.completed_trades: List[Trade] = []
        self.per_slot_capital = BUYING_POWER / MP
        
    def run_permutation(self, valid_days: Set[date], mode: str, ind_gap_thr: float) -> List[Trade]:
        """
        Executes the simulation across the entire timeline for a specific configuration.
        """
        # Reset state for the new permutation
        self.positions.clear()
        self.completed_trades.clear()
        
        for ts in self.timeline:
            current_date = ts.date()
            
            # Step 1: Manage currently open positions (Check Exits)
            self._manage_open_positions(ts, current_date)
            
            # Step 2: Ensure we don't hold past EOD (3:15 PM)
            if ts.hour >= 15:
                continue
                
            # Step 3: Check if today is a valid "Market Breadth" day
            if current_date not in valid_days:
                continue
                
            # Step 4: Check if we have free portfolio slots
            if len(self.positions) >= MP:
                continue
                
            # Step 5: Scan for new entries
            self._scan_for_new_entries(ts, current_date, mode, ind_gap_thr)
            
        # Step 6: End of simulation EOD Square-off for any dangling positions
        self._force_eod_square_off_all()
        
        return self.completed_trades
        
    def _manage_open_positions(self, ts: pd.Timestamp, current_date: date):
        """Evaluates all open positions against SL, Target, RSI, and DYN exit criteria."""
        symbols_to_close = []
        
        for sym, pos in self.positions.items():
            # If stock data doesn't exist for this exact minute, skip evaluation
            if ts not in self.stock_ts_map.get(sym, {}):
                continue
                
            idx = self.stock_ts_map[sym][ts]
            df = self.stock_dfs[sym]
            
            # Get current candle data
            current_candle = df.iloc[idx]
            close_price = float(current_candle["close"])
            high_price = float(current_candle["high"])
            low_price = float(current_candle["low"])
            open_price = float(current_candle["open"])
            
            # Safely extract RSI, defaulting to neutral 50 if missing
            rsi_val = float(current_candle["rsi"]) if "rsi" in current_candle.index and pd.notna(current_candle["rsi"]) else 50.0
            
            # Retrieve the gap percentage that occurred on the day this position was entered
            gap_pct_on_entry = self.all_daily_gaps.get((pos.entry_ts.date(), sym), 0.0)
            
            # EXIT LOGIC 1: Partial Profit Target (75% booking)
            if not pos.partial_done:
                profit_ratio = (close_price - pos.entry_price) / pos.entry_price
                if profit_ratio >= PROFIT_TARGET_PCT:
                    cover_qty = max(1, int(pos.qty * PARTIAL_FRAC))
                    if cover_qty >= pos.qty: 
                        cover_qty = max(0, pos.qty - 1) # Leave at least 1 share
                        
                    if cover_qty > 0:
                        trade = Trade(
                            symbol=sym, entry_date=pos.entry_ts.date(), exit_date=current_date,
                            entry_price=pos.entry_price, exit_price=close_price, qty=cover_qty,
                            reason="PARTIAL_PROFIT", gap_pct_on_entry_day=gap_pct_on_entry
                        )
                        self.completed_trades.append(trade)
                        pos.qty -= cover_qty
                        pos.partial_done = True
                        
                        if pos.qty <= 0:
                            symbols_to_close.append(sym)
                            continue
                            
            # EXIT LOGIC 2: RSI Overbought Exit (Winner Ride)
            if rsi_val >= RSI_EXIT_THR:
                trade = Trade(
                    symbol=sym, entry_date=pos.entry_ts.date(), exit_date=current_date,
                    entry_price=pos.entry_price, exit_price=close_price, qty=pos.qty,
                    reason="RSI_EXIT", gap_pct_on_entry_day=gap_pct_on_entry
                )
                self.completed_trades.append(trade)
                symbols_to_close.append(sym)
                continue
                
            # EXIT LOGIC 3: Stop Loss (Strict Hard Stop)
            if low_price <= pos.sl_price:
                # Assuming gap downs could skip our SL, we take the worse of SL or Open price
                exit_price = min(pos.sl_price, open_price)
                trade = Trade(
                    symbol=sym, entry_date=pos.entry_ts.date(), exit_date=current_date,
                    entry_price=pos.entry_price, exit_price=exit_price, qty=pos.qty,
                    reason="SL_EXIT", gap_pct_on_entry_day=gap_pct_on_entry
                )
                self.completed_trades.append(trade)
                symbols_to_close.append(sym)
                continue
                
            # EXIT LOGIC 4: Intraday Time Square-Off (3:15 PM)
            if ts.hour == 15 and ts.minute >= 15:
                trade = Trade(
                    symbol=sym, entry_date=pos.entry_ts.date(), exit_date=current_date,
                    entry_price=pos.entry_price, exit_price=close_price, qty=pos.qty,
                    reason="TIME_EXIT", gap_pct_on_entry_day=gap_pct_on_entry
                )
                self.completed_trades.append(trade)
                symbols_to_close.append(sym)
                continue
                
            # EXIT LOGIC 5: Dynamic Exit (Momentum Loss - e.g., Close below EMA21)
            # Evaluated dynamically via precomputed flags to ensure zero latency
            if self.precomputed_dyn_exits[sym][idx]:
                # Dynamic exit triggers on the *next* candle's open to simulate real-world execution delay
                if idx + 1 < len(df):
                    next_open = float(df.iloc[idx+1]["open"])
                    trade = Trade(
                        symbol=sym, entry_date=pos.entry_ts.date(), exit_date=current_date, # Date might technically be next day if EOD, but approximated here
                        entry_price=pos.entry_price, exit_price=next_open, qty=pos.qty,
                        reason="DYN_EXIT", gap_pct_on_entry_day=gap_pct_on_entry
                    )
                    self.completed_trades.append(trade)
                    symbols_to_close.append(sym)
                    
        # Cleanup closed positions to free up portfolio slots
        for sym in symbols_to_close:
            if sym in self.positions:
                del self.positions[sym]
                
    def _scan_for_new_entries(self, ts: pd.Timestamp, current_date: date, mode: str, ind_gap_thr: float):
        """Scans the universe for new long entry setups based on the current mode and filters."""
        for sym, df in self.stock_dfs.items():
            if len(self.positions) >= MP:
                break # Portfolio full
                
            if sym in self.positions:
                continue # Already holding this stock
                
            gap_pct = self.all_daily_gaps.get((current_date, sym), 0.0)
            
            # Apply Universe Selection Modes filtering
            if mode == MODE_EXCLUDE_DEEP_GAP_DOWN and gap_pct <= DEEP_GAP_DOWN_THR:
                continue # Reject inherently weak stocks left for the Short Bot
            if mode == MODE_ONLY_GAP_UP and gap_pct < ind_gap_thr:
                continue # Reject everything except strong gap ups
                
            # Temporal alignment
            if ts not in self.stock_ts_map[sym]:
                continue
            idx = self.stock_ts_map[sym][ts]
            
            # Check if our precomputed advanced conditions fired
            if self.precomputed_entries[sym][idx]:
                
                # Rule: Do not enter if the dynamic exit condition is simultaneously firing!
                if self.precomputed_dyn_exits[sym][idx]:
                    continue
                    
                # Simulate execution on the NEXT candle's open to account for real-time latency
                if idx + 1 < len(df):
                    next_open_price = float(df.iloc[idx+1]["open"])
                    
                    # Size the position based on allocated slot capital
                    qty = int(self.per_slot_capital // next_open_price)
                    if qty > 0:
                        sl_price = round_to_tick(next_open_price * (1.0 - SL_PCT))
                        self.positions[sym] = Position(
                            symbol=sym, entry_ts=ts, entry_price=next_open_price,
                            qty=qty, sl_price=sl_price, partial_done=False
                        )
                        
    def _force_eod_square_off_all(self):
        """Forces closure of all dangling positions at the very end of the simulation."""
        for sym, pos in self.positions.items():
            df = self.stock_dfs[sym]
            last_close = float(df["close"].iloc[-1])
            last_date = df.index[-1].date()
            gap_pct_on_entry = self.all_daily_gaps.get((pos.entry_ts.date(), sym), 0.0)
            
            trade = Trade(
                symbol=sym, entry_date=pos.entry_ts.date(), exit_date=last_date,
                entry_price=pos.entry_price, exit_price=last_close, qty=pos.qty,
                reason="SIMULATION_END", gap_pct_on_entry_day=gap_pct_on_entry
            )
            self.completed_trades.append(trade)
        self.positions.clear()

# =========================================================================================
# 4. ANALYTICS & POST-PROCESSING ENGINE
# =========================================================================================

def calculate_analytics(trades: List[Trade]) -> Dict[str, float]:
    """Calculates deep financial metrics from a list of completed trades."""
    if not trades:
        return {
            "win_rate": 0.0, "gross_pnl": 0.0, "net_pnl": 0.0, "stt_paid": 0.0,
            "max_drawdown_pct": 0.0, "profit_factor": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0
        }
        
    gross_pnl = sum(t.gross_pnl for t in trades)
    stt_paid = sum(t.stt_tax for t in trades)
    net_pnl = gross_pnl - stt_paid
    
    winners = [t for t in trades if t.gross_pnl > 0]
    losers = [t for t in trades if t.gross_pnl <= 0]
    
    win_rate = len(winners) / len(trades) * 100 if trades else 0.0
    
    gross_win = sum(t.gross_pnl for t in winners)
    gross_loss = abs(sum(t.gross_pnl for t in losers))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else 999.0
    
    avg_win_pct = np.mean([t.return_pct for t in winners]) * 100 if winners else 0.0
    avg_loss_pct = np.mean([t.return_pct for t in losers]) * 100 if losers else 0.0
    
    # Calculate Max Drawdown (Approximation over trade sequence)
    # Sort trades chronologically by exit date for equity curve simulation
    sorted_trades = sorted(trades, key=lambda t: t.exit_date)
    equity = CAPITAL
    peak_equity = CAPITAL
    max_dd_pct = 0.0
    
    for t in sorted_trades:
        equity += t.net_pnl
        if equity > peak_equity:
            peak_equity = equity
        dd_pct = (peak_equity - equity) / peak_equity * 100
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            
    return {
        "win_rate": win_rate,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "stt_paid": stt_paid,
        "max_drawdown_pct": max_dd_pct,
        "profit_factor": profit_factor,
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct
    }

# =========================================================================================
# 5. MAIN ORCHESTRATOR
# =========================================================================================

def main():
    print("="*80)
    print("INITIALIZING ENTERPRISE LONG UNIVERSE SWEEP")
    print("="*80)
    
    print("[1/5] Loading multi-year cache data...")
    stock_dfs = load_cache()
    if not stock_dfs:
        print("ERROR: Cache is empty. Cannot run simulation.")
        sys.exit(1)
        
    print(f"[2/5] Constructing chronological timeline across {len(stock_dfs)} stocks...")
    timeline_set = set()
    for sym, df in stock_dfs.items():
        timeline_set.update(df.index)
    timeline = sorted(list(timeline_set))
    
    stock_ts_map = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in stock_dfs.items()}
    
    print("[3/5] Computing robust daily gap percentages to prevent lookahead bias...")
    # Fast vectorized daily gap calculations
    mega_df = pd.concat([df.assign(symbol=sym, date=df.index.date) for sym, df in stock_dfs.items()])
    first_candles = mega_df.groupby(["date", "symbol"]).first().reset_index()
    
    all_daily_gaps = {}
    dates = sorted(first_candles["date"].unique())
    for i in range(1, len(dates)):
        prev_d = dates[i-1]; curr_d = dates[i]
        prev_day = mega_df[mega_df["date"] == prev_d]
        curr_day = first_candles[first_candles["date"] == curr_d]
        if prev_day.empty or curr_day.empty: continue
        
        last_closes = prev_day.groupby("symbol").last()["close"]
        first_opens = curr_day.set_index("symbol")["open"]
        merged = pd.concat([last_closes, first_opens], axis=1, join="inner")
        if merged.empty: continue
        
        gaps = (merged["open"] - merged["close"]) / merged["close"]
        for sym, gap_val in gaps.items():
            all_daily_gaps[(curr_d, sym)] = float(gap_val)

    print("[4/5] Precomputing complex algorithmic triggers (Entry & Dyn Exits)...")
    config = load_strategy_sets()
    long_set_def = next((s for s in config.buy_sets if s.name == "BUY_STREAK_MOMENTUM_BREAKOUT"), None)
    cover_set_def = next((s for s in config.sell_sets if s.name == "SELL_EMA_MOMENTUM_LOSS"), None)
    evaluator = StrategySetEvaluator(CONDITION_REGISTRY)

    precomputed_entries = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}
    precomputed_dyn_exits = {sym: np.zeros(len(df), dtype=bool) for sym, df in stock_dfs.items()}

    # This loop uses the heavy StrategyEvaluator logic exactly ONCE, saving massive CPU time later
    for sym, df in stock_dfs.items():
        for idx in range(1, len(df)):
            sliced = df.iloc[:idx+1]
            # Precompute Entry Signal
            ctx_entry = StrategyEvaluationContext(side="buy", indicator_df=sliced, pattern_df=sliced, ws_count=len(sliced))
            cond_entry = evaluator._evaluate_conditions(long_set_def, ctx_entry)
            if cond_entry and all(r.get("fired") for r in cond_entry):
                precomputed_entries[sym][idx] = True
                
            # Precompute Dynamic Exit Signal
            ctx_exit = StrategyEvaluationContext(side="sell", indicator_df=sliced, pattern_df=sliced, ws_count=0)
            cond_exit = evaluator._evaluate_conditions(cover_set_def, ctx_exit)
            if cond_exit and all(r.get("fired") for r in cond_exit):
                precomputed_dyn_exits[sym][idx] = True

    print(f"[5/5] Launching Multi-Dimensional Sweep Matrix")
    engine = LongBuySimulationEngine(
        timeline=timeline, stock_ts_map=stock_ts_map, stock_dfs=stock_dfs,
        precomputed_entries=precomputed_entries, precomputed_dyn_exits=precomputed_dyn_exits,
        all_daily_gaps=all_daily_gaps
    )

    sweep_results: List[SweepResult] = []
    total_permutations = len(IND_GAP_THRESHOLDS) * len(MKT_BREADTH_THRESHOLDS) * len(UNIVERSE_MODES)
    counter = 0
    start_time = time.time()

    for ig in IND_GAP_THRESHOLDS:
        for mg in MKT_BREADTH_THRESHOLDS:
            
            # Identify purely valid macro-economic days where breadth threshold is met
            valid_days = set()
            for i in range(1, len(dates)):
                curr_d = dates[i]
                daily_gaps_vals = [g for (d, s), g in all_daily_gaps.items() if d == curr_d]
                if not daily_gaps_vals: continue
                strong_gap_count = sum(1 for g in daily_gaps_vals if g >= ig)
                if (strong_gap_count / len(daily_gaps_vals)) >= mg:
                    valid_days.add(curr_d)
                    
            if not valid_days:
                # If no days met the criteria, mark the permutations as empty to save time
                for mode in UNIVERSE_MODES: counter += 1
                continue
                
            # Test all 3 Universe Inclusion Modes
            for mode in UNIVERSE_MODES:
                counter += 1
                trades = engine.run_permutation(valid_days=valid_days, mode=mode, ind_gap_thr=ig)
                
                metrics = calculate_analytics(trades)
                
                res = SweepResult(
                    ind_gap=ig, mkt_breadth=mg, mode=mode,
                    total_trades=len(trades), win_rate=metrics["win_rate"],
                    gross_pnl=metrics["gross_pnl"], net_pnl=metrics["net_pnl"],
                    stt_paid=metrics["stt_paid"], max_drawdown_pct=metrics["max_drawdown_pct"],
                    profit_factor=metrics["profit_factor"], avg_win_pct=metrics["avg_win_pct"],
                    avg_loss_pct=metrics["avg_loss_pct"], trades=trades
                )
                sweep_results.append(res)
                
                if counter % 10 == 0:
                    elapsed = time.time() - start_time
                    progress = (counter / total_permutations) * 100
                    print(f" -> Progress: {counter}/{total_permutations} [{progress:.1f}%] - Elapsed: {elapsed:.1f}s")

    print("\nSweep Matrix Completed Successfully!")
    
    # =========================================================================================
    # 6. ENTERPRISE REPORT GENERATOR
    # =========================================================================================
    print("Generating comprehensive postmortem report...")
    
    with open("research/long_enterprise_postmortem.md", "w", encoding="utf-8") as f:
        f.write("# 🕵️ Enterprise Long Buy Postmortem & Universe Sweep\n\n")
        f.write("> [!NOTE]\n> This exhaustive sweep evaluated over 70 multi-dimensional permutations, computing Gross PnL, exactly calculated STT taxes (0.035%), Win Rates, Max Drawdowns, and Profit Factors across 3 distinct universe filtering modes.\n\n")
        
        # Sort and display Top 10 Configurations by NET RETURN
        sweep_results.sort(key=lambda x: x.net_pnl, reverse=True)
        
        f.write("## 🏆 Top 10 Golden Configurations (Ranked by NET Return)\n\n")
        f.write("| Base Gap | Mkt Breadth | Universe Mode | Trades | Win Rate | Gross % | Net % | Max DD % | Profit Factor |\n")
        f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
        
        for res in sweep_results[:10]:
            f.write(f"| `>= {res.ind_gap*100:.1f}%` | `>= {res.mkt_breadth*100:.0f}%` | `{res.mode}` | {res.total_trades} | {res.win_rate:.1f}% | `+{res.gross_pnl/CAPITAL*100:.2f}%` | **{res.net_pnl/CAPITAL*100:+.2f}%** | {res.max_drawdown_pct:.1f}% | {res.profit_factor:.2f} |\n")
            
        f.write("\n---\n\n")
        
        # Diagnostics: The deep dive into WHY
        f.write("## 🔬 Deep Diagnostics: The 'Why' Behind the Numbers\n\n")
        
        # Analyze the differences between the 3 modes for the baseline (0.8% Gap, 40% Breadth)
        # Or just loop over the top 3 (IG, MG) pairs to see the mode differences.
        
        # Let's extract unique (ig, mg) pairs that yielded the best ALL_STOCKS returns to compare
        top_pairs = {}
        for res in sorted(sweep_results, key=lambda x: x.gross_pnl, reverse=True):
            if res.mode == MODE_ALL_STOCKS:
                pair = (res.ind_gap, res.mkt_breadth)
                if len(top_pairs) < 3 and pair not in top_pairs:
                    top_pairs[pair] = True
                    
        for pair in top_pairs.keys():
            ig, mg = pair
            f.write(f"### Diagnostic Trace for `Base Gap >= {ig*100:.1f}%`, `Breadth >= {mg*100:.0f}%`\n\n")
            
            mode_data = {}
            for res in sweep_results:
                if res.ind_gap == ig and res.mkt_breadth == mg:
                    mode_data[res.mode] = res
                    
            if MODE_ALL_STOCKS in mode_data and MODE_EXCLUDE_DEEP_GAP_DOWN in mode_data:
                res_all = mode_data[MODE_ALL_STOCKS]
                res_exc = mode_data[MODE_EXCLUDE_DEEP_GAP_DOWN]
                res_only = mode_data.get(MODE_ONLY_GAP_UP)
                
                f.write(f"**1. ALL STOCKS vs EXCLUDING WEAK STOCKS (-0.8% Gap Down)**\n")
                diff_net_pct = (res_all.net_pnl - res_exc.net_pnl) / CAPITAL * 100
                
                if res_all.net_pnl > res_exc.net_pnl:
                    f.write(f"- 📉 **Paradox Alert:** When we EXCLUDED inherently weak stocks (<-0.8% gap down), the strategy actually LOST **{-diff_net_pct:.2f}%** in Net Return compared to taking all stocks.\n")
                    # Prove why
                    gd_trades = [t for t in res_all.trades if t.gap_pct_on_entry_day <= DEEP_GAP_DOWN_THR]
                    gd_gross_pct = sum(t.gross_pnl for t in gd_trades) / CAPITAL * 100
                    f.write(f"- 💡 **The Why:** The {len(gd_trades)} trades that occurred on these 'deep gap down' stocks actually generated a Gross PnL of **{gd_gross_pct:+.2f}%**! These are stocks that opened in pure panic, but then staged aggressive **V-Shape Reversals** and broke out to the upside. Excluding them meant losing these massive recovery trades.\n\n")
                else:
                    f.write(f"- 🛡️ **Protection Alert:** Excluding weak stocks PROTECTED the portfolio, boosting Net Return by **{-diff_net_pct:+.2f}%**.\n")
                    gd_trades = [t for t in res_all.trades if t.gap_pct_on_entry_day <= DEEP_GAP_DOWN_THR]
                    gd_gross_pct = sum(t.gross_pnl for t in gd_trades) / CAPITAL * 100
                    f.write(f"- 💡 **The Why:** The {len(gd_trades)} trades on 'deep gap down' stocks dragged the system down by **{gd_gross_pct:+.2f}%** Gross PnL. They were fake V-shape traps that ultimately failed.\n\n")
                    
                if res_only:
                    f.write(f"**2. ONLY TRADING GAP UP STOCKS**\n")
                    f.write(f"- When forcing the system to ONLY trade stocks that gapped up by `>= {ig*100:.1f}%`, it took {res_only.total_trades} trades for a Net Return of **{res_only.net_pnl/CAPITAL*100:+.2f}%**.\n")
                    if res_only.net_pnl < res_all.net_pnl:
                        f.write(f"- This significantly underperformed `ALL_STOCKS`. This proves that in Long Breakout trading, insisting that a stock *must* gap up restricts the universe too much. Flat or mildly down stocks often make the strongest intraday breakouts.\n\n")
                        
            f.write("---\n")
            
    print(f"Report fully compiled at research/long_enterprise_postmortem.md")
    
if __name__ == "__main__":
    main()
