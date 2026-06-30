import os
import sys
import pickle
import logging
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.verify_dual_engine_enterprise import (
    SystemConfig, LongEngineConfig, ShortEngineConfig,
    MarketRegimeAnalyzer, IndicatorPreprocessor, SignalGenerator,
    LongEngineExecutor, ShortEngineExecutor, ReportingEngine, TradeRecord
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def run_universe_benchmark(universe_name: str, stock_dfs: dict):
    logging.info(f"--- Running Benchmark for {universe_name} ---")
    
    sys_config = SystemConfig()
    long_config = LongEngineConfig()
    short_config = ShortEngineConfig()

    IndicatorPreprocessor.enrich_data(stock_dfs)
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
    
    signal_gen = SignalGenerator(stock_dfs)
    signal_gen.precompute_signals()
    
    long_executor = LongEngineExecutor(
        sys_config=sys_config,
        long_config=long_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        signals=signal_gen.long_signals
    )
    long_trades = long_executor.execute()
    
    short_executor = ShortEngineExecutor(
        sys_config=sys_config,
        short_config=short_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        signals=signal_gen.short_signals
    )
    short_trades = short_executor.execute()

    return long_trades, short_trades

def extract_metrics(trades: List[TradeRecord], capital: float):
    metrics = ReportingEngine._calculate_metrics(trades, capital)
    if not metrics:
        return {"Total Trades": 0, "Win Rate": "0.00%", "Net Return": "0.00%"}
    return metrics

def print_summary(name, long_metrics, short_metrics, combined_metrics):
    print(f"\n=======================================================")
    print(f" {name} UNIVERSE BENCHMARK RESULTS")
    print(f"=======================================================")
    print(f"Combined Total Trades : {combined_metrics.get('Total Trades', 0)}")
    print(f"Combined Net Return   : {combined_metrics.get('Net Return', '0.00%')}")
    print(f"-------------------------------------------------------")
    print(f"Long Engine Trades    : {long_metrics.get('Total Trades', 0)}")
    print(f"Long Win Rate         : {long_metrics.get('Win Rate', '0.00%')}")
    print(f"Long Net Return       : {long_metrics.get('Net Return', '0.00%')}")
    print(f"-------------------------------------------------------")
    print(f"Short Engine Trades   : {short_metrics.get('Total Trades', 0)}")
    print(f"Short Win Rate        : {short_metrics.get('Win Rate', '0.00%')}")
    print(f"Short Net Return      : {short_metrics.get('Net Return', '0.00%')}")
    print(f"=======================================================\n")

if __name__ == "__main__":
    nifty50_cache = "c:/Extra Programs/Files/AlcoSoft_Financial_Services/research/nifty50_data_cache.pkl"
    next50_cache = "c:/Extra Programs/Files/AlcoSoft_Financial_Services/research/next50_data_cache.pkl"
    
    with open(nifty50_cache, "rb") as f:
        nifty50_dfs = pickle.load(f)
        
    with open(next50_cache, "rb") as f:
        next50_dfs = pickle.load(f)
        
    combined_dfs = {**nifty50_dfs, **next50_dfs}
    
    capital = SystemConfig().capital
    
    # 1. NIFTY 50 Alone
    l_trades_50, s_trades_50 = run_universe_benchmark("NIFTY 50", nifty50_dfs.copy())
    l_met_50 = extract_metrics(l_trades_50, capital)
    s_met_50 = extract_metrics(s_trades_50, capital)
    c_met_50 = extract_metrics(l_trades_50 + s_trades_50, capital)
    
    # 2. NIFTY NEXT 50 Alone
    l_trades_next, s_trades_next = run_universe_benchmark("NIFTY NEXT 50", next50_dfs.copy())
    l_met_n = extract_metrics(l_trades_next, capital)
    s_met_n = extract_metrics(s_trades_next, capital)
    c_met_n = extract_metrics(l_trades_next + s_trades_next, capital)
    
    # 3. NIFTY 100 Combined
    l_trades_100, s_trades_100 = run_universe_benchmark("NIFTY 100", combined_dfs.copy())
    l_met_c = extract_metrics(l_trades_100, capital)
    s_met_c = extract_metrics(s_trades_100, capital)
    c_met_c = extract_metrics(l_trades_100 + s_trades_100, capital)

    print_summary("NIFTY 50", l_met_50, s_met_50, c_met_50)
    print_summary("NIFTY NEXT 50", l_met_n, s_met_n, c_met_n)
    print_summary("NIFTY 100 (COMBINED)", l_met_c, s_met_c, c_met_c)
