import os
import sys
import pickle
import logging
import pandas as pd
from datetime import timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "HCLTECH", "AXISBANK", "ASIANPAINT", "MARUTI",
    "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO", "NESTLEIND",
    "TECHM", "POWERGRID", "NTPC", "ONGC", "BAJFINANCE",
    "BAJAJFINSV", "ADANIENT", "ADANIPORTS", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "GRASIM", "HEROMOTOCO", "HINDALCO", "INDUSINDBK",
    "JSWSTEEL", "M&M", "SBILIFE", "TATACONSUM", "TATAMOTORS",
    "TATASTEEL", "BRITANNIA", "CIPLA", "COALINDIA", "HDFCLIFE",
    "LTIM", "BPCL", "UPL", "APOLLOHOSP", "BAJAJ-AUTO",
]

NIFTY_NEXT_50 = [
    "ABB", "ADANIENSOL", "ADANIGREEN", "AMBUJACEM", "DMART", 
    "BAJAJHLDNG", "BANKBARODA", "BEL", "BHEL", "BOSCHLTD", 
    "CANBK", "CHOLAFIN", "COLPAL", "DLF", "GAIL", "GODREJCP", 
    "HAL", "HAVELLS", "ICICIGI", "ICICIPRULI", "IOC", "IRCTC", 
    "IRFC", "JINDALSTEL", "JIOFIN", "KALYANKJIL", "LICI", "LODHA", 
    "MARICO", "MUTHOOTFIN", "NAUKRI", "NHPC", "PIDILITIND", "PFC", 
    "PNB", "RECLTD", "SBICARD", "SIEMENS", "SRF", "TORNTPHARM", 
    "TRENT", "TVSMOTOR", "UNITEDSPR", "VEDL", "ZOMATO", "TATACOMM", 
    "CGPOWER", "MAXHEALTH", "POONAWALLA", "SHRIRAMFIN"
]

def load_archive_data(symbols, cache_file):
    stock_dfs = {}
    archive_dir = "c:/Extra Programs/Files/AlcoSoft_Financial_Services/archive"
    
    global_max_date = pd.Timestamp("2000-01-01")
    
    # First pass: find the max date across all available symbols to align the 90 days
    valid_dfs = {}
    for sym in symbols:
        filepath = os.path.join(archive_dir, f"{sym}_5minute.csv")
        if not os.path.exists(filepath):
            logging.warning(f"File not found for {sym}")
            continue
            
        try:
            df = pd.read_csv(filepath)
            if df.empty:
                continue
                
            # Rename columns if necessary. The files might have 'date', 'open', 'high', 'low', 'close', 'volume'
            # Let's standardize them
            df.columns = [c.lower() for c in df.columns]
            if "date" in df.columns:
                df["datetime"] = pd.to_datetime(df["date"])
                df = df.set_index("datetime")
            elif "datetime" in df.columns:
                df["datetime"] = pd.to_datetime(df["datetime"])
                df = df.set_index("datetime")
            else:
                # Assuming first column is datetime
                df.index = pd.to_datetime(df.iloc[:, 0])
                
            # Drop timezone if any
            if df.index.tz is not None:
                df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
                
            df = df.sort_index()
            
            # Find the max date
            max_dt = df.index.max()
            if max_dt > global_max_date:
                global_max_date = max_dt
                
            valid_dfs[sym] = df
        except Exception as e:
            logging.error(f"Error reading {sym}: {e}")
            
    for sym, df in valid_dfs.items():
        # Calculate cutoff: 90 days before this symbol's max date
        cutoff_date = df.index.max() - timedelta(days=90)
        
        # Slice the dataframe to only include the last 90 days
        sliced_df = df[df.index >= cutoff_date]
        
        # Ensure we have the required columns
        req_cols = ["open", "high", "low", "close", "volume"]
        missing = [c for c in req_cols if c not in sliced_df.columns]
        if missing:
            logging.warning(f"{sym} missing columns {missing}")
            continue
            
        sliced_df = sliced_df[req_cols]
        stock_dfs[sym] = sliced_df
        logging.info(f"Processed {sym}: {len(sliced_df)} rows")
        
    with open(cache_file, "wb") as f:
        pickle.dump(stock_dfs, f)
    logging.info(f"Saved {len(stock_dfs)} symbols to {cache_file}")

if __name__ == "__main__":
    logging.info("Building NIFTY 50 archive data cache...")
    load_archive_data(NIFTY_50, "c:/Extra Programs/Files/AlcoSoft_Financial_Services/research/nifty50_data_cache.pkl")
    logging.info("Building NIFTY NEXT 50 archive data cache...")
    load_archive_data(NIFTY_NEXT_50, "c:/Extra Programs/Files/AlcoSoft_Financial_Services/research/next50_data_cache.pkl")
