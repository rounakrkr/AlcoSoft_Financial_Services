import pandas as pd
import numpy as np
import itertools
from datetime import time
import json
import requests
import time as time_lib
from urllib.parse import quote
from ta.trend import ADXIndicator, EMAIndicator, MACD
from ta.momentum import RSIIndicator
import concurrent.futures
import os

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
    "LTIMINDTREE", "BPCL", "UPL", "APOLLOHOSP", "BAJAJ-AUTO"
]

def fetch_stock_data(symbol):
    # Use disk cache directly
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "cache")
    disk_cache_file = os.path.join(cache_dir, f"{symbol}_60d_5m.pkl")
    if os.path.exists(disk_cache_file):
        df = pd.read_pickle(disk_cache_file)
        return df

    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{quote(symbol+'.NS', safe='')}"
        params = {"range": "60d", "interval": "5m", "includePrePost": "false", "events": "div"}
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        
        chart = response.json().get("chart", {})
        results = chart.get("result") or []
        if not results: return None
        result = results[0]
        timestamps = result.get("timestamp") or []
        quote_data = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        
        index = pd.to_datetime(timestamps, unit="s", utc=True)
        exchange_tz = ((result.get("meta") or {}).get("exchangeTimezoneName") or "Asia/Kolkata")
        index = index.tz_convert(exchange_tz)
        
        df = pd.DataFrame({
            "Open": quote_data.get("open"),
            "High": quote_data.get("high"),
            "Low": quote_data.get("low"),
            "Close": quote_data.get("close"),
            "Volume": quote_data.get("volume"),
        }, index=index)
        df.dropna(inplace=True)
        
        os.makedirs(cache_dir, exist_ok=True)
        df.to_pickle(disk_cache_file)
        
        return df
    except Exception as e:
        return None

def calculate_indicators(df):
    if df is None or df.empty or len(df) < 50: return None
    try:
        # VWAP
        df['hlc3'] = (df['High'] + df['Low'] + df['Close']) / 3
        df['vwap'] = (df['hlc3'] * df['Volume']).groupby(df.index.date).cumsum() / df['Volume'].groupby(df.index.date).cumsum()
        
        # EMA20 & 21
        df['ema20'] = EMAIndicator(close=df['Close'], window=20).ema_indicator()
        df['ema21'] = EMAIndicator(close=df['Close'], window=21).ema_indicator()
        
        # RSI 14
        df['rsi'] = RSIIndicator(close=df['Close'], window=14).rsi()
        
        # MACD
        macd_ind = MACD(close=df['Close'])
        df['macd'] = macd_ind.macd()
        
        # ADX (14)
        adx_ind = ADXIndicator(high=df['High'], low=df['Low'], close=df['Close'], window=14)
        df['adx'] = adx_ind.adx()
        
        # Volume SMA
        df['vol_sma20'] = df['Volume'].rolling(20).mean()
        
        # 10 Period High of Previous Candle
        df['high_10_prev'] = df['High'].rolling(10).max().shift(1)
        
        df.dropna(inplace=True)
        return df
    except Exception:
        return None

def get_all_data():
    print("Loading 60-day 5-min data for NIFTY 50 from cache/api...")
    stock_dfs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_stock_data, sym): sym for sym in NIFTY_50}
        for future in concurrent.futures.as_completed(futures):
            sym = futures[future]
            df = future.result()
            df = calculate_indicators(df)
            if df is not None:
                stock_dfs[sym] = df
    print(f"Successfully processed {len(stock_dfs)} stocks.")
    return stock_dfs

