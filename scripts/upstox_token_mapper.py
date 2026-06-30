import os
import json
import pandas as pd
import requests

# The list of 50 + 50 stocks
NIFTY_100 = [
    # NIFTY 50
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "HCLTECH", "AXISBANK", "ASIANPAINT", "MARUTI",
    "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO", "NESTLEIND",
    "TECHM", "POWERGRID", "NTPC", "ONGC", "BAJFINANCE",
    "BAJAJFINSV", "ADANIENT", "ADANIPORTS", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "GRASIM", "HEROMOTOCO", "HINDALCO", "INDUSINDBK",
    "JSWSTEEL", "M&M", "SBILIFE", "TATACONSUM", "TATAMOTOR",
    "TATASTEEL", "BRITANNIA", "CIPLA", "COALINDIA", "HDFCLIFE",
    "LTIMINDTREE", "BPCL", "UPL", "APOLLOHOSP", "BAJAJ-AUTO",
    
    # NIFTY NEXT 50
    "ADANIGREEN", "ADANIPOWER", "AMBUJACEM", "AWL", "BEL",
    "BERGEPAINT", "BOSCHLTD", "CANBK", "CHOLAFIN", "COLPAL",
    "DLF", "DABUR", "GAIL", "GODREJCP", "HAL", "HAVELLS",
    "ICICIGI", "ICICIPRULI", "INDIGO", "IOC", "IRCTC", "IRFC",
    "JINDALSTEL", "JIOFIN", "LICI", "LODHA", "MARICO", "MUTHOOTFIN",
    "NAUKRI", "PIDILITIND", "PIIND", "PNB", "PFC", "RECLTD",
    "SAIL", "SHREECEM", "SIEMENS", "SRF", "TORNTPHARM", "TRENT",
    "TVSMOTOR", "UNITEDSPR", "VEDL", "ZOMATO"
]

def map_tokens():
    print("Downloading Upstox NSE_EQ instrument list...")
    url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        from io import BytesIO
        import gzip
        
        with gzip.open(BytesIO(response.content), 'rt') as f:
            df = pd.read_csv(f)
            
        print(f"Downloaded {len(df)} instruments.")
        
        mapping = {}
        # Columns in Upstox CSV usually: instrument_key, exchange_token, tradingsymbol, name, last_price, expiry, strike, tick_size, lot_size, instrument_type, option_type, exchange
        # Let's handle variations
        sym_col = "tradingsymbol" if "tradingsymbol" in df.columns else "trading_symbol"
        key_col = "instrument_key"
        
        alias_map = {
            "TATAMOTOR": "TATAMOTORS",
            "LTIMINDTREE": "LTIM",
            "UNITEDSPR": "MCDOWELL-N",
            "ZOMATO": "ZOMATO" # Check if it's there
        }
        
        for index, row in df.iterrows():
            sym = str(row[sym_col])
            clean_sym = sym.replace("-EQ", "")
            
            # Direct match
            if clean_sym in NIFTY_100:
                mapping[clean_sym] = row[key_col]
            else:
                # Alias match
                for original, alias in alias_map.items():
                    if clean_sym == alias:
                        mapping[original] = row[key_col]
        
        # Check missing
        missing = [s for s in NIFTY_100 if s not in mapping]
        if missing:
            print(f"WARNING: Could not find instrument keys for {len(missing)} stocks: {missing}")
        
        os.makedirs("data", exist_ok=True)
        with open("data/upstox_tokens.json", "w") as f:
            json.dump(mapping, f, indent=4)
            
        print(f"SUCCESS: Mapped {len(mapping)} stocks and saved to data/upstox_tokens.json!")
    
    except Exception as e:
        print(f"Failed to map tokens: {e}")

if __name__ == "__main__":
    map_tokens()
