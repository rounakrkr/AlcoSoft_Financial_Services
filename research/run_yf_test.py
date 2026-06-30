import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.run_yfinance_comparison import build_yfinance_cache, CACHE_PATH

# 1. Build yfinance cache
if not os.path.exists(CACHE_PATH):
    print("Downloading yfinance data...")
    build_yfinance_cache()

# 2. Monkeypatch verify_dual_engine_enterprise
import research.verify_dual_engine_enterprise as vde

def mock_load_cache():
    with open(CACHE_PATH, "rb") as f:
        data = pickle.load(f)
    from screener.morning_screener import NIFTY_50
    stock_dfs = {sym: df for sym, df in data["stock_dfs"].items() if sym in NIFTY_50}
    return stock_dfs

vde.load_cache = mock_load_cache
vde.REPORT_PATH = os.path.join(os.path.dirname(__file__), "yfinance_analytics_report.md")

sys.argv = ["verify_dual_engine_enterprise.py", "--start-date", "2026-03-20"]

print("Running Dual Engine Verifier on YFINANCE Data...")
vde.main()
print("Done!")
