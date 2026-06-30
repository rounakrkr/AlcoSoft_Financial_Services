"""
Midcap & Smallcap Cache Builder
===============================
Reads all 5-minute CSVs from data/historical_midsmall, computes all standard
indicators using core.strategy._build_indicators, and saves to mid_small_cache.pkl.
"""
import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pickle
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

from core.strategy import _build_indicators

CACHE_PATH = os.path.join(os.path.dirname(__file__), "mid_small_cache.pkl")
LOCAL_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "historical_midsmall")

def build_cache():
    print("Building Mid/Small Cap Cache from Upstox CSVs...")
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
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)
            df.columns = [c.lower() for c in df.columns]
            
            df.dropna(subset=["close"], inplace=True)
            df["bucket"] = df.index
            
            # Use original indicator builder exactly
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

    print(f"Cache saved -> {CACHE_PATH}")
    return stock_dfs

if __name__ == "__main__":
    build_cache()
