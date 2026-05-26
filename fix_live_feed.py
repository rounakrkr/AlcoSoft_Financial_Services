#!/usr/bin/env python3
"""
Quick diagnostic script to manually start the live feed
with stocks from the existing briefing.
"""

import json
import os
import sys

os.chdir(r"c:\Extra Programs\Files\AlcoSoft_Financial_Services")

# Add project root to path
sys.path.insert(0, os.getcwd())

from core.state_manager import load_briefing
from core.data_fetcher import start_live_feed

print("=" * 60)
print("ALCOSOFT LIVE FEED FIX")
print("=" * 60)

# Load the briefing
briefing = load_briefing()

if not briefing:
    print("ERROR: No briefing found!")
    sys.exit(1)

print(f"✅ Briefing loaded from: data/session_briefing.json")
print(f"   Generated: {briefing.get('generated_at')}")

# Extract stocks
approved  = [s["ticker"] for s in briefing.get("approved_stocks", [])]
watchlist = [s["ticker"] for s in briefing.get("watchlist", [])]
all_stocks = list(dict.fromkeys(approved + watchlist))

print(f"\n📊 Stocks to subscribe:")
print(f"   War Room   ({len(approved)}): {approved}")
print(f"   Watchlist  ({len(watchlist)}): {watchlist[:5]}... (showing first 5)")
print(f"   Total: {len(all_stocks)} unique stocks")

# Start live feed
print(f"\n🔌 Starting live feed for {len(all_stocks)} symbols...")
try:
    start_live_feed(all_stocks)
    print("✅ Live feed started successfully!")
    print("\nNow ticks should start flowing...")
    print("Check logs with: tail -f data/alcosoft.log")
except Exception as e:
    print(f"❌ ERROR: {e}")
    sys.exit(1)