def backtest_strategy(df, buy_signals, sell_signals, tsl_activate_pct=0.012, tsl_trail_pct=0.002):
    capital = 5000.0
    margin_mult = 5
    buying_power = capital * margin_mult
    
    in_trade = False
    entry_price = 0
    position_size = 0
    highest_price = 0
    
    tsl_price = 0
    tsl_activated = False
    
    trades = []
    
    times = df.index.time
    highs = df['High'].values
    lows = df['Low'].values
    closes = df['Close'].values
    b_sigs = buy_signals.values
    s_sigs = sell_signals.values
    
    market_open = time(9, 15)
    market_close = time(15, 10)
    
    for i in range(1, len(df)):
        current_time = times[i]
        is_market_open = market_open <= current_time < market_close
        is_square_off = current_time >= market_close
        
        if in_trade:
            highest_price = max(highest_price, highs[i])
            
            # TSL Logic
            activation_threshold = entry_price * (1.0 + tsl_activate_pct)
            if highest_price >= activation_threshold:
                tsl_activated = True
                new_tsl = highest_price * (1.0 - tsl_trail_pct)
                tsl_price = max(tsl_price, new_tsl)
            
            exit_price = None
            
            # Stop Loss check
            if tsl_activated and lows[i] <= tsl_price:
                exit_price = tsl_price
            # SELL_EMA_MOMENTUM_LOSS Check
            elif s_sigs[i]:
                exit_price = closes[i]
            # Square off
            elif is_square_off:
                exit_price = closes[i]
                
            if exit_price:
                pnl = (exit_price - entry_price) * position_size
                trades.append(pnl)
                in_trade = False
                tsl_activated = False
                tsl_price = 0
                
        elif not in_trade and is_market_open:
            if b_sigs[i]:
                entry_price = closes[i]
                position_size = int(buying_power // entry_price)
                highest_price = entry_price
                tsl_price = 0
                tsl_activated = False
                in_trade = True

    return trades

def run_grid_search():
    stock_dfs = get_all_data()
    
    # SELL_EMA_MOMENTUM_LOSS is universal: Close(1) < EMA21(1)
    sell_signals = {}
    for sym, df in stock_dfs.items():
        sell_signals[sym] = df['Close'].shift(1) < df['ema21'].shift(1)
        
    params = {
        'rsi': [55, 61, 65, 70],
        'vol': [1.0, 1.25, 1.5, 2.0],
        'adx': [0, 20, 25],
        'solid': [True, False],
        'macd_pos': [True, False]
    }
    
    keys, values = zip(*params.items())
    combinations = [dict(zip(keys, v)) for v in itertools.product(*values)]
    
    print(f"Testing {len(combinations)} algorithmic permutations of Strategy 13...")
    results = []
    
    # Iterate through combos
    count = 0
    for combo in combinations:
        count += 1
        if count % 20 == 0:
            print(f"Processed {count}/{len(combinations)}...")
            
        total_trades = []
        for sym, df in stock_dfs.items():
            # Base Strategy 13 conditions
            base_sig = (
                (df['Close'].shift(1) > df['vwap']) &
                (df['ema20'].shift(1) > df['vwap']) &
                (df['Close'] > df['high_10_prev'])
            )
            
            # Apply filters
            if combo['rsi'] > 0:
                base_sig = base_sig & (df['rsi'].shift(1) > combo['rsi'])
                
            if combo['vol'] > 1.0:
                base_sig = base_sig & (df['Volume'] > (df['vol_sma20'] * combo['vol']))
                
            if combo['adx'] > 0:
                base_sig = base_sig & (df['adx'] > combo['adx'])
                
            if combo['solid']:
                base_sig = base_sig & (df['Close'] > df['Open'])
                
            if combo['macd_pos']:
                base_sig = base_sig & (df['macd'] > 0)
                
            trades = backtest_strategy(df, base_sig, sell_signals[sym])
            total_trades.extend(trades)
            
        total_pnl = sum(total_trades)
        total_return_pct = (total_pnl / 5000.0) * 100
        w_rate = (len([t for t in total_trades if t > 0]) / len(total_trades) * 100) if total_trades else 0
        
        # Only keep combos with decent trade count and WR > 40% to save memory
        if len(total_trades) > 50 and w_rate > 40.0:
            results.append({
                "params": combo,
                "return_pct": round(total_return_pct, 2),
                "win_rate": round(w_rate, 2),
                "trades": len(total_trades)
            })
            
    # Sort primarily by Win Rate, then by Return
    results.sort(key=lambda x: (x['win_rate'], x['return_pct']), reverse=True)
    
    print("\n=== TOP 10 HOLY GRAIL COMBINATIONS ===")
    for i, res in enumerate(results[:10]):
        print(f"\nRank {i+1}: WR {res['win_rate']}% | Ret {res['return_pct']}% | Trades {res['trades']}")
        print(f"Params: RSI>{res['params']['rsi']}, Vol>{res['params']['vol']}x, ADX>{res['params']['adx']}, Solid={res['params']['solid']}, MACD_Pos={res['params']['macd_pos']}")
        
    with open("c:/Extra Programs/Files/AlcoSoft_Financial_Services/research/holy_grail_results.json", "w") as f:
        json.dump(results, f, indent=4)

if __name__ == "__main__":
    run_grid_search()
