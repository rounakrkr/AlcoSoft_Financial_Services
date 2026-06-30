import sys, os, pickle, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from screener.morning_screener import NIFTY_50, _fetch_yahoo_history
from core.strategy import _build_indicators

CACHE_PATH = os.path.join(os.path.dirname(__file__), "yfinance_cache.pkl")

def build_yfinance_cache():
    print(f"Building YFINANCE data cache for {len(NIFTY_50)} stocks...")
    stock_dfs = {}
    failed = []
    for i, sym in enumerate(NIFTY_50):
        print(f"  Fetching {i+1}/{len(NIFTY_50)}: {sym}    ", end="\r")
        try:
            df = _fetch_yahoo_history(sym, period="60d", interval="5m")
            if df is None or df.empty:
                failed.append(sym); continue
            df.columns = [c.lower() for c in df.columns]
            df.dropna(subset=["close"], inplace=True)
            df["bucket"] = df.index
            df = _build_indicators(df)
            df.dropna(subset=["ema21", "vwap"], inplace=True)
            if len(df) >= 20:
                stock_dfs[sym] = df
            else:
                failed.append(sym)
            time.sleep(0.05)
        except Exception as e:
            failed.append(sym)

    print(f"\nLoaded: {len(stock_dfs)} stocks | Failed: {failed}")

    with open(CACHE_PATH, "wb") as f:
        pickle.dump({"stock_dfs": stock_dfs, "built_at": pd.Timestamp.now()}, f)

    print(f"Yfinance Cache saved -> {CACHE_PATH}")

if __name__ == "__main__":
    if not os.path.exists(CACHE_PATH):
        build_yfinance_cache()
    
    # We will just run a terminal command instead of monkeypatching python code, it's safer.
