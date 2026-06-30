"""
MONTHLY UNIVERSE BREAKDOWN REPORT (CORRECTED)
==============================================
Runs the AlcoSoft Dual-Engine strategy on NIFTY 50, NIFTY Next 50, NIFTY 100
for Jan 2024 - Jun 2026, and outputs a month-by-month breakdown.

This script uses the EXACT SAME ENGINE as the verification script by importing
it directly, guaranteeing 100% matching logic and no discrepancies.
"""
import sys, os, pickle, warnings
from collections import defaultdict
import pandas as pd
import logging

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# VERY IMPORTANT: Point to the correct JSON strategy sets before importing the engine!
os.environ["STRATEGY_SETS_PATH"] = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "strategy_sets_opt.json")
)

# Import the actual, verified engine components
from research.verify_dual_engine_enterprise_opt import (
    SystemConfig, LongEngineConfig, ShortEngineConfig,
    MarketRegimeAnalyzer, IndicatorPreprocessor, SignalGenerator,
    LongEngineExecutor, ShortEngineExecutor
)

from screener.morning_screener import NIFTY_50

logging.basicConfig(level=logging.WARNING)

START_DATE = "2025-06-01"
END_DATE   = "2026-06-20"


def load_universes():
    research_dir = os.path.dirname(os.path.abspath(__file__))
    print("Loading data_cache.pkl (full 2.5-year history)...")
    with open(os.path.join(research_dir, "data_cache.pkl"), "rb") as f:
        raw = pickle.load(f)
    all_dfs = raw["stock_dfs"] if "stock_dfs" in raw else raw

    n50  = {s: df for s, df in all_dfs.items() if s in NIFTY_50}
    nx50 = {s: df for s, df in all_dfs.items() if s not in NIFTY_50}
    n100 = dict(all_dfs)
    return n50, nx50, n100


def run_engine_for_universe(stock_dfs, label):
    sys_cfg   = SystemConfig()
    long_cfg  = LongEngineConfig()
    short_cfg = ShortEngineConfig()

    IndicatorPreprocessor.enrich_data(stock_dfs)
    regime = MarketRegimeAnalyzer(stock_dfs)
    sig_gen = SignalGenerator(stock_dfs)
    sig_gen.precompute_signals(start_date=START_DATE)

    long_exec = LongEngineExecutor(
        sys_config=sys_cfg, long_config=long_cfg,
        stock_dfs=stock_dfs, regime_analyzer=regime,
        signals=sig_gen.long_signals
    )
    long_trades = long_exec.execute(start_date=START_DATE, end_date=END_DATE)

    short_exec = ShortEngineExecutor(
        sys_config=sys_cfg, short_config=short_cfg,
        stock_dfs=stock_dfs, regime_analyzer=regime,
        signals=sig_gen.short_signals
    )
    short_trades = short_exec.execute(start_date=START_DATE, end_date=END_DATE)

    return long_trades, short_trades, sys_cfg.capital


def compute_monthly(long_trades, short_trades, capital):
    monthly = defaultdict(lambda: {"trades": 0, "wins": 0, "long": 0, "short": 0, "gross": 0.0, "stt": 0.0, "net": 0.0})

    for t in long_trades:
        key = t.entry_time.strftime("%Y-%m")
        monthly[key]["trades"] += 1
        monthly[key]["long"]   += 1
        if t.pnl_gross > 0: monthly[key]["wins"] += 1
        monthly[key]["gross"] += (t.pnl_gross / capital) * 100
        monthly[key]["stt"]   += (t.stt_tax   / capital) * 100
        monthly[key]["net"]   += (t.pnl_net   / capital) * 100

    for t in short_trades:
        key = t.entry_time.strftime("%Y-%m")
        monthly[key]["trades"] += 1
        monthly[key]["short"]  += 1
        if t.pnl_gross > 0: monthly[key]["wins"] += 1
        monthly[key]["gross"] += (t.pnl_gross / capital) * 100
        monthly[key]["stt"]   += (t.stt_tax   / capital) * 100
        monthly[key]["net"]   += (t.pnl_net   / capital) * 100

    return dict(monthly)


def print_table(label, monthly_data, all_months):
    print(f"\n{'='*80}")
    print(f"  {label}")
    print(f"{'='*80}")
    print(f"  {'Month':<10} {'Trades':>6} {'Long':>5} {'Short':>5} {'Win%':>6} {'Gross%':>8} {'STT%':>6} {'Net%':>8} {'Cumulative':>11}")
    print(f"  {'-'*78}")

    cumul = 0.0
    g_trades = g_wins = g_long = g_short = 0
    g_gross = g_stt = g_net = 0.0

    for m in all_months:
        d = monthly_data.get(m)
        if not d or d["trades"] == 0:
            print(f"  {m:<10} {'--':>6} {'--':>5} {'--':>5} {'--':>6} {'--':>8} {'--':>6} {'--':>8} {'--':>11}")
            continue

        cumul += d["net"]
        g_trades += d["trades"]; g_wins += d["wins"]; g_long += d["long"]; g_short += d["short"]
        g_gross += d["gross"]; g_stt += d["stt"]; g_net += d["net"]
        wr = (d["wins"] / d["trades"]) * 100

        print(f"  {m:<10} {d['trades']:>6} {d['long']:>5} {d['short']:>5} {wr:>5.1f}% {d['gross']:>+7.2f}% {d['stt']:>5.2f}% {d['net']:>+7.2f}% {cumul:>+10.2f}%")

    print(f"  {'-'*78}")
    g_wr = (g_wins / g_trades * 100) if g_trades > 0 else 0
    print(f"  {'TOTAL':<10} {g_trades:>6} {g_long:>5} {g_short:>5} {g_wr:>5.1f}% {g_gross:>+7.2f}% {g_stt:>5.2f}% {g_net:>+7.2f}%")
    print(f"  {'='*80}")
    return g_net


def main():
    n50, nx50, n100 = load_universes()
    all_months = pd.date_range(START_DATE, END_DATE, freq="MS").strftime("%Y-%m").tolist()

    print("\nRUNNING NIFTY 50...")
    l50, s50, cap = run_engine_for_universe(n50, "NIFTY 50")
    m50 = compute_monthly(l50, s50, cap)
    
    print("\nRUNNING NIFTY NEXT 50...")
    lnx, snx, _ = run_engine_for_universe(nx50, "NIFTY NEXT 50")
    mnx = compute_monthly(lnx, snx, cap)
    
    print("\nRUNNING NIFTY 100...")
    l100, s100, _ = run_engine_for_universe(n100, "NIFTY 100")
    m100 = compute_monthly(l100, s100, cap)

    print("\n\n" + "#"*80)
    print("  CORRECTED MONTHLY BACKTEST (JAN 2024 - JUN 2026)")
    print("#"*80)
    net50  = print_table("NIFTY 50", m50, all_months)
    netnx  = print_table("NIFTY NEXT 50", mnx, all_months)
    net100 = print_table("NIFTY 100", m100, all_months)

    print(f"\nFINAL COMPARISON")
    print(f"  NIFTY 50:      {net50:+.2f}%")
    print(f"  NIFTY NEXT 50: {netnx:+.2f}%")
    print(f"  NIFTY 100:     {net100:+.2f}%")

if __name__ == "__main__":
    main()
