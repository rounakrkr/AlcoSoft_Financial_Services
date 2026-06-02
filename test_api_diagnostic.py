#!/usr/bin/env python
"""Deep diagnostic of yfinance API issue"""
import sys
import requests
import json

print("\n=== YFINANCE API DIAGNOSTIC ===\n")

# Test 1: Direct HTTP request to Yahoo Finance
print("[TEST 1] Direct HTTP Request to Yahoo Finance")
print("-" * 60)

url = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/RELIANCE.NS?modules=price"
print(f"URL: {url}")

try:
    response = requests.get(url, timeout=10)
    print(f"Status Code: {response.status_code}")
    print(f"Content Length: {len(response.content)} bytes")
    print(f"Headers: Content-Type = {response.headers.get('Content-Type')}")
    
    if response.status_code == 200:
        try:
            data = response.json()
            print(f"✅ JSON parsed successfully")
            print(f"Keys in response: {list(data.keys())[:5]}")
        except json.JSONDecodeError as e:
            print(f"❌ JSON Decode Error: {str(e)[:80]}")
            print(f"Response preview: {response.text[:200]}")
    else:
        print(f"❌ HTTP Error: {response.status_code}")
        print(f"Response: {response.text[:200]}")
        
except requests.exceptions.RequestException as e:
    print(f"❌ Request Error: {str(e)[:80]}")

# Test 2: Check yfinance version
print("\n[TEST 2] yfinance Version")
print("-" * 60)
try:
    import yfinance as yf
    print(f"yfinance version: {yf.__version__}")
except Exception as e:
    print(f"Error: {e}")

# Test 3: Try direct Ticker initialization with debug
print("\n[TEST 3] yfinance Ticker Initialization")
print("-" * 60)
try:
    import yfinance as yf
    ticker = yf.Ticker("RELIANCE.NS")
    print(f"✅ Ticker object created")
    print(f"Ticker info type: {type(ticker.info)}")
    try:
        info = ticker.info
        print(f"✅ Info fetched: {len(str(info))} characters")
    except Exception as e:
        print(f"❌ Info error: {str(e)[:80]}")
except Exception as e:
    print(f"❌ Ticker error: {str(e)[:80]}")

print("\n=== END DIAGNOSTIC ===\n")
