"""
Script to download 2 years of daily data for NIFTY Midcap 50 and Smallcap 50.
Uses yfinance bulk download to avoid rate limits, and saves to mid_small_cache.pkl.
"""

import os
import sys
import yfinance as yf
import pandas as pd
import pickle
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("MidSmallBulkDownloader")

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
    
    # Prepare ticker list
    tickers = " ".join([f"{sym}.NS" for sym in ALL_SYMBOLS])
    
    try:
        # Bulk download
        logger.info("Executing yfinance bulk download (this prevents rate limits)...")
        data = yf.download(tickers, period="2y", interval="1d", group_by="ticker", auto_adjust=False)
        
        stock_dfs = {}
        failed = []
        
        # Parse multi-index dataframe
        for sym in ALL_SYMBOLS:
            ns_sym = f"{sym}.NS"
            try:
                # yfinance returns a multi-level column DataFrame if multiple tickers are downloaded
                if ns_sym in data:
                    df = data[ns_sym].copy()
                else:
                    logger.warning(f"  Missing {sym} in bulk data.")
                    failed.append(sym)
                    continue
                
                if df.empty:
                    logger.warning(f"  Empty data for {sym}.")
                    failed.append(sym)
                    continue
                
                df.index.name = 'timestamp'
                df.columns = [c.lower() for c in df.columns]
                
                # Keep standard columns and drop NaNs
                # Bulk download might have 'adj close', we'll just keep standard OHLCV
                df = df[['open', 'high', 'low', 'close', 'volume']]
                df.dropna(subset=['close'], inplace=True)
                
                if len(df) > 50:
                    stock_dfs[sym] = df
                else:
                    logger.warning(f"  Insufficient data for {sym} ({len(df)} rows)")
                    failed.append(sym)
                    
            except Exception as e:
                logger.error(f"  Error parsing {sym}: {e}")
                failed.append(sym)
                
        logger.info(f"\nDownload Complete! Loaded: {len(stock_dfs)} stocks | Failed: {len(failed)}")
        if failed:
            logger.info(f"Failed symbols: {failed}")
            
        with open(CACHE_PATH, "wb") as f:
            pickle.dump({"stock_dfs": stock_dfs, "built_at": pd.Timestamp.now()}, f)
            
        logger.info(f"Data safely stored in -> {CACHE_PATH}")

    except Exception as e:
        logger.error(f"Critical error during bulk download: {e}")

if __name__ == "__main__":
    download_data()
