"""Check which GIFT Nifty / Nifty proxy tickers are available"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screener.morning_screener import _fetch_yahoo_history
import pandas as pd

tickers = [
    "^SGXNIFTY",
    "NIFTY_GIF.NS",
    "NIFTYGI.NS",
    "^NSEBANK",
    "^NSEI",
    # GIFT Nifty ETFs / Futures proxies
    "NIFTYBEES.NS",     # Nifty ETF
    "BANKNIFTY.NS",
    "0QZZ.IL",          # GIFT Nifty on some exchanges
    "SGXNIFTY.NS",
    "IN1!",             # Nifty futures
    "NQ=F",             # Nasdaq futures (for reference)
]

for t in tickers:
    try:
        df = _fetch_yahoo_history(t, period="10d", interval="1d")
        if df is not None and not df.empty:
            df.columns = [c.lower() for c in df.columns]
            cl = df["close"].iloc[-1]
            print(f"OK   {t:20s} -> {len(df)} rows | Close: {cl:.2f}")
        else:
            print(f"EMPTY {t}")
    except Exception as e:
        err = str(e)[:80]
        print(f"ERR  {t:20s} -> {err}")

# Also try 5m interval for those that work
print("\n--- 5-minute data test ---")
for t in ["^NSEI", "NIFTYBEES.NS", "^NSEBANK"]:
    try:
        df = _fetch_yahoo_history(t, period="5d", interval="5m")
        if df is not None and not df.empty:
            df.columns = [c.lower() for c in df.columns]
            # Check if there are pre-9:15 candles
            early = df[df.index.time < pd.Timestamp("09:15").time()]
            print(f"OK   {t:20s} -> {len(df)} candles | Pre-9:15: {len(early)}")
            if not early.empty:
                print(f"     First pre-9:15: {early.index[0]} | Last: {early.index[-1]}")
        else:
            print(f"EMPTY {t}")
    except Exception as e:
        print(f"ERR  {t:20s} -> {str(e)[:80]}")
