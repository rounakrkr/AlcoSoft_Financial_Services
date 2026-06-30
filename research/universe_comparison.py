"""
UNIVERSE COMPARISON SCRIPT
===========================
Runs the AlcoSoft Dual-Engine strategy on 3 universes:
  1. NIFTY 50
  2. NIFTY Next 50
  3. NIFTY 100 (combined)
and produces a side-by-side report.

NO LOOKAHEAD BIAS: Uses current candle close as entry price.
Live system files in core/ are NOT touched.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["STRATEGY_SETS_PATH"] = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "strategy_sets_opt.json")
)

import pickle
import logging
import warnings
warnings.filterwarnings("ignore")

import pandas as pd

# Import the entire engine from the opt verifier (reuse all classes)
from research.verify_dual_engine_enterprise_opt import (
    SystemConfig, LongEngineConfig, ShortEngineConfig,
    MarketRegimeAnalyzer, IndicatorPreprocessor, SignalGenerator,
    LongEngineExecutor, ShortEngineExecutor, ReportingEngine
)

logging.basicConfig(level=logging.WARNING)  # Suppress verbose logs for clean output

START_DATE = "2026-01-01"
END_DATE   = "2026-06-20"

NIFTY_50 = [
    'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','ITC','SBIN',
    'BHARTIARTL','KOTAKBANK','LT','HCLTECH','AXISBANK','ASIANPAINT','MARUTI',
    'SUNPHARMA','BAJFINANCE','TITAN','NESTLEIND','WIPRO','POWERGRID','ULTRACEMCO',
    'BAJAJFINSV','NTPC','TECHM','TATASTEEL','HDFCLIFE','JSWSTEEL','COALINDIA',
    'ONGC','M&M','INDUSINDBK','CIPLA','DRREDDY','BRITANNIA','GRASIM','DIVISLAB',
    'APOLLOHOSP','HINDALCO','BPCL','TATACONSUM','EICHERMOT','ADANIENT','ADANIPORTS',
    'BAJAJ-AUTO','HEROMOTOCO','TATAMOTORS','SBILIFE','UPL','ADANIPOWER'
]


def load_pkl(path: str) -> dict:
    with open(path, "rb") as f:
        data = pickle.load(f)
    if isinstance(data, dict) and "stock_dfs" in data:
        return data["stock_dfs"]
    return data


def run_engine(stock_dfs: dict, universe_name: str) -> dict:
    """Runs full Dual-Engine backtest on given stock universe. Returns metrics dict."""
    print(f"\n{'='*60}")
    print(f"  Running: {universe_name} ({len(stock_dfs)} stocks)")
    print(f"{'='*60}")

    sys_config   = SystemConfig()
    long_config  = LongEngineConfig()
    short_config = ShortEngineConfig()

    # Enrich indicators
    IndicatorPreprocessor.enrich_data(stock_dfs)

    # Regime
    regime = MarketRegimeAnalyzer(stock_dfs)

    # Signals
    sig_gen = SignalGenerator(stock_dfs)
    sig_gen.precompute_signals(start_date=START_DATE)

    # Long Engine
    long_exec = LongEngineExecutor(
        sys_config=sys_config,
        long_config=long_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime,
        signals=sig_gen.long_signals
    )
    long_trades = long_exec.execute(start_date=START_DATE, end_date=END_DATE)

    # Short Engine
    short_exec = ShortEngineExecutor(
        sys_config=sys_config,
        short_config=short_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime,
        signals=sig_gen.short_signals
    )
    short_trades = short_exec.execute(start_date=START_DATE, end_date=END_DATE)

    # Compute metrics inline (same logic as ReportingEngine)
    capital = sys_config.capital
    stt_pct = sys_config.stt_percentage

    def compute_metrics(trades, direction):
        if not trades:
            return {"trades": 0, "wins": 0, "gross": 0.0, "stt": 0.0, "net": 0.0, "pf": 0.0}
        wins  = sum(1 for t in trades if t.pnl_gross > 0)
        gross = sum(t.pnl_gross for t in trades)
        stt   = sum(t.stt_tax for t in trades)
        net   = sum(t.pnl_net for t in trades)
        losses_sum = abs(sum(t.pnl_gross for t in trades if t.pnl_gross < 0))
        gains_sum  = sum(t.pnl_gross for t in trades if t.pnl_gross > 0)
        pf = (gains_sum / losses_sum) if losses_sum > 0 else float('inf')
        return {
            "trades": len(trades),
            "wins":   wins,
            "gross":  (gross / capital) * 100,
            "stt":    (stt / capital) * 100,
            "net":    (net / capital) * 100,
            "pf":     round(pf, 2)
        }

    lm = compute_metrics(long_trades,  "LONG")
    sm = compute_metrics(short_trades, "SHORT")

    # Combined
    all_trades   = long_trades + short_trades
    total_trades = len(all_trades)
    total_wins   = lm["wins"] + sm["wins"]
    gross_net    = lm["gross"] + sm["gross"]
    stt_total    = lm["stt"]   + sm["stt"]
    net_total    = lm["net"]   + sm["net"]

    total_gains  = sum(t.pnl_gross for t in all_trades if t.pnl_gross > 0)
    total_losses = abs(sum(t.pnl_gross for t in all_trades if t.pnl_gross < 0))
    combined_pf  = round(total_gains / total_losses, 2) if total_losses > 0 else float('inf')

    print(f"  [OK] Long: {lm['trades']} trades | Net: {lm['net']:.2f}%")
    print(f"  [OK] Short: {sm['trades']} trades | Net: {sm['net']:.2f}%")
    print(f"  [**] Combined Net: {net_total:.2f}%")

    return {
        "universe": universe_name,
        "stocks":   len(stock_dfs),
        "long":     lm,
        "short":    sm,
        "combined": {
            "trades":   total_trades,
            "win_rate": (total_wins / total_trades * 100) if total_trades > 0 else 0,
            "gross":    gross_net,
            "stt":      stt_total,
            "net":      net_total,
            "pf":       combined_pf
        }
    }


def print_comparison(results: list):
    print("\n")
    print("=" * 90)
    print("  ALCOSOFT DUAL-ENGINE -- UNIVERSE COMPARISON REPORT")
    print(f"  Period: {START_DATE} to {END_DATE}")
    print("=" * 90)

    # Header
    print(f"\n{'Metric':<32}", end="")
    for r in results:
        print(f"  {r['universe']:<20}", end="")
    print()
    print("-" * 90)

    def row(label, key_path, fmt=".2f", suffix="%"):
        print(f"  {label:<30}", end="")
        for r in results:
            val = r
            for k in key_path:
                val = val[k]
            print(f"  {val:{fmt}}{suffix:<17}", end="")
        print()

    print(f"\n{'-- COMBINED PORTFOLIO --':<32}")
    row("Total Trades",         ["combined","trades"],   ".0f", " ")
    row("Win Rate",             ["combined","win_rate"], ".2f", "% ")
    row("Gross Return",         ["combined","gross"],    ".2f", "% ")
    row("STT Impact",           ["combined","stt"],      ".2f", "% ")
    row("Net Return (*BEST*)",  ["combined","net"],      ".2f", "% ")
    row("Profit Factor",        ["combined","pf"],       ".2f", "  ")

    print(f"\n{'-- LONG ENGINE --':<32}")
    row("  Trades",             ["long","trades"],   ".0f", " ")
    row("  Win Rate",           ["long","wins"],     ".0f", " wins")
    row("  Gross Return",       ["long","gross"],    ".2f", "% ")
    row("  Net Return",         ["long","net"],      ".2f", "% ")
    row("  Profit Factor",      ["long","pf"],       ".2f", "  ")

    print(f"\n{'-- SHORT ENGINE --':<32}")
    row("  Trades",             ["short","trades"],  ".0f", " ")
    row("  Win Rate",           ["short","wins"],    ".0f", " wins")
    row("  Gross Return",       ["short","gross"],   ".2f", "% ")
    row("  Net Return",         ["short","net"],     ".2f", "% ")
    row("  Profit Factor",      ["short","pf"],      ".2f", "  ")

    print("\n" + "=" * 90)

    # Best universe
    best = max(results, key=lambda r: r["combined"]["net"])
    print(f"\n  WINNER: {best['universe']} with Net Return = {best['combined']['net']:.2f}%")
    print("=" * 90)


def main():
    research_dir = os.path.dirname(os.path.abspath(__file__))

    # Load the single full-data cache (1.17GB, has all 90 stocks with full 6-month history)
    print("Loading full data cache (data_cache.pkl)...")
    cache_path = os.path.join(research_dir, "data_cache.pkl")
    with open(cache_path, "rb") as f:
        raw = pickle.load(f)
    all_stock_dfs = raw["stock_dfs"] if "stock_dfs" in raw else raw
    print(f"  Total stocks in cache: {len(all_stock_dfs)}")

    # Get NIFTY 50 list
    sys.path.insert(0, os.path.dirname(research_dir))
    from screener.morning_screener import NIFTY_50

    # Split into universes using same full data
    nifty50_dfs  = {s: df for s, df in all_stock_dfs.items() if s in NIFTY_50}
    next50_dfs   = {s: df for s, df in all_stock_dfs.items() if s not in NIFTY_50}
    nifty100_dfs = dict(all_stock_dfs)  # All 90 stocks = NIFTY 100 proxy

    print(f"  NIFTY 50 stocks found:      {len(nifty50_dfs)}")
    print(f"  NIFTY Next 50 proxy stocks: {len(next50_dfs)}")
    print(f"  NIFTY 100 total:            {len(nifty100_dfs)}")

    results = []
    results.append(run_engine(dict(nifty50_dfs),  "NIFTY 50"))
    results.append(run_engine(dict(next50_dfs),   "NIFTY NEXT 50"))
    results.append(run_engine(dict(nifty100_dfs), "NIFTY 100"))

    print_comparison(results)


if __name__ == "__main__":
    main()
