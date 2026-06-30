"""
Upstox Data Fetcher for Midcap & Smallcap 50
============================================
Downloads exactly 2 years of 1-minute historical data via Upstox API,
resamples it to 5-minute candles, and saves to data/historical_midsmall/.
"""
import os
import sys
import json
import time
import pandas as pd
import requests
import gzip
from io import BytesIO
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip("'\" ")
HEADERS = {
    'Accept': 'application/json',
    'Authorization': f'Bearer {TOKEN}'
}

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

def get_upstox_tokens():
    print("Downloading Upstox NSE_EQ instrument list...")
    url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    with gzip.open(BytesIO(response.content), 'rt') as f:
        df = pd.read_csv(f)
        
    mapping = {}
    sym_col = "tradingsymbol" if "tradingsymbol" in df.columns else "trading_symbol"
    key_col = "instrument_key"
    
    for index, row in df.iterrows():
        sym = str(row[sym_col])
        clean_sym = sym.replace("-EQ", "")
        if clean_sym in ALL_SYMBOLS:
            mapping[clean_sym] = row[key_col]
            
    print(f"Mapped {len(mapping)} / 100 instruments.")
    return mapping

def get_1min_candles_chunk(instrument_key, start_date, end_date, attempt=1):
    url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/1minute/{end_date}/{start_date}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"Network error: {e}. Retrying...")
        time.sleep(5)
        if attempt <= 3:
            return get_1min_candles_chunk(instrument_key, start_date, end_date, attempt + 1)
        return []

    if response.status_code == 200:
        data = response.json()
        if 'data' in data and data['data'] and 'candles' in data['data']:
            return data['data']['candles']
    elif response.status_code == 429:
        print("Rate limit hit! Sleeping for 60 seconds...")
        time.sleep(60)
        return get_1min_candles_chunk(instrument_key, start_date, end_date)
    return []

def resample_to_5min(candles):
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
    df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    df = df.set_index('timestamp')
    df = df.sort_index()
    df = df[~df.index.duplicated(keep='first')]
    
    df_5min = df.resample('5min').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum',
        'oi': 'last'
    }).dropna(subset=['open', 'close'])
    
    return df_5min.reset_index()

def download_history():
    if not TOKEN:
        print("ERROR: UPSTOX_ACCESS_TOKEN not found in .env.")
        return

    mapping = get_upstox_tokens()
    out_dir = "data/historical_midsmall"
    os.makedirs(out_dir, exist_ok=True)

    end_date = datetime.now()
    start_date = end_date - timedelta(days=2*365) # 2 years

    print(f"Starting 2-year Upstox download from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}...")

    total = len(mapping)
    for idx, (symbol, key) in enumerate(mapping.items(), 1):
        out_path = f"{out_dir}/{symbol}_5min.csv"
        if os.path.exists(out_path):
            print(f"[{idx}/{total}] Skipping {symbol} - Already exists.")
            continue

        print(f"[{idx}/{total}] Fetching {symbol} via Upstox...")
        current_start = start_date
        all_candles = []
        
        while current_start < end_date:
            current_end = current_start + timedelta(days=30)
            if current_end > end_date:
                current_end = end_date
                
            s_str = current_start.strftime("%Y-%m-%d")
            e_str = current_end.strftime("%Y-%m-%d")
            
            chunk = get_1min_candles_chunk(key, s_str, e_str)
            if chunk:
                all_candles.extend(chunk)
                
            current_start = current_end + timedelta(days=1)
            time.sleep(0.3) # Avoid Upstox rate limits
            
        if all_candles:
            df_5min = resample_to_5min(all_candles)
            df_5min.to_csv(out_path, index=False)
            print(f"  Saved {len(df_5min)} candles.")
        else:
            print(f"  No data found.")
            
if __name__ == "__main__":
    download_history()
