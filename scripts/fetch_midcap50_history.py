import os
import json
import time
import pandas as pd
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from niftystocks import ns

load_dotenv()
TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip("'\" ")
HEADERS = {
    'Accept': 'application/json',
    'Authorization': f'Bearer {TOKEN}'
}

def map_midcap50_tokens():
    print("Fetching Midcap 50 symbols from niftystocks...")
    symbols = ns.get_nifty_midcap50()
    clean_symbols = [s.replace('.NS', '') for s in symbols]
    
    print("Downloading Upstox NSE_EQ instrument list...")
    url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    from io import BytesIO
    import gzip
    
    with gzip.open(BytesIO(response.content), 'rt') as f:
        df = pd.read_csv(f)
        
    mapping = {}
    sym_col = "tradingsymbol" if "tradingsymbol" in df.columns else "trading_symbol"
    key_col = "instrument_key"
    
    for index, row in df.iterrows():
        sym = str(row[sym_col])
        clean_sym = sym.replace("-EQ", "")
        if clean_sym in clean_symbols:
            mapping[clean_sym] = row[key_col]
            
    missing = [s for s in clean_symbols if s not in mapping]
    if missing:
        print(f"WARNING: Could not find instrument keys for: {missing}")
        
    return mapping

def get_1min_candles_chunk(instrument_key, start_date, end_date, attempt=1):
    url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/1minute/{end_date}/{start_date}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"Network error: {e}. Retrying in 5 seconds...")
        time.sleep(5)
        if attempt <= 3:
            return get_1min_candles_chunk(instrument_key, start_date, end_date, attempt + 1)
        return []

    if response.status_code == 200:
        data = response.json()
        if 'data' in data and data['data'] and 'candles' in data['data']:
            return data['data']['candles']
    elif response.status_code == 429:
        print("Rate limit hit! Sleeping for 30 seconds...")
        time.sleep(30)
        return get_1min_candles_chunk(instrument_key, start_date, end_date)
    else:
        print(f"Error: {response.status_code}")
    return []

def resample_to_5min(candles):
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
    df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    df = df.set_index('timestamp').sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df_5min = df.resample('5min').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum', 'oi': 'last'
    }).dropna(subset=['open', 'close'])
    return df_5min.reset_index()

def download_midcap50_history():
    if not TOKEN:
        print("ERROR: UPSTOX_ACCESS_TOKEN not found in .env")
        return

    mapping = map_midcap50_tokens()
    if not mapping:
        print("Failed to map tokens.")
        return

    os.makedirs("data/historical", exist_ok=True)

    start_date = datetime(2024, 1, 1)
    end_date = datetime.now()

    print(f"\nStarting download from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}...")

    total_stocks = len(mapping)
    for idx, (symbol, key) in enumerate(mapping.items(), 1):
        out_path = f"data/historical/{symbol}_5min.csv"
        
        # Check if already has data since Jan 2024
        if os.path.exists(out_path):
            existing_df = pd.read_csv(out_path)
            if not existing_df.empty and pd.to_datetime(existing_df['timestamp'].iloc[0]) <= datetime(2024, 1, 5):
                print(f"[{idx}/{total_stocks}] {symbol} already has data since Jan 2024. Skipping.")
                continue

        print(f"[{idx}/{total_stocks}] Downloading {symbol}...")
        
        current_start = start_date
        all_candles = []
        
        while current_start < end_date:
            current_end = current_start + timedelta(days=30)
            if current_end > end_date:
                current_end = end_date
                
            chunk = get_1min_candles_chunk(key, current_start.strftime("%Y-%m-%d"), current_end.strftime("%Y-%m-%d"))
            if chunk:
                all_candles.extend(chunk)
                
            current_start = current_end + timedelta(days=1)
            time.sleep(0.5)
            
        if all_candles:
            df_5min = resample_to_5min(all_candles)
            if os.path.exists(out_path):
                # Merge with existing
                old_df = pd.read_csv(out_path)
                old_df['timestamp'] = pd.to_datetime(old_df['timestamp'])
                df_5min = pd.concat([old_df, df_5min]).drop_duplicates(subset=['timestamp']).sort_values('timestamp')
            
            df_5min.to_csv(out_path, index=False)
            print(f"  -> Saved {len(df_5min)} candles.")
        else:
            print(f"  -> No data found.")
            
        time.sleep(1)
        
if __name__ == "__main__":
    download_midcap50_history()
