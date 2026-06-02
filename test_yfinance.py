#!/usr/bin/env python
"""Direct yfinance test"""
import sys
import yfinance as yf

print("\n" + "="*80)
print("YFINANCE DIRECT TEST - Python 3.10.11")
print("="*80 + "\n")

try:
    print("Testing yfinance 0.2.28 API...")
    
    # Test basic call WITHOUT progress parameter
    ticker = yf.Ticker("RELIANCE.NS")
    print("✅ Ticker created")
    
    # Test history WITHOUT progress parameter
    hist = ticker.history(period="30d", interval="1d")
    print(f"✅ History fetched: {len(hist)} bars")
    
    # Test news access
    news = ticker.news
    print(f"✅ News fetched: {len(news) if news else 0} items")
    if news:
        print(f"   First headline: {news[0].get('title', 'N/A')[:60]}")
    
    print("\n✅ ALL YFINANCE CALLS WORKING!")
    
except TypeError as e:
    print(f"\n❌ YFINANCE API ERROR:")
    print(f"   {str(e)}")
    print("\n   Likely cause: Unsupported parameter for this yfinance version")
    
except Exception as e:
    print(f"\n❌ ERROR: {type(e).__name__}: {str(e)}")

print("\n" + "="*80 + "\n")
