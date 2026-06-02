#!/usr/bin/env python
"""Test different yfinance suffixes"""
import yfinance as yf

print("\n" + "="*60)
print("TESTING YFINANCE SUFFIX FORMATS")
print("="*60 + "\n")

suffixes_to_test = [
    ("RELIANCE.NS", "NSE style"),
    ("RELIANCE.BO", "BSE style"),
    ("RELIANCE", "No suffix"),
    ("0939300020", "Token ID"),
]

for ticker_str, desc in suffixes_to_test:
    print(f"Testing: {ticker_str:<20} ({desc})")
    try:
        ticker = yf.Ticker(ticker_str)
        hist = ticker.history(period="5d", interval="1d")
        if hist.empty:
            print(f"  ❌ Empty data\n")
        else:
            print(f"  ✅ Success! {len(hist)} bars")
            print(f"     Latest: {hist['Close'].iloc[-1]:.2f}\n")
    except Exception as e:
        print(f"  ❌ Error: {str(e)[:50]}\n")

print("="*60 + "\n")
