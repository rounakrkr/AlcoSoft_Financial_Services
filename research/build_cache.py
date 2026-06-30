"""
DATA CACHE BUILDER (PREMIUM LOCAL EDITION)
==========================================
Parse all downloaded 5-minute CSVs from data/historical.
"""
import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pickle, time
import pandas as pd
import logging, warnings
warnings.filterwarnings("ignore")

from core.strategy import _build_indicators

CACHE_PATH = os.path.join(os.path.dirname(__file__), "data_cache.pkl")
LOCAL_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "historical")

def build_cache():
    print("Building data cache from PREMIUM LOCAL CSVs...")
    stock_dfs = {}
    failed = []
    
    csv_files = glob.glob(os.path.join(LOCAL_DATA_DIR, "*_5min.csv"))
    if not csv_files:
        print(f"ERROR: No CSV files found in {LOCAL_DATA_DIR}!")
        return {}
        
    for i, file_path in enumerate(csv_files):
        sym = os.path.basename(file_path).replace("_5min.csv", "")
        print(f"  Parsing {i+1}/{len(csv_files)}: {sym}    ", end="\r")
        try:
            df = pd.read_csv(file_path)
            # Ensure proper datetime index
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)
            df.columns = [c.lower() for c in df.columns]
            
            df.dropna(subset=["close"], inplace=True)
            df["bucket"] = df.index
            
            # Precompute indicators
            df = _build_indicators(df)
            df.dropna(subset=["ema21", "vwap"], inplace=True)
            
            if len(df) >= 20:
                stock_dfs[sym] = df
            else:
                failed.append(sym)
        except Exception as e:
            print(f"\nError processing {sym}: {e}")
            failed.append(sym)

    print(f"\nLoaded: {len(stock_dfs)} stocks | Failed: {failed}")

    with open(CACHE_PATH, "wb") as f:
        pickle.dump({"stock_dfs": stock_dfs, "built_at": pd.Timestamp.now()}, f)

    print(f"Premium Cache saved -> {CACHE_PATH}")
    return stock_dfs

def load_cache():
    if not os.path.exists(CACHE_PATH):
        print("No cache found. Building from CSVs...")
        return build_cache()
    with open(CACHE_PATH, "rb") as f:
        data = pickle.load(f)
    age_hrs = (pd.Timestamp.now() - data["built_at"]).total_seconds() / 3600
    print(f"Premium Cache loaded: {len(data['stock_dfs'])} stocks | Age: {age_hrs:.1f} hrs")
    # No expiration warning for static historical data
    return data["stock_dfs"]

if __name__ == "__main__":
    build_cache()
