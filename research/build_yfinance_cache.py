import os
import sys
import pickle
import logging
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener.morning_screener import _fetch_yahoo_history

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

def fetch_data(symbols, cache_file):
    stock_dfs = {}
    
    for sym in symbols:
        try:
            # We use 60d since yfinance chart API limits 5m interval to 60 days max.
            df = _fetch_yahoo_history(f"{sym}.NS", period="60d", interval="5m")
            if df is not None and not df.empty:
                df = df.rename(columns={
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume"
                })
                # Remove timezone
                df.index = pd.to_datetime(df.index).tz_convert("Asia/Kolkata").tz_localize(None)
                stock_dfs[sym] = df
                logging.info(f"Fetched {sym}: {len(df)} rows")
            else:
                logging.warning(f"No data for {sym}")
        except Exception as e:
            logging.error(f"Error fetching {sym}: {e}")
            
    with open(cache_file, "wb") as f:
        pickle.dump(stock_dfs, f)
    logging.info(f"Saved {len(stock_dfs)} symbols to {cache_file}")

if __name__ == "__main__":
    logging.info("Fetching NIFTY 50 data...")
    fetch_data(NIFTY_50, "c:/Extra Programs/Files/AlcoSoft_Financial_Services/research/nifty50_data_cache.pkl")
    logging.info("Fetching NIFTY NEXT 50 data...")
    fetch_data(NIFTY_NEXT_50, "c:/Extra Programs/Files/AlcoSoft_Financial_Services/research/next50_data_cache.pkl")
