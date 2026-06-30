import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pickle, time
import pandas as pd
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from niftystocks import ns
from screener.morning_screener import _fetch_yahoo_history
from core.strategy import _build_indicators

CACHE_PATH = os.path.join(os.path.dirname(__file__), "midcap_data_cache.pkl")

def get_midcap_symbols():
    try:
        symbols = ns.get_nifty_midcap100()
        # niftystocks returns raw symbols, we need .NS for Yahoo Finance
        ns_symbols = [s if s.endswith(".NS") else f"{s}.NS" for s in symbols]
        return ns_symbols
    except Exception as e:
        print(f"Error fetching midcap symbols: {e}")
        return []

def build_cache():
    MIDCAP_100 = get_midcap_symbols()
    if not MIDCAP_100:
        print("Failed to get symbols. Exiting.")
        return
        
    print(f"Building data cache for {len(MIDCAP_100)} Midcap stocks...")
    stock_dfs = {}
    failed = []
    for i, sym in enumerate(MIDCAP_100):
        print(f"  Fetching {i+1}/{len(MIDCAP_100)}: {sym}    ", end="\r")
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
            time.sleep(0.05)  # gentle on the API
        except Exception as e:
            failed.append(sym)

    print(f"\nLoaded: {len(stock_dfs)} stocks | Failed: {failed}")

    with open(CACHE_PATH, "wb") as f:
        pickle.dump({"stock_dfs": stock_dfs, "built_at": pd.Timestamp.now()}, f)

    print(f"Cache saved -> {CACHE_PATH}")
    return stock_dfs

def load_cache():
    if not os.path.exists(CACHE_PATH):
        print("No cache found. Building...")
        return build_cache()
    with open(CACHE_PATH, "rb") as f:
        data = pickle.load(f)
    age_hrs = (pd.Timestamp.now() - data["built_at"]).total_seconds() / 3600
    print(f"Cache loaded: {len(data['stock_dfs'])} stocks | Age: {age_hrs:.1f} hrs")
    if age_hrs > 20:
        print("Cache is old (>20h). Consider rebuilding with: python research/build_midcap_cache.py")
    return data["stock_dfs"]

if __name__ == "__main__":
    build_cache()
