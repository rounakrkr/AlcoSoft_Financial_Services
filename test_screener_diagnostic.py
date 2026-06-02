#!/usr/bin/env python
"""Comprehensive screener diagnostic"""
import sys
import yfinance as yf
import pandas as pd
import ta
from datetime import datetime

print("\n" + "="*80)
print("SCREENER DIAGNOSTIC TEST - CHECKING FOR HIDDEN ISSUES")
print("="*80 + "\n")

# Test 1: Single stock fetch
print("[TEST 1] Single Stock Data Fetch (RELIANCE)")
print("-" * 60)
try:
    ticker = yf.Ticker("RELIANCE.NS")
    hist = ticker.history(period="30d", interval="1d")
    
    if hist.empty:
        print("❌ No data returned from yfinance")
    else:
        print(f"✅ Data fetched: {len(hist)} bars")
        print(f"   Latest close: {hist['Close'].iloc[-1]:.2f}")
        print(f"   Date range: {hist.index[0].date()} to {hist.index[-1].date()}")
        
        # Test TA-Lib calculations
        close = hist["Close"]
        rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        ema20 = ta.trend.EMAIndicator(close, window=20).ema_indicator().iloc[-1]
        print(f"   RSI(14): {rsi:.1f}")
        print(f"   EMA(20): {ema20:.2f}")
        print("   ✅ TA calculations working")
except Exception as e:
    print(f"❌ ERROR: {type(e).__name__}: {str(e)[:80]}")

# Test 2: News fetch (the problematic one)
print("\n[TEST 2] News Fetch (ticker.news)")
print("-" * 60)
try:
    ticker = yf.Ticker("RELIANCE.NS")
    try:
        news = ticker.news
        if news:
            print(f"✅ News fetched: {len(news)} items")
            if len(news) > 0:
                print(f"   First: {news[0].get('title', 'N/A')[:60]}")
        else:
            print(f"⚠️  News is empty but no error")
    except (ValueError, Exception) as e:
        print(f"⚠️  News fetch error (recoverable): {str(e)[:60]}")
except Exception as e:
    print(f"❌ CRITICAL ERROR: {type(e).__name__}: {str(e)[:80]}")

# Test 3: Multiple stocks (10)
print("\n[TEST 3] Multiple Stocks (10 NIFTY stocks)")
print("-" * 60)
symbols = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS", 
           "LT.NS", "ITBP.NS", "MARUTI.NS", "NESTLEIND.NS", "HDFC.NS"]
success = 0
failed = 0
for symbol in symbols:
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="30d", interval="1d")
        if not hist.empty and len(hist) >= 15:
            success += 1
            print(f"  ✅ {symbol:<20} {len(hist)} bars")
        else:
            failed += 1
            print(f"  ⚠️  {symbol:<20} Empty data")
    except Exception as e:
        failed += 1
        print(f"  ❌ {symbol:<20} {str(e)[:40]}")

print(f"\nResults: {success} success, {failed} failed")

# Test 4: NIFTY 50 index
print("\n[TEST 4] NIFTY 50 Index Fetch (^NSEI)")
print("-" * 60)
try:
    nifty = yf.Ticker("^NSEI")
    hist = nifty.history(period="5d", interval="1d")
    if hist.empty:
        print("❌ No data for NIFTY")
    else:
        closes = hist["Close"].tolist()
        print(f"✅ NIFTY data: {len(hist)} bars")
        print(f"   Closes: {[f'{c:.0f}' for c in closes]}")
except Exception as e:
    print(f"❌ ERROR: {type(e).__name__}: {str(e)[:80]}")

print("\n" + "="*80)
print("DIAGNOSTIC COMPLETE")
print("="*80 + "\n")
