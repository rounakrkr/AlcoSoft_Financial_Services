import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pickle, time
import pandas as pd
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from niftystocks import ns
from core.strategy import _build_indicators

CACHE_PATH = os.path.join(os.path.dirname(__file__), "midcap50_historical_cache.pkl")
HISTORICAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "historical")

def get_midcap50_symbols():
    try:
        symbols = ns.get_nifty_midcap50()
        ns_symbols = [s if s.endswith(".NS") else f"{s}.NS" for s in symbols]
        return ns_symbols
    except Exception as e:
        print(f"Error fetching midcap50 symbols: {e}")
        return []

def build_cache():
    MIDCAP_50 = get_midcap50_symbols()
    if not MIDCAP_50:
        print("Failed to get symbols. Exiting.")
        return
        
    print(f"Building historical data cache for {len(MIDCAP_50)} Midcap 50 stocks from Jan 2024...")
    stock_dfs = {}
    failed = []
    
    start_date = pd.Timestamp("2024-01-01")
    
    for i, sym in enumerate(MIDCAP_50):
        print(f"  Processing {i+1}/{len(MIDCAP_50)}: {sym}    ", end="\r")
        base_sym = sym.replace(".NS", "")
        csv_path = os.path.join(HISTORICAL_DIR, f"{base_sym}_5min.csv")
        
        if not os.path.exists(csv_path):
            # Try without _5min
            csv_path = os.path.join(HISTORICAL_DIR, f"{base_sym}_5minute.csv")
            if not os.path.exists(csv_path):
                failed.append(sym)
                continue
                
        try:
            df = pd.read_csv(csv_path)
            # Upstox CSV format: timestamp, open, high, low, close, volume, oi
            if 'date' in df.columns:
                df.rename(columns={'date': 'timestamp'}, inplace=True)
            
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df = df.set_index('timestamp')
            df = df.sort_index()
            
            # Slice from Jan 2024
            df = df[df.index >= start_date]
            
            if df.empty:
                failed.append(sym)
                continue
                
            df.columns = [c.lower() for c in df.columns]
            df.dropna(subset=["close"], inplace=True)
            df["bucket"] = df.index
            
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

    print(f"Historical cache saved -> {CACHE_PATH}")
    return stock_dfs

if __name__ == "__main__":
    build_cache()
