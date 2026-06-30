"""
Script to download 2 years of daily data for NIFTY Midcap 50 and Smallcap 50.
Uses yfinance and saves the data to a separate cache file (mid_small_cache.pkl).
DOES NOT touch the existing NIFTY 100 cache.
"""

import os
import sys
import yfinance as yf
import pandas as pd
import pickle
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("MidSmallDownloader")

CACHE_PATH = os.path.join(os.path.dirname(__file__), "mid_small_cache.pkl")

# NIFTY MIDCAP 50
MIDCAP_50 = [
    "ABFRL", "ASTRAL", "AUBANK", "AUROPHARMA", "BALKRISIND", "BANDHANBNK", 
    "BANKINDIA", "BATAINDIA", "BHARATFORG", "COFORGE", "CONCOR", "CUMMINSIND", 
    "DIXON", "ESCORTS", "FEDERALBNK", "GODREJPROP", "GUJGASLTD", "HINDPETRO", 
    "IDFCFIRSTB", "INDHOTEL", "INDUSTOWER", "JINDALSTEL", "JUBLFOOD", "L&TFH", 
    "LAURUSLABS", "LICHSGFIN", "LUPIN", "M&MFIN", "MAXHEALTH", "MPHASIS", "MRF", 
    "MUTHOOTFIN", "NMDC", "OBEROIRLTY", "PAGEIND", "PERSISTENT", "PETRONET", 
    "PIIND", "POLYCAB", "PVRINOX", "SAIL", "SHRIRAMFIN", "SIEMENS", "TRENT", 
    "TVSMOTOR", "UBL", "IDEA", "VOLTAS", "ZEEL", "ZYDUSLIFE"
]

# NIFTY SMALLCAP 50
SMALLCAP_50 = [
    "ALOKINDS", "AMBER", "ANGELONE", "APOLLOTYRE", "BSE", "CASTROLIND", "CDSL", 
    "CENTRALBK", "CHAMBLFERT", "CAMS", "CROMPTON", "CYIENT", "EIDPARRY", 
    "EQUITASBNK", "EXIDEIND", "GLENMARK", "GRANULES", "HAPPSTMNDS", "HINDCOPPER", 
    "IDBI", "INDIAMART", "INDIANB", "IEX", "IOB", "JBCHEPHARM", "KARURVYSYA", 
    "KEI", "LATENTVIEW", "MCX", "METROPOLIS", "MRPL", "NATIONALUM", "NBCC", 
    "NETWORK18", "POONAWALLA", "PRAJIND", "RBLBANK", "REDINGTON", "ROUTE", 
    "SUZLON", "SWANENERGY", "SYNGENE", "TEJASNET", "TITAGARH", "UCOBANK", 
    "UTIAMC", "VIPIND", "WELCORP", "WELSPUNLIV", "ZENSARTECH"
]

ALL_SYMBOLS = MIDCAP_50 + SMALLCAP_50

def download_data():
    logger.info(f"Downloading 2 years of daily data for {len(ALL_SYMBOLS)} Mid & Small cap stocks...")
    
    stock_dfs = {}
    failed = []
    
    for i, sym in enumerate(ALL_SYMBOLS):
        ns_sym = f"{sym}.NS"
        try:
            logger.info(f"[{i+1}/{len(ALL_SYMBOLS)}] Fetching {sym}...")
            # Download exactly 2 years of daily data
            ticker = yf.Ticker(ns_sym)
            df = ticker.history(period="2y", interval="1d")
            
            if df.empty:
                logger.warning(f"  No data found for {sym}")
                failed.append(sym)
                continue
                
            # Clean up dataframe to match expected format (lowercase columns, 'timestamp' index)
            df.index.name = 'timestamp'
            df.columns = [c.lower() for c in df.columns]
            
            # Keep only standard OHLCV
            df = df[['open', 'high', 'low', 'close', 'volume']]
            df.dropna(subset=['close'], inplace=True)
            
            if len(df) > 50:
                stock_dfs[sym] = df
            else:
                logger.warning(f"  Insufficient data for {sym} ({len(df)} rows)")
                failed.append(sym)
                
            # Be nice to Yahoo Finance API
            time.sleep(0.5)
            
        except Exception as e:
            logger.error(f"  Error fetching {sym}: {e}")
            failed.append(sym)
            
    logger.info(f"\nDownload Complete! Loaded: {len(stock_dfs)} stocks | Failed: {len(failed)}")
    if failed:
        logger.info(f"Failed symbols: {failed}")
        
    with open(CACHE_PATH, "wb") as f:
        pickle.dump({"stock_dfs": stock_dfs, "built_at": pd.Timestamp.now()}, f)
        
    logger.info(f"Data saved to -> {CACHE_PATH}")

if __name__ == "__main__":
    download_data()
