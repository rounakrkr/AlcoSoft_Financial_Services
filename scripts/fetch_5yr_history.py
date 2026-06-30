import os
import json
import time
import pandas as pd
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip("'\" ")
HEADERS = {
    'Accept': 'application/json',
    'Authorization': f'Bearer {TOKEN}'
}
# V3 Endpoint format for historical intraday candles
# https://api.upstox.com/v3/historical-candle/intraday/{instrumentKey}/1minute/{to_date}/{from_date}
# Wait, V3 intraday candle URL is /v3/historical-candle/intraday/{instrumentKey}/{interval}/{to_date}/{from_date}
# Wait, let's use the standard Upstox API v2 endpoint for historical data which is widely supported if V3 is complex.
# V2 endpoint: /v2/historical-candle/intraday/{instrumentKey}/1minute/{to_date}/{from_date} -- actually V2 intraday is only for today.
# We will use the proper historical candle endpoint.
# GET /v2/historical-candle/{instrumentKey}/{interval}/{to_date}/{from_date}
# Interval can be 1minute, 30minute, day, week, month. Wait, V2 doesn't have 5minute.
# Ah, if we need 5-minute, we must fetch 1-minute and resample! Because Upstox provides 1minute.

def get_1min_candles_chunk(instrument_key, start_date, end_date, attempt=1):
    url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/1minute/{end_date}/{start_date}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"Network error: {e}. Retrying in 5 seconds (Attempt {attempt})...")
        time.sleep(5)
        if attempt <= 3:
            return get_1min_candles_chunk(instrument_key, start_date, end_date, attempt + 1)
        return []

    if response.status_code == 200:
        data = response.json()
        if 'data' in data and data['data'] and 'candles' in data['data']:
            candles = data['data']['candles']
            return candles
    elif response.status_code == 429:
        print("Rate limit hit! Sleeping for 60 seconds...")
        time.sleep(60)
        return get_1min_candles_chunk(instrument_key, start_date, end_date)
    elif response.status_code in [401, 403]:
        raise ValueError(f"Authentication error: {response.status_code} - {response.text}. Please check your access token.")
    else:
        print(f"Error fetching {start_date} to {end_date}: {response.status_code} - {response.text}")
    return []

def resample_to_5min(candles):
    if not candles:
        return pd.DataFrame()
    
    # Upstox candle format: [timestamp, open, high, low, close, volume, oi]
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
    df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    df = df.set_index('timestamp')
    df = df.sort_index()
    df = df[~df.index.duplicated(keep='first')]
    
    # Resample to 5-minute
    df_5min = df.resample('5min').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum',
        'oi': 'last'
    }).dropna(subset=['open', 'close'])
    
    return df_5min.reset_index()

def download_5yr_history():
    if not TOKEN:
        print("ERROR: UPSTOX_ACCESS_TOKEN not found in .env. Please run upstox_auth.py first.")
        return

    tokens_file = "data/upstox_tokens.json"
    if not os.path.exists(tokens_file):
        print("ERROR: Tokens file not found. Please run upstox_token_mapper.py first.")
        return

    with open(tokens_file, "r") as f:
        instruments = json.load(f)

    os.makedirs("data/historical", exist_ok=True)

    # 5 Years from today
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5*365)

    print(f"Starting 5-year download from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}...")

    total_stocks = len(instruments)
    for idx, (symbol, key) in enumerate(instruments.items(), 1):
        out_path = f"data/historical/{symbol}_5min.csv"
        if os.path.exists(out_path):
            print(f"[{idx}/{total_stocks}] Skipping {symbol} - Already downloaded.")
            continue

        print(f"[{idx}/{total_stocks}] Downloading {symbol} ({key})...")
        
        current_start = start_date
        all_candles = []
        
        while current_start < end_date:
            current_end = current_start + timedelta(days=30)
            if current_end > end_date:
                current_end = end_date
                
            s_str = current_start.strftime("%Y-%m-%d")
            e_str = current_end.strftime("%Y-%m-%d")
            
            # Fetch 1-month chunk
            chunk = get_1min_candles_chunk(key, s_str, e_str)
            if chunk:
                all_candles.extend(chunk)
                
            current_start = current_end + timedelta(days=1)
            time.sleep(0.5) # Prevent aggressive rate limiting (max 50/sec)
            
        if all_candles:
            print(f"  Resampling {len(all_candles)} 1-minute candles to 5-minute...")
            df_5min = resample_to_5min(all_candles)
            df_5min.to_csv(out_path, index=False)
            print(f"  SUCCESS: Saved {len(df_5min)} 5-minute candles to {out_path}")
        else:
            print(f"  WARNING: No data found for {symbol}")
            
        # Optional sleep between stocks to avoid 2000/30min limit too fast
        time.sleep(2)

if __name__ == "__main__":
    download_5yr_history()
